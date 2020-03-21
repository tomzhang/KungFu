#!/usr/bin/env python3
"""
Implemented based on:
https://github.com/uber/horovod/blob/master/examples/tensorflow_synthetic_benchmark.py
"""

from __future__ import absolute_import, division, print_function

import argparse
import os
import timeit
import time
import json
import logging

import numpy as np
import tensorflow as tf
from tensorflow.keras import applications
from tensorflow.python.util import deprecation
from kungfu.tensorflow.ops import all_reduce, resize_cluster_from_url, current_rank, current_cluster_size, save_variable
from kungfu._utils import measure

deprecation._PRINT_DEPRECATION_WARNINGS = False

# Benchmark settings
parser = argparse.ArgumentParser(
    description='TensorFlow Synthetic Benchmark',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--log-file-path',
                    type=str,
                    default="./training_output/throughput.json",
                    help='path to throughput log file')
parser.add_argument('--model',
                    type=str,
                    default='ResNet50',
                    help='model to benchmark')
parser.add_argument('--batch-size',
                    type=int,
                    default=32,
                    help='input batch size')
parser.add_argument(
    '--num-warmup-batches',
    type=int,
    default=10,
    help='number of warm-up batches that don\'t count towards benchmark')
parser.add_argument('--num-batches-per-iter',
                    type=int,
                    default=1,
                    help='number of batches per benchmark iteration')
parser.add_argument('--num-iters',
                    type=int,
                    default=10,
                    help='number of benchmark iterations')
parser.add_argument('--eager',
                    action='store_true',
                    default=False,
                    help='enables eager execution')
parser.add_argument('--no-cuda',
                    action='store_true',
                    default=False,
                    help='disables CUDA training')
parser.add_argument('--kf-optimizer',
                    type=str,
                    default='sync-sgd',
                    help='KungFu optimizers')
parser.add_argument('--optimizer',
                    type=str,
                    default='sgd',
                    help='Optimizer: sgd, adam')
parser.add_argument('--fuse',
                    action='store_true',
                    default=False,
                    help='Fuse KungFu operations')

args = parser.parse_args()
args.cuda = not args.no_cuda

logging.basicConfig(filename=os.path.join(args.log_file_path, "training.log"), level=logging.DEBUG)

config = tf.ConfigProto()
if args.cuda:
    config.gpu_options.allow_growth = True
    from kungfu.ext import _get_cuda_index
    config.gpu_options.visible_device_list = str(_get_cuda_index())
else:
    config.gpu_options.allow_growth = False
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    config.gpu_options.visible_device_list = ''

if args.eager:
    tf.enable_eager_execution(config)

# Set up standard model.
model = getattr(applications, args.model)(weights=None)

opt = None
learning_rate = 0.01
if args.optimizer == 'sgd':
    opt = tf.train.GradientDescentOptimizer(learning_rate)
elif args.optimizer == 'adam':
    opt = tf.train.AdamOptimizer(learning_rate)
else:
    raise Exception('Unknown optimizer option')

barrier_op = None

if args.kf_optimizer:
    from kungfu.tensorflow.ops import barrier
    barrier_op = barrier()
    if args.kf_optimizer == 'sync-sgd':
        from kungfu.tensorflow.optimizers import SynchronousSGDOptimizer
        opt = SynchronousSGDOptimizer(opt)
    elif args.kf_optimizer == 'sync-sgd-nccl':
        from kungfu.tensorflow.optimizers import SynchronousSGDOptimizer
        opt = SynchronousSGDOptimizer(opt, nccl=True, nccl_fusion=args.fuse)
    elif args.kf_optimizer == 'async-sgd':
        from kungfu.tensorflow.optimizers import PairAveragingOptimizer
        opt = PairAveragingOptimizer(opt, fuse_requests=args.fuse)
    elif args.kf_optimizer == 'sma':
        from kungfu.tensorflow.optimizers import SynchronousAveragingOptimizer
        opt = SynchronousAveragingOptimizer(opt)
    else:
        raise Exception('Unknown kungfu option')

data = tf.random_uniform([args.batch_size, 224, 224, 3])
target = tf.random_uniform([args.batch_size, 1],
                           minval=0,
                           maxval=999,
                           dtype=tf.int64)


