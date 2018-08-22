# Copyright 2018 The RLgraph authors. All Rights Reserved.
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
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import cv2
import numpy as np
from six.moves import xrange as range_

from rlgraph import get_backend
from rlgraph.utils.ops import unflatten_op
from rlgraph.components.layers.preprocessing import PreprocessLayer

if get_backend() == "tf":
    import tensorflow as tf
    from tensorflow.python.ops.image_ops_impl import ResizeMethod


class ImageResize(PreprocessLayer):
    """
    Resizes one or more images to a new size without touching the color channel.
    """
    def __init__(self, width, height, interpolation="bilinear", scope="image-resize", **kwargs):
        """
        Args:
            width (int): The new width.
            height (int): The new height.
            interpolation (str): One of "bilinear", "area". Default: "bilinear" (which is also the default for both
                cv2 and tf).
        """
        super(ImageResize, self).__init__(scope=scope, **kwargs)
        self.width = width
        self.height = height
        
        if interpolation == "bilinear":
            self.cv2_interpolation = cv2.INTER_LINEAR
            self.tf_interpolation = ResizeMethod.BILINEAR
        else:
            self.cv2_interpolation = cv2.INTER_AREA
            self.tf_interpolation = ResizeMethod.AREA

        # The output spaces after preprocessing (per flat-key).
        self.output_spaces = None

    def get_preprocessed_space(self, space):
        ret = dict()
        for key, value in space.flatten().items():
            # Do some sanity checking.
            rank = value.rank
            assert rank == 2 or rank == 3, \
                "ERROR: Given image's rank (which is {}{}, not counting batch rank) must be either 2 or 3!".\
                format(rank, ("" if key == "" else " for key '{}'".format(key)))
            # Determine the output shape.
            shape = list(value.shape)
            shape[0] = self.width
            shape[1] = self.height
            ret[key] = value.__class__(shape=tuple(shape), add_batch_rank=value.has_batch_rank)
        return unflatten_op(ret)

    def check_input_spaces(self, input_spaces, action_space=None):
        super(ImageResize, self).check_input_spaces(input_spaces, action_space)
        in_space = input_spaces["preprocessing_inputs"]

        self.output_spaces = self.get_preprocessed_space(in_space)

    def _graph_fn_apply(self, preprocessing_inputs):
        """
        Images come in with either a batch dimension or not.
        """
        if self.backend == "python" or get_backend() == "python":
            if isinstance(preprocessing_inputs, list):
                preprocessing_inputs = np.asarray(preprocessing_inputs)
            if preprocessing_inputs.ndim == 4:
                resized = []
                for i in range_(len(preprocessing_inputs)):
                    resized.append(cv2.resize(preprocessing_inputs[i], dsize=(self.width, self.height),
                                              interpolation=self.cv2_interpolation))
                resized = np.asarray(resized)
                # TODO: Not sure about the following line ...
                # resized = resized[:, :, :, np.newaxis]
                return resized
            else:
                # Single sample.
                return cv2.resize(preprocessing_inputs, dsize=(self.width, self.height),
                                  interpolation=self.cv2_interpolation)
        elif get_backend() == "tf":
            return tf.image.resize_images(images=preprocessing_inputs, size=(self.width, self.height),
                                          method=self.tf_interpolation)

