# Copyright 2019 The Magenta Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Train the model."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import sys
import argparse

from magenta.models.coconet import lib_data
from magenta.models.coconet import lib_graph
from magenta.models.coconet import lib_hparams
from magenta.models.coconet import lib_util
import numpy as np
import six
from six.moves import range
from six.moves import zip
import tensorflow as tf

sys.path.append("/Users/chrisolen/Documents/uchicago_courses/deep_learning_and_image_recognition/audio_generation/magenta")

parser = argparse.ArgumentParser()

parser.add_argument('--data_dir', default=None, type=str,
                    help='Path to the base directory for different datasets.') 

parser.add_argument('--logdir', default=None, type=str,
                    help='Path to the directory where checkpoints and \
                    summary events will be saved during training and \
                    evaluation. Multiple runs can be stored within the \
                    parent directory of `logdir`. Point TensorBoard \
                    to the parent directory of `logdir` to see all \
                    your runs.') 

parser.add_argument('--log_progress', default=True, type=bool,
                  help='If False, do not log any checkpoints and summary\
                  statistics.') 

# Dataset.
parser.add_argument('--dataset', default=None, type=str,
                    help='Choices: Jsb16thSeparated, MuseData, Nottingham,\
                    PianoMidiDe') 

parser.add_argument('--quantization_level', default=0.125, type=float,
                   help='Quantization duration.\
                   For qpm=120, notated quarter note equals 0.5.') 

parser.add_argument('--num_instruments', default=4, type=int,
                     help='Maximum number of instruments that appear in this\
                     dataset.  Use 0 if not separating instruments and\
                     hence does not matter how many there are.') 

parser.add_argument('--separate_instruments', default=True, type=bool,
                  help='Separate instruments into different input feature\
                  maps or not.') 

parser.add_argument('--crop_piece_len', default=64, type=int,
                    help='The number of time steps\
                     included in a crop') 

# Model architecture.
parser.add_argument('--architecture', default='straight', type=str,
                    help='Convnet style. Choices: straight') 

# Hparams for depthwise separable conv.
parser.add_argument('--use_sep_conv', default=False, type=bool, help='Use depthwise separable\
                  convolutions.') 

parser.add_argument('--sep_conv_depth_multiplier', default=1, type=int, help='Depth multiplier for\
                     depthwise separable convs.') 

parser.add_argument('--num_initial_regular_conv_layers', default=2, type=int, help='The number of\
                     regular convolutional layers to start with when using\
                     depthwise separable convolutional layers.') 

# Hparams for reducing pointwise in separable convs.
parser.add_argument('--num_pointwise_splits', default=1, type=int, help='Num of splits on the\
                     pointwise convolution stage in depthwise separable\
                     convolutions.') 

parser.add_argument('--interleave_split_every_n_layers', default=1, type=int, help='Num of split\
                     pointwise layers to interleave between full pointwise\
                     layers.') 

# Hparams for dilated conv.
parser.add_argument('--num_dilation_blocks', default=3, type=int, help='The number dilation blocks\
                     that starts from dilation rate=1.') 

parser.add_argument('--dilate_time_only', default=False, type=bool, help='If set, only dilates the time\
                  dimension and not pitch.') 

parser.add_argument('--repeat_last_dilation_level', default=False, type=bool, help='If set, repeats the\
                  last dilation rate.')

parser.add_argument('--num_layers', default=64, type=int, help='The number of convolutional layers\
                     for architectures that do not use dilated convs.') 

parser.add_argument('--num_filters', default=128, type=int,
                     help='The number of filters for each convolutional\
                     layer.') 

parser.add_argument('--use_residual', default=True, type=bool, help='Add residual connections or not.') 

parser.add_argument('--batch_size', default=20, type=int,
                     help='The batch size for training and validating the model.') 

# Mask related.
parser.add_argument('--maskout_method', default='orderless', type=str,
                    help="The choices include: 'bernoulli'\
                    and 'orderless' (which \
                    invokes gradient rescaling as per NADE).") 

parser.add_argument('--mask_indicates_context', default=True, type=bool, help='Feed inverted mask into convnet so that zero-padding makes sense.') 

parser.add_argument('--optimize_mask_only', default=False, type=bool,help='Optimize masked predictions only.') 

parser.add_argument('--rescale_loss', default=True, type=bool, help='Rescale loss based on context size.') 

parser.add_argument('--patience', default=5, type=int, help='Number of epochs to wait for improvement before decaying learning rate.') 

parser.add_argument('--corrupt_ratio', default=0.5, type=float, help='Fraction of variables to mask out.') 

# Run parameters.
parser.add_argument('--num_epochs', default=0, type=int,
                     help='The number of epochs to train the model. Default\
                     is 0, which means to run until terminated\
                     manually.')

parser.add_argument('--save_model_secs', default=360, type=int,
                     help='The number of seconds between saving each\
                     checkpoint.') 

parser.add_argument('--eval_freq', default=5, type=int,
                     help='The number of training iterations before validation.') 

