# -*- coding:utf-8 -*-
__author__ = 'Randolph'

import tensorflow as tf
from tensorflow.contrib import rnn
from tensorflow.contrib.layers import batch_norm


def linear(input_, output_size, scope=None):
    """
    Linear map: output[k] = sum_i(Matrix[k, i] * args[i] ) + Bias[k]
    Args:
        input_: a tensor or a list of 2D, batch x n, Tensors.
        output_size: int, second dimension of W[i].
        scope: VariableScope for the created subgraph; defaults to "Linear".
    Returns:
        A 2D Tensor with shape [batch x output_size] equal to
        sum_i(args[i] * W[i]), where W[i]s are newly created matrices.
    Raises:
        ValueError: if some of the arguments has unspecified or wrong shape.
    """

    shape = input_.get_shape().as_list()
    if len(shape) != 2:
        raise ValueError("Linear is expecting 2D arguments: {0}".format(str(shape)))
    if not shape[1]:
        raise ValueError("Linear expects shape[1] of arguments: {0}".format(str(shape)))
    input_size = shape[1]

    # Now the computation.
    with tf.variable_scope(scope or "SimpleLinear"):
        W = tf.get_variable("W", [output_size, input_size], dtype=input_.dtype)
        b = tf.get_variable("b", [output_size], dtype=input_.dtype)

    return tf.nn.xw_plus_b(input_, tf.transpose(W), b)


def highway(input_, size, num_layers=1, bias=-2.0, f=tf.nn.relu, scope='Highway'):
    """
    Highway Network (cf. http://arxiv.org/abs/1505.00387).
    t = sigmoid(Wy + b)
    z = t * g(Wy + b) + (1 - t) * y
    where g is nonlinearity, t is transform gate, and (1 - t) is carry gate.
    """

    with tf.variable_scope(scope):
        for idx in range(num_layers):
            g = f(linear(input_, size, scope=('highway_lin_{0}'.format(idx))))
            t = tf.sigmoid(linear(input_, size, scope=('highway_gate_{0}'.format(idx))) + bias)
            output = t * g + (1. - t) * input_
            input_ = output

    return output


