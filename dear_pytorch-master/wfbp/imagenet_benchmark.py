from __future__ import print_function

import argparse
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data.distributed
from torchvision import models
import os
os.environ['HOROVOD_FUSION_THRESHOLD'] = '0'
os.environ['HOROVOD_CACHE_CAPACITY'] = '0'
os.environ['HOROVOD_CYCLE_TIME'] = '0'
#import hv_distributed_optimizer as hvd
import dopt as hvd

from compression import compressors
import timeit
import numpy as np
from mpi4py import MPI
from profiling import benchmark
comm = MPI.COMM_WORLD

# Benchmark settings
parser = argparse.ArgumentParser(description='PyTorch Synthetic Benchmark',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--fp16', action='store_true', default=False,
                    help='use fp16 compression during allreduce')

parser.add_argument('--model', type=str, default='resnet50',
                    help='model to benchmark')
parser.add_argument('--batch-size', type=int, default=32,
                    help='input batch size')

parser.add_argument('--num-warmup-batches', type=int, default=10,
                    help='number of warm-up batches that don\'t count towards benchmark')
parser.add_argument('--num-batches-per-iter', type=int, default=10,
                    help='number of batches per benchmark iteration')
parser.add_argument('--num-iters', type=int, default=5,
                    help='number of benchmark iterations')

parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disables CUDA training')

parser.add_argument('--use-adasum', action='store_true', default=False,
                    help='use adasum algorithm to do reduction')

parser.add_argument('--mgwfbp', action='store_true', default=False, help='Use MG-WFBP')
parser.add_argument('--asc', action='store_true', default=False, help='Use MG-WFBP')
parser.add_argument('--nstreams', type=int, default=1, help='Number of communication streams')
parser.add_argument('--threshold', type=int, default=-1, help='Set threshold if mgwfbp is False')
parser.add_argument('--rdma', action='store_true', default=False, help='Use RDMA')
parser.add_argument('--compressor', type=str, default='none', choices=compressors.keys(), help='Specify the compressors if density < 1.0')
parser.add_argument('--density', type=float, default=1, help='Density for sparsification')

args = parser.parse_args()
args.cuda = not args.no_cuda and torch.cuda.is_available()

os.environ['HOROVOD_NUM_NCCL_STREAMS'] = str(args.nstreams)


if args.cuda:
    # Horovod: pin GPU to local rank.
    torch.cuda.set_device(hvd.rank()%4)

hvd.init()

cudnn.benchmark = True

# Set up standard model.
if args.model == 'inceptionv4':
    from inceptionv4 import inceptionv4
    model = inceptionv4()
else:
    model = getattr(models, args.model)()

# By default, Adasum doesn't need scaling up learning rate.
lr_scaler = hvd.size() if not args.use_adasum else 1

if args.cuda:
    # Move model to GPU.
    model.cuda()
    # If using GPU Adasum allreduce, scale learning rate by local_size.
    if args.use_adasum and hvd.nccl_built():
        lr_scaler = hvd.local_size()

optimizer = optim.SGD(model.parameters(), lr=0.01 * lr_scaler)

# Set up fixed fake data
image_size = 224
if args.model == 'inception_v3':
    image_size = 227
data = torch.randn(args.batch_size, 3, image_size, image_size)
target = torch.LongTensor(args.batch_size).random_() % 1000
if args.cuda:
    data, target = data.cuda(), target.cuda()



if args.mgwfbp:
    seq_layernames, layerwise_times, _ = benchmark(model, (data, target), F.cross_entropy, task='imagenet')
    layerwise_times = comm.bcast(layerwise_times, root=0)
else:
    seq_layernames, layerwise_times = None, None

if hvd.size() > 1:
    optimizer = hvd.DistributedOptimizer(optimizer, named_parameters=model.named_parameters(), compression=compressors[args.compressor](), is_sparse=args.density<1, density=args.density, seq_layernames=seq_layernames, layerwise_times=layerwise_times, norm_clip=None, threshold=args.threshold, writer=None, gradient_path='./', momentum_correction=False, fp16=args.fp16, mgwfbp=args.mgwfbp, rdma=args.rdma, multi_job_scheduling=False)

    # Horovod: broadcast parameters & optimizer state.
    hvd.broadcast_parameters(model.state_dict(), root_rank=0)
    #hvd.broadcast_optimizer_state(optimizer, root_rank=0)



def benchmark_step():
    optimizer.zero_grad()
    output = model(data)
    loss = F.cross_entropy(output, target)
    loss.backward()
    optimizer.step()
    torch.cuda.synchronize()


def log(s, nl=True):
    if hvd.rank() != 0:
        return
    print(s, end='\n' if nl else '')


log('Model: %s' % args.model)
log('Batch size: %d' % args.batch_size)
device = 'GPU' if args.cuda else 'CPU'
log('Number of %ss: %d' % (device, hvd.size()))

# Warm-up
log('Running warmup...')
timeit.timeit(benchmark_step, number=args.num_warmup_batches)

# Benchmark
log('Running benchmark...')
img_secs = []
for x in range(args.num_iters):
    time = timeit.timeit(benchmark_step, number=args.num_batches_per_iter)
    img_sec = args.batch_size * args.num_batches_per_iter / time
    log('Iter #%d: %.1f img/sec per %s' % (x, img_sec, device))
    img_secs.append(img_sec)

# Results
img_sec_mean = np.mean(img_secs)
img_sec_conf = 1.96 * np.std(img_secs)
log('Img/sec per %s: %.1f +-%.1f' % (device, img_sec_mean, img_sec_conf))
log('Total img/sec on %d %s(s): %.1f +-%.1f' %
    (hvd.size(), device, hvd.size() * img_sec_mean, hvd.size() * img_sec_conf))