parser.add_argument('--run_id', default='', type=str, help='A run_id to add to directory names to avoid accidentally overwriting when\
                    testing same setups.') 

args = parser.parse_args()


def estimate_popstats(unused_sv, sess, m, dataset, unused_hparams):
  """Averages over mini batches for population statistics for batch norm."""
  print('Estimating population statistics...')
  tfbatchstats, tfpopstats = list(zip(*list(m.popstats_by_batchstat.items())))

  nepochs = 3
  nppopstats = [lib_util.AggregateMean('') for _ in tfpopstats]
  for _ in range(nepochs):
    batches = (
        dataset.get_featuremaps().batches(size=m.batch_size, shuffle=True))
    for unused_step, batch in enumerate(batches):
      feed_dict = batch.get_feed_dict(m.placeholders)
      npbatchstats = sess.run(tfbatchstats, feed_dict=feed_dict)
      for nppopstat, npbatchstat in zip(nppopstats, npbatchstats):
        nppopstat.add(npbatchstat)
  nppopstats = [nppopstat.mean for nppopstat in nppopstats]

  _print_popstat_info(tfpopstats, nppopstats)

  # Update tfpopstat variables.
  for unused_j, (tfpopstat, nppopstat) in enumerate(
      zip(tfpopstats, nppopstats)):
    tfpopstat.load(nppopstat)


def run_epoch(supervisor, sess, m, dataset, hparams, eval_op, experiment_type,
              epoch_count):
  """Runs an epoch of training or evaluate the model on given data."""
  # reduce variance in validation loss by fixing the seed
  data_seed = 123 if experiment_type == 'valid' else None
  with lib_util.numpy_seed(data_seed):
    batches = (
        dataset.get_featuremaps().batches(
            size=m.batch_size, shuffle=True, shuffle_rng=data_seed))

  losses = lib_util.AggregateMean('losses')
  losses_total = lib_util.AggregateMean('losses_total')
  losses_mask = lib_util.AggregateMean('losses_mask')
  losses_unmask = lib_util.AggregateMean('losses_unmask')

  start_time = time.time()
  for unused_step, batch in enumerate(batches):
    # Evaluate the graph and run back propagation.
    fetches = [
        m.loss, m.loss_total, m.loss_mask, m.loss_unmask, m.reduced_mask_size,
        m.reduced_unmask_size, m.learning_rate, eval_op
    ]
    feed_dict = batch.get_feed_dict(m.placeholders)
    (loss, loss_total, loss_mask, loss_unmask, reduced_mask_size,
     reduced_unmask_size, learning_rate, _) = sess.run(
         fetches, feed_dict=feed_dict)

    # Aggregate performances.
    losses_total.add(loss_total, 1)
    # Multiply the mean loss_mask by reduced_mask_size for aggregation as the
    # mask size could be different for every batch.
    losses_mask.add(loss_mask * reduced_mask_size, reduced_mask_size)
    losses_unmask.add(loss_unmask * reduced_unmask_size, reduced_unmask_size)

    if hparams.optimize_mask_only:
      losses.add(loss * reduced_mask_size, reduced_mask_size)
    else:
      losses.add(loss, 1)

  # Collect run statistics.
  run_stats = dict()
  run_stats['loss_mask'] = losses_mask.mean
  run_stats['loss_unmask'] = losses_unmask.mean
  run_stats['loss_total'] = losses_total.mean
  run_stats['loss'] = losses.mean
  if experiment_type == 'train':
    run_stats['learning_rate'] = float(learning_rate)

  # Make summaries.
  if args.log_progress:
    summaries = tf.Summary()
    for stat_name, stat in six.iteritems(run_stats):
      value = summaries.value.add()
      value.tag = '%s_%s' % (stat_name, experiment_type)
      value.simple_value = stat
    supervisor.summary_computed(sess, summaries, epoch_count)

  tf.logging.info(
      '%s, epoch %d: loss (mask): %.4f, loss (unmask): %.4f, '
      'loss (total): %.4f, log lr: %.4f, time taken: %.4f',
      experiment_type, epoch_count, run_stats['loss_mask'],
      run_stats['loss_unmask'], run_stats['loss_total'],
      np.log(run_stats['learning_rate']) if 'learning_rate' in run_stats else 0,
      time.time() - start_time)

  return run_stats['loss']


