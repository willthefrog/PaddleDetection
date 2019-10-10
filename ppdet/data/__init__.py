# Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

from __future__ import absolute_import

import numbers
import os
import random

try:
    from collections.abc import Mapping, Sequence
except Exception:
    from collections import Mapping, Sequence

import numpy as np

from paddle import fluid
from paddle.fluid import framework
from ppdet.core.workspace import register, serializable

from . import datasets
from . import samplers
from . import transforms
from . import dataloader

for m in [datasets, samplers, transforms]:
    for c in getattr(m, '__all__'):
        serializable(register(getattr(m, c)))

type_map = {
    'coco': datasets.COCODataSet,
    'voc': datasets.PascalVocDataSet,
    'folder': datasets.ImageFolder,
}


def register_dataset(name, cls):
    global type_map
    type_map[name] = cls


class ExtractFields(object):
    def __init__(self,
                 feed_vars=[],
                 extra_vars=[], yolo_class_fix=False):

        super(ExtractFields, self).__init__()
        self.feed_vars = feed_vars
        self.extra_vars = extra_vars
        self.yolo_class_fix = yolo_class_fix

        self._normalized_vars = [self._normalize(v) for v in self.feed_vars]
        self._normalized_vars += [
            self._normalize(v, True) for v in self.extra_vars]

    def _normalize(self, var, extra=False):
        if isinstance(var, str):
            name = var
            fields = [var]
            lod_level = 0
        else:
            assert isinstance(var, Mapping), \
                "feed_var should be either string or dict like object"
            name = var['name']
            if 'fields' in var:
                fields = var['fields']
            else:
                fields = [name]
            lod_level = 'lod_level' in var and var['lod_level'] or 0
        return {'name': name,
                'fields': fields,
                'lod_level': lod_level,
                'extra': extra}

    def __call__(self, batch):
        feed_dict = {}
        extra_dict = {}

        for var in self._normalized_vars:
            name = var['name']
            lod_level = var['lod_level']
            fields = var['fields']
            extra = var['extra']

            arr_list = []
            seq_length = None

            for idx, f in enumerate(fields):
                # XXX basically just for `im_shape`
                if isinstance(f, numbers.Number):
                    arr = f
                else:
                    arr = batch[f]
                    if self.yolo_class_fix and f == 'gt_label':
                        arr = [np.clip(a - 1, 0, None) for a in arr]

                if lod_level == 0:
                    # stack only feed vars or combined fields
                    if (not extra or len(fields) > 1) and isinstance(
                            arr, Sequence) and isinstance(arr[0], np.ndarray):
                        arr = np.stack(arr)
                    arr_list.append(arr)
                    continue

                if not extra:
                    flat, seq_length = self._flatten(arr, lod_level + 1)
                    arr_list.append(flat)

            # combine fields
            if len(fields) == 1:
                ndarray = arr_list[0]
            else:
                ndarray = np.column_stack(np.broadcast_arrays(*arr_list))
                if extra:
                    ndarray = [ndarray]

            if extra:
                extra_dict[name] = ndarray
                continue

            if seq_length is not None:
                seq_length = seq_length[1:]

            if not isinstance(ndarray, np.ndarray):
                ndarray = np.asarray(ndarray)
            if ndarray.dtype == np.float64:
                ndarray = ndarray.astype(np.float32)
            if ndarray.dtype == np.int64:
                ndarray = ndarray.astype(np.int32)

            feed_dict[name] = (ndarray, seq_length)

        return feed_dict, extra_dict

    def _flatten(self, arr, lod_level):
        flat = []
        seq_length = [[] for _ in range(lod_level)]

        def _recurse(data, result, level):
            if level == 0:
                flat.append(data)
                return
            result[0].append(len(data))
            for item in data:
                _recurse(item, result[1:], level - 1)

        _recurse(arr, seq_length, lod_level)
        return flat, seq_length


