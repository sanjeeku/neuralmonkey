"""Beam search decoder.

This module implements the beam search algorithm for the recurrent decoder.

As well as the recurrent decoder, this decoder works dynamically, which means
it uses the ``tf.while_loop`` function conditioned on both maximum output
length and list of finished hypotheses.

The beam search decoder works by appending data from ``SearchStepOutput``
objects to a ``SearchStepOutputTA`` object. The ``SearchStepOutput`` object
stores information about the hypotheses in the beam. Each hypothesis keeps its
score, its final token, and a pointer to a "parent" hypothesis, which is a
one-token-shorter hypothesis which shares the tokens with the child hypothesis.

For the beam search decoder to work, it must keep an inner state which stores
information about hypotheses in the beam. It is an object of type
``SearchState`` which stores, *for each hypothesis*, its sum of log
probabilities of the tokens, its length, finished flag, ID of the last token,
and the last decoder and attention states.

There is another inner state object here, the ``BeamSearchLoopState``. It is a
technical structure used with the ``tf.while_loop`` function. It stores all the
previously mentioned information, plus the decoder ``LoopState``, which is used
in the decoder when its own ``tf.while_loop`` function is used - this is not
the case when using beam search because we want to run the decoder's steps
manually.
"""
from typing import NamedTuple, List, Callable

import tensorflow as tf
from typeguard import check_argument_types

from neuralmonkey.decoding_function import BaseAttention
from neuralmonkey.model.model_part import ModelPart, FeedDict
from neuralmonkey.dataset import Dataset
from neuralmonkey.decoders.decoder import Decoder, LoopState
from neuralmonkey.vocabulary import (START_TOKEN_INDEX, END_TOKEN_INDEX,
                                     PAD_TOKEN_INDEX)

# pylint: disable=invalid-name
SearchState = NamedTuple("SearchState",
                         [("logprob_sum", tf.Tensor),  # beam x 1
                          ("lengths", tf.Tensor),  # beam x 1
                          ("finished", tf.Tensor),  # beam x 1
                          ("last_word_ids", tf.Tensor),  # beam x 1
                          ("last_state", tf.Tensor),  # beam x rnn_size
                          ("last_attns", tf.Tensor)])  # beam x ???

SearchStepOutput = NamedTuple("SearchStepOutput",
                              [("scores", tf.Tensor),
                               ("parent_ids", tf.Tensor),
                               ("token_ids", tf.Tensor)])

SearchStepOutputTA = NamedTuple("SearchStepOutputTA",
                                [("scores", tf.TensorArray),
                                 ("parent_ids", tf.TensorArray),
                                 ("token_ids", tf.TensorArray)])

BeamSearchLoopState = NamedTuple("BeamSearchLoopState",
                                 [("bs_state", SearchState),
                                  ("bs_output", SearchStepOutputTA),
                                  ("decoder_loop_state", LoopState)])
# pylint: enable=invalid-name


