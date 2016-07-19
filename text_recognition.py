import tensorflow as tf
import numpy as np
import utils
from utils import logger
from cnn import CNN
import h5py
import math
import os
import time
import json
import sys

class Config():
  def __init__(self):
    with open('config.json', 'r') as json_file:
      json_data = json.load(json_file)
      self.dataset_dir = json_data['dataset_dir']
      self.height = json_data['height']
      self.window_size = json_data['window_size']
      self.depth = json_data['depth']
      self.embed_size = json_data['embed_size']
      self.stride = json_data['stride']
      self.lr = json_data['lr']
      self.keep_prob = json_data['keep_prob']
      self.lstm_size = json_data['lstm_size']
      self.num_epochs = json_data['num_epochs']
      self.batch_size = json_data['batch_size']
      self.debug = json_data['debug']
      self.debug_size = json_data['debug_size']
      self.full_load_cnn_ckpt = json_data['full_load_cnn_ckpt']
      self.full_load_ckpt = json_data['full_load_ckpt']
      self.ckpt_dir = json_data['ckpt_dir']
      self.save_every_n_steps = json_data['save_every_n_steps']
      self.test_only = json_data['test_only']
      self.test_every_n_steps = json_data['test_every_n_steps']
      self.test_size = json_data['test_size']
      self.test_size = self.test_size-self.test_size%self.batch_size
      self.gpu = json_data['gpu']

class DTRN_Model():
  def __init__(self, config):
    self.config = config
    self.load_data(self.config.debug, self.config.test_only)
    self.add_placeholders()
    self.rnn_outputs = self.add_model()
    self.outputs = self.add_projection(self.rnn_outputs)
    self.loss = self.add_loss_op(self.outputs)
    self.pred, self.groundtruth = self.add_decoder(self.outputs)
    self.train_op = self.add_training_op(self.loss)

  def load_data(self, debug=False, test_only=False):
    filename_test = os.path.join(self.config.dataset_dir, 'test.hdf5')
    f_test = h5py.File(filename_test, 'r')
    self.imgs_test = np.array(f_test.get('imgs'), dtype=np.uint8)
    self.words_embed_test = f_test.get('words_embed')[()].tolist()
    self.time_test = np.array(f_test.get('time'), dtype=np.uint8)
    logger.info('loading test data (%d examples)', self.imgs_test.shape[0])
    f_test.close()

    if self.imgs_test.shape[0] > self.config.test_size:
      self.imgs_test = self.imgs_test[:self.config.test_size]
      self.words_embed_test = self.words_embed_test[:self.config.test_size]
      self.time_test = self.time_test[:self.config.test_size]

    if test_only:
      self.max_time = np.amax(self.time_test)
      self.imgs_test = self.imgs_test[:, :self.max_time]
      return

    filename_train = os.path.join(self.config.dataset_dir, 'train.hdf5')
    f_train = h5py.File(filename_train, 'r')
    self.imgs_train = np.array(f_train.get('imgs'), dtype=np.uint8)
    self.words_embed_train = f_train.get('words_embed')[()].tolist()
    self.time_train = np.array(f_train.get('time'), dtype=np.uint8)
    logger.info('loading training data (%d examples)', self.imgs_train.shape[0])
    f_train.close()

    if self.config.debug:
      self.imgs_train = self.imgs_train[:self.config.debug_size]
      self.words_embed_train = self.words_embed_train[:self.config.debug_size]
      self.time_train = self.time_train[:self.config.debug_size]

    self.max_time = max(np.amax(self.time_train), np.amax(self.time_test))
    self.imgs_train = self.imgs_train[:, :self.max_time]
    self.imgs_test = self.imgs_test[:, :self.max_time]

  def add_placeholders(self):
    # batch_size x max_time x height x width x depth
    self.inputs_placeholder = tf.placeholder(tf.float32,
        shape=[self.config.batch_size, self.max_time, self.config.height,
        self.config.window_size, self.config.depth])

    # batch_size x max_time x embed_size (63)
    self.labels_placeholder = tf.sparse_placeholder(tf.int32)

    # batch_size
    self.sequence_length_placeholder = tf.placeholder(tf.int32,
        shape=[self.config.batch_size])

    # max_time x batch_size x embed_size
    self.outputs_mask_placeholder = tf.placeholder(tf.float32,
        shape=[self.max_time, self.config.batch_size, self.config.embed_size])

    # float
    self.keep_prob_placeholder = tf.placeholder(tf.float32)

  def add_model(self):
    self.cell = tf.nn.rnn_cell.LSTMCell(self.config.lstm_size,
        state_is_tuple=True)

    with tf.variable_scope('CNN_LSTM') as scope:
      # inputs_placeholder: batch_size x max_time x height x window_size x depth
      # data_cnn: batch_size*max_time x height x window_size x depth
      data_cnn = tf.reshape(self.inputs_placeholder,
          [self.max_time*self.config.batch_size, self.config.height,
          self.config.window_size, self.config.depth])

      # img_features: batch_size*max_time x feature_size (128)
      img_features, self.saver = CNN(data_cnn, self.config.depth, self.config.embed_size, self.keep_prob_placeholder)

      # data_encoder: batch_size x max_time x feature_size
      data_encoder = tf.reshape(img_features, (self.config.batch_size,
          self.max_time, -1))

      # rnn_outputs: max_time x batch_size x lstm_size
      rnn_outputs, _ = tf.nn.dynamic_rnn(self.cell,
          data_encoder, sequence_length=self.sequence_length_placeholder,
          dtype=tf.float32, time_major=False)

    return rnn_outputs

  def add_projection(self, rnn_outputs):
    with tf.variable_scope('Projection'):
      W = tf.get_variable('Weight', [self.config.lstm_size,
          self.config.embed_size],
          initializer=tf.contrib.layers.xavier_initializer())
      b = tf.get_variable('Bias', [self.config.embed_size],
          initializer=tf.constant_initializer(0.0))

      rnn_outputs_reshape = tf.reshape(rnn_outputs,
          (self.config.batch_size*self.max_time, self.config.lstm_size))
      outputs = tf.matmul(rnn_outputs_reshape, W)+b
      outputs = tf.nn.log_softmax(outputs)
      outputs = tf.reshape(outputs, (self.config.batch_size, self.max_time, -1))
      outputs = tf.transpose(outputs, perm=[1, 0, 2])
      outputs = tf.add(outputs, self.outputs_mask_placeholder)

      # outputs: max_time x batch_size x embed_size
    return outputs

  def add_loss_op(self, outputs):
    loss = tf.contrib.ctc.ctc_loss(outputs, self.labels_placeholder,
        self.sequence_length_placeholder)
    loss = tf.reduce_mean(loss)

    return loss

  def add_decoder(self, outputs):
    decoded, _ = tf.contrib.ctc.ctc_beam_search_decoder(outputs,
        self.sequence_length_placeholder, merge_repeated=False)
    pred = tf.sparse_tensor_to_dense(decoded[0])
    groundtruth = tf.sparse_tensor_to_dense(self.labels_placeholder)

    return (pred, groundtruth)

  def add_training_op(self, loss):
    optimizer = tf.train.AdamOptimizer(self.config.lr)
    train_op = optimizer.minimize(loss)
    return train_op