class DataLoaderBuilder(dataloader.DataLoader):
    """
    Constructs the dataloader.

    Args:
        dataset (object): dataset instance or dict.
        sampler (object): sampler instance or dict.
        batch_size (int): batch size.
        sample_transforms (list): list of data transformations to be performed
            on each sample.
        batch_transforms (list):  list of data transformations to be performed
            on each batch, after all samples are collected.
        feed_vars (list):  list of sample fields to be fed to the network
        extra_vars (list): list of sample fields to be used out of the network,
            e.g., for computing evaluation metrics
        num_workers (int): number of dataloader workers.
        multiprocessing (bool): use threading or multiprocessing.
        buffer_size (int): number of batches to buffer.
        pin_memory (bool): prefetch data to CUDA pinned memory.
        prefetch_to_gpu (bool): prefetch data to CUDA device.
    """
    __category__ = 'data'
    __shared__ = ['use_gpu']

    def __init__(self,
                 dataset,
                 sampler=None,
                 batch_size=1,
                 sample_transforms=[],
                 batch_transforms=[],
                 feed_vars=[],
                 extra_vars=[],
                 num_devices=None,
                 num_workers=0,
                 auto_reset=True,
                 multiprocessing=False,
                 buffer_size=2,
                 pin_memory=False,
                 prefetch_to_gpu=False,
                 use_gpu=False,
                 yolo_class_fix=False):
        if isinstance(dataset, dict):
            kwargs = dataset
            cls = kwargs.pop('type')
            dataset = type_map[cls](**kwargs)

        self.feed_vars = feed_vars
        self.auto_reset = auto_reset
        self.yolo_class_fix = yolo_class_fix

        if use_gpu:
            if num_devices is None:
                num_devices = len(framework.cuda_places())
            if prefetch_to_gpu:
                places = framework.cuda_places()[:num_devices]
            elif pin_memory:
                places = [fluid.CUDAPinnedPlace()] * num_devices
            else:
                places = [fluid.CPUPlace()] * num_devices
        else:
            places = framework.cpu_places()
            if num_devices is None:
                num_devices = len(places)
        self.places = places
        self._tensor_dicts = [{} for _ in places]

        init_seed = random.randint(0, 1e5)
        rank = int(os.getenv('PADDLE_TRAINER_ID', 0))
        world_size = int(os.getenv('PADDLE_TRAINERS_NUM', 1))
        if world_size > 1:
            init_seed = 42 * world_size

        if isinstance(sampler, dict):
            kwargs = sampler
            kwargs['rank'] = rank
            if 'world_size' not in kwargs:
                kwargs['world_size'] = world_size
            if 'init_seed' not in kwargs:
                kwargs['init_seed'] = init_seed
            # XXX currently we only have one default sampler
            if 'type' not in kwargs:
                sampler = samplers.Sampler(dataset, batch_size, **kwargs)

        extract = ExtractFields(feed_vars, extra_vars, yolo_class_fix)

        if 'PADDLE_TRAINERS_NUM' not in os.environ:
            buffer_size = min(num_devices * buffer_size, 8)
            num_workers = min(num_devices * num_workers, 8)

        super(DataLoaderBuilder, self).__init__(
            dataset, sampler, sample_transforms, batch_transforms + [extract],
            num_workers, num_devices, multiprocessing, buffer_size)

    def _to_tensor(self, feed_dict, place, out=None):
        if out is None:
            out = {}
        for k, (ndarray, seq_length) in feed_dict.items():
            if k not in out:
                out[k] = fluid.core.LoDTensor()
            out[k].set(ndarray, place)
            if seq_length is not None:
                out[k].set_recursive_sequence_lengths(seq_length)
        return out

    def _merge_seq_length(self, seq_lengths):
        results = seq_lengths[0]
        if results is None:
            return None
        for idx, v in enumerate(results):
            for rest in seq_lengths[1:]:
                v += rest[idx]
        return results

    def __next__(self):
        feed_list = []
        coalesced_extra_dict = {}
        _drained = False
        for place in self.places:
            try:
                feed_dict, extra_dict = next(self._iter)
            except StopIteration:
                if self.auto_reset:
                    self.reset()
                    feed_dict, extra_dict = next(self._iter)
                else:
                    _drained = True
            finally:
                if not _drained:
                    feed_list.append((feed_dict, place))
                    for k, v in extra_dict.items():
                        if k not in coalesced_extra_dict:
                            coalesced_extra_dict[k] = v
                        else:
                            coalesced_extra_dict[k] += v
                else:
                    break

        if _drained and len(feed_list) == 0:
            raise StopIteration

        # XXX merge remaining items into single dict, let executor split it
        if _drained and len(feed_list) != len(self.places):
            # place = self.places[0]
            # if self.places[0] != fluid.CPUPlace():
            #     place = fluid.CUDAPinnedPlace()
            # XXX pinned memory needs patch to paddle, use CPU for now
            place = fluid.CPUPlace()

            if len(feed_list) == 1:
                feed_list = self._to_tensor(feed_list[0][0], place)
            else:
                feed_dicts = [data[0] for data in feed_list]
                coalesced_feed_dict = {}
                for k in feed_dicts[0].keys():
                    ndarrays = [d[k][0] for d in feed_dicts]
                    seq_lengths = [d[k][1] for d in feed_dicts]
                    coalesced_feed_dict[k] = [
                        np.concatenate(ndarrays),
                        self._merge_seq_length(seq_lengths)]
                feed_list = self._to_tensor(
                    coalesced_feed_dict, place)
        else:
            feed_list = [self._to_tensor(v[0], v[1], o) for v, o in zip(
                feed_list, self._tensor_dicts)]
        return feed_list, coalesced_extra_dict

    next = __next__

    def __iter__(self):
        self._iter = super(DataLoaderBuilder, self).__iter__()
        return self


@register
@serializable
class TrainDataLoader(DataLoaderBuilder):
    __doc__ = DataLoaderBuilder.__doc__


@register
@serializable
class EvalDataLoader(DataLoaderBuilder):
    __doc__ = DataLoaderBuilder.__doc__


@register
@serializable
class TestDataLoader(DataLoaderBuilder):
    __doc__ = DataLoaderBuilder.__doc__