def main(unused_argv):
  """Builds the graph and then runs training and validation."""
  print('TensorFlow version:', tf.__version__)

  tf.logging.set_verbosity(tf.logging.INFO)

  if args.data_dir is None:
    tf.logging.fatal('No input directory was provided.')

  print(args.maskout_method, 'separate', args.separate_instruments)

  hparams = _hparams_from_flags()

  # Get data.
  print('dataset:', args.dataset, args.data_dir)
  print('current dir:', os.path.curdir)
  train_data = lib_data.get_dataset(args.data_dir, hparams, 'train')
  valid_data = lib_data.get_dataset(args.data_dir, hparams, 'valid')
  print('# of train_data:', train_data.num_examples)
  print('# of valid_data:', valid_data.num_examples)
  if train_data.num_examples < hparams.batch_size:
    print('reducing batch_size to %i' % train_data.num_examples)
    hparams.batch_size = train_data.num_examples

  train_data.update_hparams(hparams)

  # Save hparam configs.
  logdir = os.path.join(args.logdir, hparams.log_subdir_str)
  tf.gfile.MakeDirs(logdir)
  config_fpath = os.path.join(logdir, 'config')
  tf.logging.info('Writing to %s', config_fpath)
  with tf.gfile.Open(config_fpath, 'w') as p:
    hparams.dump(p)

  # Build the graph and subsequently running it for train and validation.
  with tf.Graph().as_default():
    no_op = tf.no_op()

    # Build placeholders and training graph, and validation graph with reuse.
    m = lib_graph.build_graph(is_training=True, hparams=hparams)
    tf.get_variable_scope().reuse_variables()
    mvalid = lib_graph.build_graph(is_training=False, hparams=hparams)

    tracker = Tracker(
        label='validation loss',
        patience=args.patience,
        decay_op=m.decay_op,
        save_path=os.path.join(args.logdir, hparams.log_subdir_str,
                               'best_model.ckpt'))

    # Graph will be finalized after instantiating supervisor.
    sv = tf.train.Supervisor(
        logdir=logdir,
        saver=tf.train.Supervisor.USE_DEFAULT if args.log_progress else None,
        summary_op=None,
        save_model_secs=args.save_model_secs)
    with sv.PrepareSession() as sess:
      epoch_count = 0
      while epoch_count < args.num_epochs or not args.num_epochs:
        if sv.should_stop():
          break

        # Run training.
        run_epoch(sv, sess, m, train_data, hparams, m.train_op, 'train',
                  epoch_count)

        # Run validation.
        if epoch_count % hparams.eval_freq == 0:
          estimate_popstats(sv, sess, m, train_data, hparams)
          loss = run_epoch(sv, sess, mvalid, valid_data, hparams, no_op,
                           'valid', epoch_count)
          tracker(loss, sess)
          if tracker.should_stop():
            break

        epoch_count += 1

    print('best', tracker.label, tracker.best)
    print('Done.')
    return tracker.best


class Tracker(object):
  """Tracks the progress of training and checks if training should stop."""

  def __init__(self, label, save_path, sign=-1, patience=5, decay_op=None):
    self.label = label
    self.sign = sign
    self.best = np.inf
    self.saver = tf.train.Saver()
    self.save_path = save_path
    self.patience = patience
    # NOTE: age is reset with decay, but true_age is not
    self.age = 0
    self.true_age = 0
    self.decay_op = decay_op

  def __call__(self, loss, sess):
    if self.sign * loss > self.sign * self.best:
      if args.log_progress:
        tf.logging.info('Previous best %s: %.4f.', self.label, self.best)
        tf.gfile.MakeDirs(os.path.dirname(self.save_path))
        self.saver.save(sess, self.save_path)
        tf.logging.info('Storing best model so far with loss %.4f at %s.' %
                        (loss, self.save_path))
      self.best = loss
      self.age = 0
      self.true_age = 0
    else:
      self.age += 1
      self.true_age += 1
      if self.age > self.patience:
        sess.run([self.decay_op])
        self.age = 0

  def should_stop(self):
    return self.true_age > 5 * self.patience


def _print_popstat_info(tfpopstats, nppopstats):
  """Prints the average and std of population versus batch statistics."""
  mean_errors = []
  stdev_errors = []
  for j, (tfpopstat, nppopstat) in enumerate(zip(tfpopstats, nppopstats)):
    moving_average = tfpopstat.eval()
    if j % 2 == 0:
      mean_errors.append(abs(moving_average - nppopstat))
    else:
      stdev_errors.append(abs(np.sqrt(moving_average) - np.sqrt(nppopstat)))

  def flatmean(xs):
    return np.mean(np.concatenate([x.flatten() for x in xs]))

  print('average of pop mean/stdev errors: %g %g' % (flatmean(mean_errors),
                                                     flatmean(stdev_errors)))
  print('average of batch mean/stdev: %g %g' %
        (flatmean(nppopstats[0::2]),
         flatmean([np.sqrt(ugh) for ugh in nppopstats[1::2]])))


def _hparams_from_flags():
  """Instantiate hparams based on flags set in FLAGS."""
  keys = ("""
      dataset quantization_level num_instruments separate_instruments
      crop_piece_len architecture use_sep_conv num_initial_regular_conv_layers
      sep_conv_depth_multiplier num_dilation_blocks dilate_time_only
      repeat_last_dilation_level num_layers num_filters use_residual
      batch_size maskout_method mask_indicates_context optimize_mask_only
      rescale_loss patience corrupt_ratio eval_freq run_id
      num_pointwise_splits interleave_split_every_n_layers
      """.split())
  hparams = lib_hparams.Hyperparameters(**dict(
      (key, getattr(args, key)) for key in keys))
  return hparams


#if __name__ == '__main__':
#  tf.app.run()
