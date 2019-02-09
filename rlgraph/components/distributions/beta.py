# Copyright 2018/2019 The RLgraph authors. All Rights Reserved.
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

from rlgraph import get_backend
from rlgraph.components.distributions.distribution import Distribution
from rlgraph.spaces.space_utils import sanity_check_space
from rlgraph.spaces import Tuple, FloatBox
from rlgraph.utils.decorators import rlgraph_api, graph_fn

if get_backend() == "tf":
    import tensorflow as tf
elif get_backend() == "pytorch":
    import torch


class Beta(Distribution):
    """
    A Beta distribution is defined on the interval [0, 1] and parameterized by shape parameters
    alpha and beta (also called concentration parameters).

    PDF(x; alpha, beta) = x**(alpha - 1) (1 - x)**(beta - 1) / Z
        with Z = Gamma(alpha) Gamma(beta) / Gamma(alpha + beta)
        and Gamma(n) = (n - 1)!

    """
    def __init__(self, scope="beta", **kwargs):
        # Do not flatten incoming DataOps as we need more than one parameter in our parameterize graph_fn.
        super(Beta, self).__init__(scope=scope, **kwargs)

    def check_input_spaces(self, input_spaces, action_space=None):
        # Must be a Tuple of len 2 (alpha and beta).
        in_space = input_spaces["parameters"]
        sanity_check_space(in_space, allowed_types=[Tuple])
        assert len(in_space) == 2, "ERROR: Expected Tuple of len=2 as input Space to Beta!"
        sanity_check_space(in_space[0], allowed_types=[FloatBox])
        sanity_check_space(in_space[1], allowed_types=[FloatBox])

    @rlgraph_api
    def _graph_fn_get_distribution(self, parameters):
        """
        Args:
            parameters (DataOpTuple): Tuple holding the alpha and beta parameters.
        """
        if get_backend() == "tf":
            #alpha, beta = tf.split(parameters, num_or_size_splits=2, axis=-1)
            # Note: concentration0==beta, concentration1=alpha (!)
            return tf.distributions.Beta(concentration1=parameters[0], concentration0=parameters[1])
        elif get_backend() == "pytorch":
            #alpha, beta = torch.split(parameters, 2, dim=-1)
            return torch.distributions.Beta(parameters[0], parameters[1])

    @graph_fn
    def _graph_fn_sample_deterministic(self, distribution):
        return distribution.mean()