class TextCRNN(object):
    """A CRNN for text classification."""

    def __init__(
            self, sequence_length, num_classes, vocab_size, lstm_hidden_size, fc_hidden_size, embedding_size,
            embedding_type, filter_sizes, num_filters, l2_reg_lambda=0.0, pretrained_embedding=None):

        # Placeholders for input, output, dropout_prob and training_tag
        self.input_x = tf.placeholder(tf.int32, [None, sequence_length], name="input_x")
        self.input_y = tf.placeholder(tf.float32, [None, num_classes], name="input_y")
        self.dropout_keep_prob = tf.placeholder(tf.float32, name="dropout_keep_prob")
        self.is_training = tf.placeholder(tf.bool, name="is_training")

        self.global_step = tf.Variable(0, trainable=False, name="Global_Step")

        # Embedding Layer
        with tf.device('/cpu:0'), tf.name_scope("embedding"):
            # Use random generated the word vector by default
            # Can also be obtained through our own word vectors trained by our corpus
            if pretrained_embedding is None:
                self.embedding = tf.Variable(tf.random_uniform([vocab_size, embedding_size], minval=-1.0, maxval=1.0,
                                                               dtype=tf.float32), trainable=True, name="embedding")
            else:
                if embedding_type == 0:
                    self.embedding = tf.constant(pretrained_embedding, dtype=tf.float32, name="embedding")
                if embedding_type == 1:
                    self.embedding = tf.Variable(pretrained_embedding, trainable=True,
                                                 dtype=tf.float32, name="embedding")
            self.embedded_sentence = tf.nn.embedding_lookup(self.embedding, self.input_x)
            self.embedded_sentence_expanded = tf.expand_dims(self.embedded_sentence, axis=-1)

        # Create a convolution + maxpool layer for each filter size
        pooled_outputs = []

        for filter_size in filter_sizes:
            with tf.name_scope("conv-filter{0}".format(filter_size)):
                # Convolution Layer
                filter_shape = [filter_size, embedding_size, 1, num_filters]
                W = tf.Variable(tf.truncated_normal(shape=filter_shape, stddev=0.1, dtype=tf.float32), name="W")
                b = tf.Variable(tf.constant(value=0.1, shape=[num_filters], dtype=tf.float32), name="b")
                conv = tf.nn.conv2d(
                    self.embedded_sentence_expanded,
                    W,
                    strides=[1, 1, 1, 1],
                    padding="VALID",
                    name="conv")

                conv = tf.nn.bias_add(conv, b)

                # Batch Normalization Layer
                conv_bn = tf.layers.batch_normalization(conv, training=self.is_training)

                # Apply nonlinearity
                conv_out = tf.nn.relu(conv_bn, name="relu")

            with tf.name_scope("pool-filter{0}".format(filter_size)):
                # Maxpooling over the outputs
                pooled = tf.nn.max_pool(
                    conv_out,
                    ksize=[1, sequence_length - filter_size + 1, 1, 1],
                    strides=[1, 1, 1, 1],
                    padding="VALID",
                    name="pool")

            pooled_outputs.append(pooled)

        # Combine all the pooled features
        pool_flat_outputs = []
        for i in pooled_outputs:
            pool_flat = tf.reshape(i, shape=[-1, 1, num_filters])
            pool_flat = tf.nn.dropout(pool_flat, self.dropout_keep_prob)
            pool_flat_outputs.append(pool_flat)

        # Bi-LSTM Layer
        lstm_outputs = []

        for index, pool_flat in enumerate(pool_flat_outputs):
            with tf.variable_scope("Bi-lstm-{0}".format(index)):
                lstm_fw_cell = rnn.BasicLSTMCell(lstm_hidden_size)  # forward direction cell
                lstm_bw_cell = rnn.BasicLSTMCell(lstm_hidden_size)  # backward direction cell
                if self.dropout_keep_prob is not None:
                    lstm_fw_cell = rnn.DropoutWrapper(lstm_fw_cell, output_keep_prob=self.dropout_keep_prob)
                    lstm_bw_cell = rnn.DropoutWrapper(lstm_bw_cell, output_keep_prob=self.dropout_keep_prob)

                # Creates a dynamic bidirectional recurrent neural network
                # shape of `outputs`: tuple -> (outputs_fw, outputs_bw)
                # shape of `outputs_fw`: [batch_size, sequence_length, lstm_hidden_size]

                # shape of `state`: tuple -> (outputs_state_fw, output_state_bw)
                # shape of `outputs_state_fw`: tuple -> (c, h) c: memory cell; h: hidden state

                outputs, state = tf.nn.bidirectional_dynamic_rnn(lstm_fw_cell, lstm_bw_cell, pool_flat, dtype=tf.float32)
                # Concat output
                lstm_concat = tf.concat(outputs, axis=2)  # [batch_size, sequence_length, lstm_hidden_size * 2]
                lstm_out = tf.reduce_mean(lstm_concat, axis=1)  # [batch_size, lstm_hidden_size * 2]

                # shape of `lstm_outputs`: list -> len(filter_sizes) * [batch_size, lstm_hidden_size * 2]
                lstm_outputs.append(lstm_out)

        self.lstm_out = tf.concat(lstm_outputs, axis=1)  # [batch_size, lstm_hidden_size * 2 * len(filter_sizes)]

        # Fully Connected Layer
        with tf.name_scope("fc"):
            W = tf.Variable(tf.truncated_normal(shape=[lstm_hidden_size * 2 * len(filter_sizes), fc_hidden_size],
                                                stddev=0.1, dtype=tf.float32), name="W")
            b = tf.Variable(tf.constant(value=0.1, shape=[fc_hidden_size], dtype=tf.float32), name="b")
            self.fc = tf.nn.xw_plus_b(self.lstm_out, W, b)

            # Batch Normalization Layer
            self.fc_bn = tf.layers.batch_normalization(self.fc, training=self.is_training)

            # Apply nonlinearity
            self.fc_out = tf.nn.relu(self.fc_bn, name="relu")

        # Highway Layer
        self.highway = highway(self.fc_out, self.fc_out.get_shape()[1], num_layers=1, bias=0, scope="Highway")

        # Add dropout
        with tf.name_scope("dropout"):
            self.h_drop = tf.nn.dropout(self.highway, self.dropout_keep_prob)

        # Final scores
        with tf.name_scope("output"):
            W = tf.Variable(tf.truncated_normal(shape=[fc_hidden_size, num_classes],
                                                stddev=0.1, dtype=tf.float32), name="W")
            b = tf.Variable(tf.constant(value=0.1, shape=[num_classes], dtype=tf.float32), name="b")
            self.logits = tf.nn.xw_plus_b(self.h_drop, W, b, name="logits")
            self.scores = tf.sigmoid(self.logits, name="scores")

        # Calculate mean cross-entropy loss, L2 loss
        with tf.name_scope("loss"):
            losses = tf.nn.sigmoid_cross_entropy_with_logits(labels=self.input_y, logits=self.logits)
            losses = tf.reduce_mean(tf.reduce_sum(losses, axis=1), name="sigmoid_losses")
            l2_losses = tf.add_n([tf.nn.l2_loss(tf.cast(v, tf.float32)) for v in tf.trainable_variables()],
                                 name="l2_losses") * l2_reg_lambda
            self.loss = tf.add(losses, l2_losses, name="loss")
