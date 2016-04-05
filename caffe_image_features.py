#!/usr/bin/env python

"""

Does the Image feature extraction by calling directly Caffe.
Based on tutorial http://www.marekrei.com/blog/transforming-images-to-feature-vectors/

"""
import sys, os
os.environ['GLOG_minloglevel'] = '4'
sys.path.append("caffe/python")
import os, argparse, caffe
import numpy as np
from learning_utils import log

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Image feature extraction")
    parser.add_argument("--model-prototxt", type=str, required=True)
    parser.add_argument("--model-parameters", type=str, required=True)
    parser.add_argument("--img-mean", type=str, required=True)
    parser.add_argument("--feature-layer", type=str, required=True)
    parser.add_argument("--image-directory", type=str, required=True)
    parser.add_argument("--image-list", type=argparse.FileType('r'), required=True)
    parser.add_argument("--output-file", type=argparse.FileType('wb'), required=True)
    args = parser.parse_args()

    log("Loading the ImageNet labels")
    with open("imagenet_synset_words.txt") as f:
         labels = f.readlines()


    # Setting this to CPU, but feel free to use GPU if you have CUDA installed
    caffe.set_mode_cpu()

    # Loading the Caffe model, setting preprocessing parameters
    log("Model starts loading")
    net = caffe.Classifier(args.model_prototxt, args.model_parameters,
                           mean=np.load(args.img_mean).mean(1).mean(1),
                           channel_swap=(2,1,0),
                           raw_scale=255,
                           image_dims=(256, 256))

    log("Model loaded")

    # Processing one image at a time, printint predictions and writing the vector to a file
    data = []
    input_images = []
    paths = []

    def process_batch(input_images, paths):
        prediction = net.predict(input_images, oversample=False)
        f_output = net.blobs[args.feature_layer].data.transpose((0,2,3,1)).copy()
        for img, p in zip(paths, prediction):
            print os.path.basename(img), ' : ' , labels[p.argmax()].strip() , ' (', p[p.argmax()] , ')'
        data.append(f_output)

    for i, image_path in enumerate(args.image_list):
        image_path = image_path.strip()
        paths.append(image_path)
        input_images.append(caffe.io.load_image(os.path.join(args.image_directory, image_path)))

        if len(input_images) >= 10:
            process_batch(input_images, paths)
            input_images = []
            paths = []

        if i % 99 == 0:
            log("Processed {} images.".format(i + 1))

    process_batch(input_images, paths)

    log("All images processed.")
    np.save(args.output_file, np.concatenate(data, axis=0))
    log("Features saved. Shape: {}".format(data[0][0].shape))