def loss_function():
    logits = model(data, training=True)
    return tf.losses.sparse_softmax_cross_entropy(target, logits)


def log(s, nl=True):
    #if current_rank() != 0:
    #    return
    print(s, end='\n' if nl else '')


log('Model: %s' % args.model)
log('Batch size: %d' % args.batch_size)
device = '/gpu:0' if args.cuda else 'CPU'


def log_to_file(num_workers, images_second):
    result = dict()
    result["num_workers"] = num_workers
    result["images_second"] = images_second
    result["timestamp"] = time.time()
    
    with open(os.path.join(args.log_file_path, "throughput.json"), "a") as file:
        file.write(json.dumps(result) + "\n")

def log_adaptation_duration_to_file(num_workers, duration):
    line = str(num_workers) + "," + str(duration)
    
    with open(os.path.join(args.log_file_path, "adaptation_duration.txt"), "a") as file:
        file.write(line + "\n")


def log_detailed_result(value, error, attrs):
    attr_str = json.dumps(attrs, separators=(',', ':'))
    print('RESULT: %f +-%f %s' % (value, error, attr_str))  # grep RESULT *.log


def log_final_result(value, error):
    if current_rank() != 0:
        return
    attrs = {
        'np': current_cluster_size(),
        'strategy': os.getenv('KUNGFU_ALLREDUCE_STRATEGY'),
        'bs': args.batch_size,
        'model': args.model,
        'kf-opt': args.kf_optimizer,
    }
    log_detailed_result(value, error, attrs)


def run(sess, train_op, bcast_op):
    if args.num_batches_per_iter > 1:
        print('--num-batches-per-iter == 1 is highly recommended, using %d' %
              (args.num_batches_per_iter))
    step_place = tf.placeholder(dtype=tf.int32, shape=())
    sync_step_op = all_reduce(step_place, op='max')
    resize_op = resize_cluster_from_url()
    init_save_op = tf.group([save_variable(v) for v in tf.trainable_variables()])
    logging.debug("before init_save_op")
    sess.run(init_save_op)
    logging.debug("after init_save_op")
    # Benchmark
    log('Running benchmark...')
    img_secs = []
    need_sync = True
    step = 0
    adaptation_start = time.time()
    while step < args.num_iters:
        if need_sync:
            logging.debug("before sync_step_op")
            new_step = sess.run(sync_step_op, feed_dict={step_place: step})
            logging.debug("after sync_step_op")
            if new_step != step:
                print('sync step : %d -> %d' % (step, new_step))
            step = new_step
            if bcast_op:
                logging.debug("before bcast_op")
                duration, _ = measure(lambda: sess.run(bcast_op))
                logging.debug("after bcast_op")
                log('bcast_op took %.3fs' % (duration))
            need_sync = False
            adaptation_duration = time.time() - adaptation_start
            log_adaptation_duration_to_file(current_cluster_size(), adaptation_duration)
        step += 1
        logging.debug("before train_op")
        time = timeit.timeit(lambda: sess.run(train_op),
                             number=args.num_batches_per_iter)
        logging.debug("after train_op")
        img_sec = args.batch_size / time
        log('Iter #%d: %.1f img/sec per %s' % (step, img_sec, device))
        log_to_file(current_cluster_size(), img_sec)
        img_secs.append(img_sec)

        changed, keep = sess.run(resize_op)
        if not keep:
            return
        if changed:
            adaptation_start = time.time()
            need_sync = True

    img_sec_mean = np.mean(img_secs)
    img_sec_conf = 1.96 * np.std(img_secs)
    log('Img/sec per %s: %.1f +-%.1f' % (device, img_sec_mean, img_sec_conf))
    log_final_result(img_sec_mean, img_sec_conf)


loss = loss_function()
train_op = opt.minimize(loss)

bcast_op = None
if args.kf_optimizer:
    from kungfu.tensorflow.initializer import BroadcastGlobalVariablesOp
    bcast_op = BroadcastGlobalVariablesOp()
init = tf.global_variables_initializer()
with tf.Session(config=config) as session:
    duration, _ = measure(lambda: session.run(init))
    log('init took %.3fs' % (duration))
    run(session, train_op, bcast_op)
print('stopped')
