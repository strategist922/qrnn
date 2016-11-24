# -*- coding: utf-8 -*-
from __future__ import absolute_import
import numpy as np

from keras import backend as K
from keras import activations, initializations, regularizers, constraints
from keras.layers import Layer, InputSpec

from keras.utils.np_utils import conv_output_length

import theano
import theano.tensor as T


def _dropout(x, level, noise_shape=None, seed=None):
    '''Sets entries in `x` to zero at random,
    while scaling the entire tensor.

    # Arguments
        x: tensor
        level: fraction of the entries in the tensor
            that will be set to 0.
        noise_shape: shape for randomly generated keep/drop flags,
            must be broadcastable to the shape of `x`
        seed: random seed to ensure determinism.
    '''
    from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
    if level < 0. or level >= 1:
        raise Exception('Dropout level must be in interval [0, 1[.')
    if seed is None:
        seed = np.random.randint(1, 10e6)

    rng = RandomStreams(seed=seed)
    retain_prob = 1. - level

    if noise_shape is None:
        random_tensor = rng.binomial(x.shape, p=retain_prob, dtype=x.dtype)
    else:
        random_tensor = rng.binomial(noise_shape, p=retain_prob, dtype=x.dtype)
        random_tensor = T.patternbroadcast(random_tensor, [dim == 1 for dim in noise_shape])

    x *= random_tensor
    #x /= retain_prob ## no scale for QRNN
    return x


def _rnn(step_function, inputs, initial_states,
        go_backwards=False, mask=None, constants=None):
    '''Iterates over the time dimension of a tensor.

    # Arguments
        inputs: tensor or list of tensors of temporal data of shape (samples, time, ...)
            (at least 2D).
        step_function:
        initial_states: tensor with shape (samples, ...) (no time dimension),
            containing the initial values for the states used in
            the step function.
        go_backwards: boolean. If True, do the iteration over
            the time dimension in reverse order.
        mask: binary tensor with shape (samples, time),
            with a zero for every element that is masked.
        constants: a list of constant values passed at each step.

    # Returns
        A tuple (last_output, outputs, new_states).
            last_output: the latest output of the rnn, of shape (samples, ...)
            outputs: tensor with shape (samples, time, ...) where each
                entry outputs[s, t] is the output of the step function
                at time t for sample s.
            new_states: list of tensors, latest states returned by
                the step function, of shape (samples, ...).
    '''
    if type(inputs) not in [list, tuple]:
        inputs = [inputs]

    ndims = set([input.ndim for input in inputs])
    assert len(ndims) == 1, "must be the same dim"

    ndim = inputs[-1].ndim
    assert ndim >= 2, 'Input should be at least 3D.'

    axes = [1, 0] + list(range(2, ndim))
    print(axes)
    inputs = [input.dimshuffle(axes) for input in inputs]

    if constants is None:
        constants = []

    if mask is not None:
        assert "mask is not supported yet!"
    else:
        def _step(*inputs):
            output, new_states = step_function(*inputs)
            return [output] + new_states

        results, _ = theano.scan(
            _step,
            sequences=inputs,
            outputs_info=[None] + initial_states,
            non_sequences=constants,
            go_backwards=go_backwards)

        # deal with Theano API inconsistency
        if type(results) is list:
            outputs = results[-1]
            states = results[0:]
        else:
            outputs = results
            states = []

    outputs = T.squeeze(outputs)
    last_output = outputs[-2]

    axes = [1, 0] + list(range(2, outputs.ndim))
    outputs = outputs.dimshuffle(axes)
    states = [T.squeeze(state[-2]) for state in states]
    return last_output, outputs, states


