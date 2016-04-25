import tensorflow as tf
from bidirectional_rnn_layer import BidirectionalRNNLayer
from tensorflow.models.rnn import rnn_cell
from noisy_gru_cell import NoisyGRUCell


class DeepSentenceEncoder(object):
    def __init__(self, max_input_len, vocabulary, embedding_size, rnn_size, depth, dropout_placeholder,
                 is_training, use_noisy_activations=False, name="deep_sentence_encoder"):
        assert (depth > 0), "Depth of Deep Sentence Encoder must be a positive non-zero number"

        with tf.variable_scope(name):
            self.inputs = \
                    [tf.placeholder(tf.int32, shape=[None], name="input_{}".format(i)) for i in range(max_input_len + 2)]
            self.sentence_lengths = tf.placeholder(tf.int64, shape=[None], name="sequence_lengths")

            self.word_embeddings = tf.Variable(tf.random_uniform([len(vocabulary), embedding_size], -1.0, 1.0))

            embedded_inputs = [tf.nn.embedding_lookup(self.word_embeddings, input_) for input_ in self.inputs]

            dropped_embedded_inputs = [tf.nn.dropout(i, dropout_placeholder) for i in embedded_inputs]

            layer_inputs = [dropped_embedded_inputs]

            for i in range(depth):
                scope = "bidilayer-{0}".format(i)

                with tf.variable_scope(scope):
                    if use_noisy_activations:
                        forward_gru = NoisyGRUCell(rnn_size, is_training, input_size=embedding_size)
                        backward_gru = NoisyGRUCell(rnn_size, is_training, input_size=embedding_size)
                    else:
                        forward_gru = rnn_cell.GRUCell(rnn_size, input_size=embedding_size)
                        backward_gru = rnn_cell.GRUCell(rnn_size, input_size=embedding_size)
                    bidi_layer = BidirectionalRNNLayer(forward_gru, backward_gru,
                                                       layer_inputs[i],
                                                       self.sentence_lengths)

                    layer_outputs_dropped = [tf.nn.dropout(i, dropout_placeholder) for i in bidi_layer.outputs_bidi]
                    layer_inputs.append(layer_outputs_dropped)


            self.outputs_bidi = bidi_layer.outputs_bidi ## tady nechceme dropout
            self.encoded = bidi_layer.encoded

            self.attention_tensor = \
                    tf.concat(1, [tf.expand_dims(o, 1) for o in self.outputs_bidi])
                    #tf.transpose(tf.concat(1, [tf.expand_dims(o, 1) for o in self.outputs_bidi]), [0, 2, 1])


    def feed_dict(self, sentences, batch_size, dicts=None):
        if dicts == None:
            dicts = [{} for _ in range(len(sentences) / batch_size + int(len(sentences) % batch_size > 0))]

        for fd, start in zip(dicts, range(0, len(sentences, batch_size))):
            fd[self.sentence_lengths] = np.array([min(args.maximum_output, len(s)) + 2 for s in src_sentences])
            vectors, _ = \
                    self.vocabulary.sentences_to_tensor(sentences, self.max_input_len, train=train)
            for words_plc, words_tensor in zip(self.inputs, vectors):
                fd[words_plc] = words_tensor

        return dicts