class BeamSearchDecoder(ModelPart):
    """In-graph beam search for batch size 1.

    The hypothesis scoring algorithm is taken from
    https://arxiv.org/pdf/1609.08144.pdf. Length normalization is parameter
    alpha from equation 14.
    """
    def __init__(self,
                 name: str,
                 parent_decoder: Decoder,
                 beam_size: int,
                 length_normalization: float,
                 max_steps: int = None,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None) -> None:
        ModelPart.__init__(self, name, save_checkpoint, load_checkpoint)
        check_argument_types()

        self.parent_decoder = parent_decoder
        self._beam_size = beam_size
        self._length_normalization = length_normalization

        self._max_steps = max_steps
        if self._max_steps is None:
            self._max_steps = parent_decoder.max_output_len

        self.outputs = self._decoding_loop()

    @property
    def beam_size(self):
        return self._beam_size

    @property
    def vocabulary(self):
        return self.parent_decoder.vocabulary

    def get_initial_loop_state(self, att_objects: List) -> BeamSearchLoopState:

        state = SearchState(
            logprob_sum=tf.zeros([self._beam_size]),
            lengths=tf.ones([self._beam_size], dtype=tf.int32),
            finished=tf.zeros([self._beam_size], dtype=tf.bool),
            last_word_ids=tf.fill([self._beam_size], START_TOKEN_INDEX),
            last_state=tf.reshape(
                tf.tile(self.parent_decoder.initial_state,
                        [self._beam_size, 1]),
                [self._beam_size, self.parent_decoder.rnn_size]),
            last_attns=[tf.zeros([self._beam_size, a.attn_size])
                        for a in att_objects])

        output_ta = SearchStepOutputTA(
            scores=tf.TensorArray(dtype=tf.float32, dynamic_size=True,
                                  size=0, name="beam_scores"),
            parent_ids=tf.TensorArray(dtype=tf.int32, dynamic_size=True,
                                      size=0, name="beam_parents"),
            token_ids=tf.TensorArray(dtype=tf.int32, dynamic_size=True,
                                     size=0, name="beam_tokens"))

        dec_loop_state = self.parent_decoder.get_initial_loop_state(
            att_objects, beam_size=self._beam_size)

        return BeamSearchLoopState(
            bs_state=state,
            bs_output=output_ta,
            decoder_loop_state=dec_loop_state)

    def _decoding_loop(self) -> SearchStepOutput:
        # collect attention objects
        att_objects = [self.parent_decoder.get_attention_object(e, False)
                       for e in self.parent_decoder.encoders]
        att_objects = [a for a in att_objects if a is not None]

        def cond(*args) -> tf.Tensor:
            bsls = BeamSearchLoopState(*args)
            return tf.less(bsls.decoder_loop_state.step, self._max_steps)

        final_state = tf.while_loop(
            cond,
            self.get_body(att_objects),
            self.get_initial_loop_state(att_objects))

        scores = final_state.bs_output.scores.stack()
        parent_ids = final_state.bs_output.parent_ids.stack()
        token_ids = final_state.bs_output.token_ids.stack()

        return SearchStepOutput(scores=scores,
                                parent_ids=parent_ids,
                                token_ids=token_ids)

    def get_body(self, att_objects: List[BaseAttention]) -> Callable[
            [BeamSearchLoopState], BeamSearchLoopState]:
        """Return a function that will act as the body for the ``tf.while_loop``
        call."""
        decoder_body = self.parent_decoder.get_body(att_objects, False)

        # pylint: disable=too-many-locals
        def body(*args) -> BeamSearchLoopState:
            """The beam search body function. This is where the beam search
            algorithm is implemented.

            Arguments:
                loop_state: ``BeamSearchLoopState`` instance (see the docs for
                    this module)
            """
            loop_state = BeamSearchLoopState(*args)
            bs_state = loop_state.bs_state
            dec_loop_state = loop_state.decoder_loop_state

            # don't want to use this decoder with uninitialized parent
            assert self.parent_decoder.step_scope.reuse

            # CALL THE DECODER BODY FUNCTION
            # TODO figure out why mypy throws too-many-arguments on this
            next_loop_state = decoder_body(*dec_loop_state)  # type: ignore

            logits = next_loop_state.prev_logits
            rnn_state = next_loop_state.prev_rnn_state
            rnn_output = next_loop_state.prev_rnn_output
            attns = next_loop_state.prev_contexts

            # mask the probabilities
            # shape(logprobs) = beam x vocabulary
            logprobs = tf.nn.log_softmax(logits)

            finished_mask = tf.expand_dims(tf.to_float(bs_state.finished), 1)
            unfinished_logprobs = (1. - finished_mask) * logprobs

            finished_row = tf.one_hot(
                PAD_TOKEN_INDEX,
                len(self.parent_decoder.vocabulary),
                dtype=tf.float32,
                on_value=0.,
                off_value=tf.float32.min)

            finished_logprobs = finished_mask * finished_row
            logprobs = unfinished_logprobs + finished_logprobs

            # update hypothesis scores
            # shape(hyp_probs) = beam x vocabulary
            hyp_probs = tf.expand_dims(bs_state.logprob_sum, 1) + logprobs

            # update hypothesis lengths
            hyp_lengths = bs_state.lengths + 1 - tf.to_int32(bs_state.finished)

            # shape(scores) = beam x vocabulary
            scores = hyp_probs / tf.expand_dims(
                self._length_penalty(hyp_lengths), 1)

            # flatten so we can use top_k
            scores_flat = tf.reshape(
                scores,
                [self._beam_size * len(self.parent_decoder.vocabulary)])

            # shape(both) = beam
            topk_scores, topk_indices = tf.nn.top_k(
                scores_flat, self._beam_size)

            topk_scores.set_shape([self._beam_size])
            topk_indices.set_shape([self._beam_size])

            # flatten the hypothesis probabilities
            hyp_probs_flat = tf.reshape(hyp_probs, [-1])

            # select logprobs of the best hyps (disregard lenghts)
            next_logprob_sum = tf.gather(hyp_probs_flat, topk_indices)
            # pylint: disable=no-member
            next_logprob_sum.set_shape([self._beam_size])
            # pylint: enable=no-member

            next_word_ids = tf.mod(topk_indices,
                                   len(self.parent_decoder.vocabulary))

            next_beam_ids = tf.div(topk_indices,
                                   len(self.parent_decoder.vocabulary))

            next_beam_prev_rnn_state = tf.gather(rnn_state, next_beam_ids)
            next_beam_prev_rnn_output = tf.gather(rnn_output, next_beam_ids)
            next_beam_prev_attns = [tf.gather(a, next_beam_ids) for a in attns]
            next_lengths = tf.gather(hyp_lengths, next_beam_ids)

            # update finished flags
            has_just_finished = tf.equal(next_word_ids, END_TOKEN_INDEX)
            next_finished = tf.logical_or(
                tf.gather(bs_state.finished, next_beam_ids),
                has_just_finished)

            prev_output = loop_state.bs_output

            step = dec_loop_state.step
            output = SearchStepOutputTA(
                scores=prev_output.scores.write(step, topk_scores),
                parent_ids=prev_output.parent_ids.write(step, next_beam_ids),
                token_ids=prev_output.token_ids.write(step, next_word_ids))

            search_state = SearchState(
                logprob_sum=next_logprob_sum,
                lengths=next_lengths,
                finished=next_finished,
                last_word_ids=next_word_ids,
                last_state=next_beam_prev_rnn_state,
                last_attns=next_beam_prev_attns)

            ## For run-time computation, the decoder needs:
            # - step
            # - input_symbol
            # - prev_rnn_state
            # - prev_rnn_output
            # - prev_contexts
            # - attention_loop_states
            # - finished

            ## For train-mode computation, it also needs
            # - train_inputs

            ## For recording the computation in time, it needs
            # - rnn_outputs (TA)
            # - logits (TA)
            # - mask (TA)

            ## Because of the beam search algorithm, it outputs
            ## (but does not not need)
            # - prev_logits

            ## During beam search decoding, we are not interested in recording
            ## of the computation as done by the decoder. The record is stored
            ## in search states and step outputs of this decoder.

            next_prev_logits = tf.gather(next_loop_state.prev_logits,
                                         next_beam_ids)

            next_prev_contexts = [tf.gather(ctx, next_beam_ids) for ctx in
                                  next_loop_state.prev_contexts]

            # Update the decoder next_loop_state
            next_loop_state = next_loop_state._replace(
                input_symbol=next_word_ids,
                prev_rnn_state=next_beam_prev_rnn_state,
                prev_rnn_output=next_beam_prev_rnn_output,
                prev_logits=next_prev_logits,
                prev_contexts=next_prev_contexts,
                finished=next_finished)

            ## TODO figure out what to supply for attention_loop_states
            ## they are tensorarrays so its not so easy as a simple gather.

            return BeamSearchLoopState(
                bs_state=search_state,
                bs_output=output,
                decoder_loop_state=next_loop_state)
        # pylint: enable=too-many-locals

        return body

    def feed_dict(self, dataset: Dataset, train: bool = False) -> FeedDict:
        """Populate the feed dictionary for the decoder object

        Arguments:
            dataset: The dataset to use for the decoder.
            train: Boolean flag, telling whether this is a training run
        """
        assert not train
        assert len(dataset) == 1

        return {}

    def _length_penalty(self, lengths):
        """lp term from eq. 14"""

        return ((5. + tf.to_float(lengths)) ** self._length_normalization /
                (5. + 1.) ** self._length_normalization)