class QRNN(Layer):
    '''Qausi RNN

    # Arguments
        output_dim: dimension of the internal projections and the final output.

    # References
        - [Qausi-recurrent Neural Networks](http://arxiv.org/abs/1611.01576)
    '''
    def __init__(self, output_dim, window_size=2,
                 return_sequences=False, go_backwards=False, stateful=False,
                 unroll=False, subsample_length=1,
                 init='uniform', activation='tanh',
                 W_regularizer=None, b_regularizer=None,
                 W_constraint=None, b_constraint=None, 
                 dropout=0, weights=None,
                 bias=True, input_dim=None, input_length=None,
                 **kwargs):
        self.return_sequences = return_sequences
        self.go_backwards = go_backwards
        self.stateful = stateful
        self.unroll = unroll

        self.output_dim = output_dim
        self.window_size = window_size
        self.subsample = (subsample_length, 1)

        self.bias = bias
        self.init = initializations.get(init)
        self.activation = activations.get(activation)
        self.W_regularizer = regularizers.get(W_regularizer)
        self.b_regularizer = regularizers.get(b_regularizer)

        self.W_constraint = constraints.get(W_constraint)
        self.b_constraint = constraints.get(b_constraint)

        self.dropout = dropout
        if self.dropout:
            self.uses_learning_phase = True
        self.initial_weights = weights

        self.supports_masking = True
        self.input_spec = [InputSpec(ndim=3)]
        self.input_dim = input_dim
        self.input_length = input_length
        if self.input_dim:
            kwargs['input_shape'] = (self.input_length, self.input_dim)
        super(QRNN, self).__init__(**kwargs)

    def build(self, input_shape):
        if self.stateful:
            self.reset_states()
        else:
            # initial states: all-zero tensor of shape (output_dim)
            self.states = [None]

        input_dim = input_shape[2]
        self.input_spec = [InputSpec(shape=input_shape)]
        self.W_shape = (self.window_size, 1, input_dim, self.output_dim)

        self.W_z = self.init(self.W_shape, name='{}_W_z'.format(self.name))
        self.W_f = self.init(self.W_shape, name='{}_W_f'.format(self.name))
        self.W_o = self.init(self.W_shape, name='{}_W_o'.format(self.name))
        self.trainable_weights = [self.W_z, self.W_f, self.W_o]
        self.W = None # TODO: concatenate Ws

        if self.bias:
            self.b_z = K.zeros((self.output_dim,), name='{}_b_z'.format(self.name))
            self.b_f = K.zeros((self.output_dim,), name='{}_b_f'.format(self.name))
            self.b_o = K.zeros((self.output_dim,), name='{}_b_o'.format(self.name))
            self.trainable_weights += [self.b_z, self.b_f, self.b_o]

            self.b = None # TODO: concatenate bs

         # TODO apply regularizers and constraints

        if self.initial_weights is not None:
            self.set_weights(self.initial_weights)
            del self.initial_weights

    def get_output_shape_for(self, input_shape):
        length = input_shape[1]
        if length:
            length = conv_output_length(length + self.window_size - 1,
                                        self.window_size,
                                        'valid',
                                        self.subsample[0])
        if self.return_sequences:
            return (input_shape[0], length, self.output_dim)
        else:
            return (input_shape[0], self.output_dim)

    def compute_mask(self, input, mask):
        if self.return_sequences:
            return mask
        else:
            return None

    def get_initial_states(self, x):
        # build an all-zero tensor of shape (samples, output_dim)
        initial_state = K.zeros_like(x)  # (samples, timesteps, input_dim)
        initial_state = K.sum(initial_state, axis=(1, 2))  # (samples,)
        initial_state = K.expand_dims(initial_state)  # (samples, 1)
        initial_state = K.tile(initial_state, [1, self.output_dim])  # (samples, output_dim)
        initial_states = [initial_state for _ in range(len(self.states))]
        return initial_states

    def reset_states(self):
        assert self.stateful, 'Layer must be stateful.'
        input_shape = self.input_spec[0].shape
        if not input_shape[0]:
            raise Exception('If a RNN is stateful, a complete ' +
                            'input_shape must be provided (including batch size).')
        if hasattr(self, 'states'):
            K.set_value(self.states[0],
                        np.zeros((input_shape[0], self.output_dim)))
        else:
            self.states = [K.zeros((input_shape[0], self.output_dim))]

    def call(self, x, mask=None):
        # input shape: (nb_samples, time (padded with zeros), input_dim)
        # note that the .build() method of subclasses MUST define
        # self.input_spec with a complete input shape.
        input_shape = self.input_spec[0].shape
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(x)
        constants = self.get_constants(x)
        preprocessed_input = self.preprocess_input(x)

        last_output, outputs, states = _rnn(self.step, preprocessed_input,
                                            initial_states,
                                            go_backwards=self.go_backwards,
                                            mask=mask,
                                            constants=constants)
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))

        if self.return_sequences:
            return outputs
        else:
            return last_output

    def preprocess_input(self, x):
        if self.bias:
            weights = zip(self.trainable_weights[0:3], self.trainable_weights[3:])
        else:
            weights = self.trainable_weights

        outputs = []
        if self.window_size > 1:
            x = K.asymmetric_temporal_padding(x, self.window_size-1, 0)
        x = K.expand_dims(x, 2)  # add a dummy dimension
        # z, f, o
        for param in weights:
            if self.bias:
               W, b = param
            else:
               W = param
            output = K.conv2d(x, W, strides=self.subsample,
                              border_mode='valid',
                              dim_ordering='tf')
            output = K.squeeze(output, 2)  # remove the dummy dimension
            if self.bias:
                output += K.reshape(b, (1, 1, self.output_dim))

            outputs.append(output)

        if self.dropout:
            f = outputs[1]
            f = K.in_train_phase(1 - _dropout(1 - K.sigmoid(f), self.dropout), K.sigmoid(f))
            outputs[1] = f

        return outputs

    def step(self, *inputs):
        xs, states = inputs[:3], inputs[3:]
        prev_output = states[0]

        z = self.activation(xs[0])
        f = xs[1] if self.dropout else K.sigmoid(xs[1])
        o = K.sigmoid(xs[2])

        output =  f * prev_output + (1 - f) * z
        output = o * output
        output = z + f + o

        return output, [output]

    def get_constants(self, x):
        constants = []
        return constants

    def get_config(self):
        config = {'output_dim': self.output_dim,
                  'init': self.init.__name__,
                  'window_size': self.window_size,
                  'subsample_length': self.subsample[0],
                  'activation': self.activation.__name__,
                  'W_regularizer': self.W_regularizer.get_config() if self.W_regularizer else None,
                  'b_regularizer': self.b_regularizer.get_config() if self.b_regularizer else None,
                  'W_constraint': self.W_constraint.get_config() if self.W_constraint else None,
                  'b_constraint': self.b_constraint.get_config() if self.b_constraint else None,
                  'bias': self.bias,
                  'input_dim': self.input_dim,
                  'input_length': self.input_length}
        base_config = super(QRNN, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))