def main():
  config = Config()
  model = DTRN_Model(config)
  init = tf.initialize_all_variables()

  if not os.path.exists(model.config.ckpt_dir):
    os.makedirs(model.config.ckpt_dir)

  with tf.Session() as session:
    session.run(init)

    # restore previous session
    if model.config.full_load_cnn_ckpt or model.config.test_only:
      model.saver.restore(session, model.config.ckpt_dir+'model_cnn.ckpt')
      logger.info('cnn model restored')
    elif model.config.full_load_ckpt or model.config.test_only:
      model.saver = tf.train.Saver()
      model.saver.restore(session, model.config.ckpt_dir+'model_full.ckpt')
      logger.info('full model restored')

    iterator_train = utils.data_iterator(
        model.imgs_train, model.words_embed_train, model.time_train,
        model.config.num_epochs, model.config.batch_size, model.max_time,
        model.config.embed_size)

    num_examples = model.imgs_train.shape[0]
    num_steps = int(math.ceil(
        num_examples*model.config.num_epochs/model.config.batch_size))

    losses_train = []
    cur_epoch = 0
    step_epoch = 0
    for step, (inputs_train, labels_sparse_train, sequence_length_train,
        outputs_mask_train, epoch_train) in enumerate(iterator_train):

      # test
      if step%model.config.test_every_n_steps == 0:
        losses_test = []
        iterator_test = utils.data_iterator(
          model.imgs_test, model.words_embed_test, model.time_test,
          1, model.config.batch_size, model.max_time, model.config.embed_size)

        for step_test, (inputs_test, labels_sparse_test, sequence_length_test,
            outputs_mask_test, epoch_test) in enumerate(iterator_test):
          feed_test = {model.inputs_placeholder: inputs_test,
                       model.labels_placeholder: labels_sparse_test,
                       model.sequence_length_placeholder: sequence_length_test,
                       model.outputs_mask_placeholder: outputs_mask_test,
                       model.keep_prob_placeholder: 1.0}

          ret_test = session.run([model.loss, model.pred, model.groundtruth],
              feed_dict=feed_test)
          losses_test.append(ret_test[0])

        logger.info('<-------------------->')
        logger.info('average test loss: %f (#batches = %d)',
            np.mean(losses_test), len(losses_test))
        logger.info(ret_test[1][:5])
        logger.info(ret_test[2][:5])
        logger.info('<-------------------->')

        if model.config.test_only:
          return

      # new epoch, calculate average loss from last epoch
      if epoch_train != cur_epoch:
        logger.info('average training loss in epoch %d: %f', cur_epoch,
            np.mean(losses_train[step_epoch:]))
        #logger.info('average loss overall: %f', np.mean(losses_train))
        step_epoch = step
        cur_epoch = epoch_train

      feed_train = {model.inputs_placeholder: inputs_train,
                    model.labels_placeholder: labels_sparse_train,
                    model.sequence_length_placeholder: sequence_length_train,
                    model.outputs_mask_placeholder: outputs_mask_train,
                    model.keep_prob_placeholder: model.config.keep_prob}

      ret_train = session.run([model.train_op, model.loss],
          feed_dict=feed_train)
      losses_train.append(ret_train[1])
      # logger.info('epoch %d, step %d: training loss = %f', epoch_train, step,
      # ret_train[1])

      if step%model.config.save_every_n_steps == 0:
        save_path = model.saver.save(session, model.config.ckpt_dir+'model_full.ckpt')
        logger.info('full model saved in file: %s', save_path)

if __name__ == '__main__':
  main()
