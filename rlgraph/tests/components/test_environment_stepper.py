# Copyright 2018 The RLgraph authors, All Rights Reserved.
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

import numpy as np
import unittest

from rlgraph.environments.environment import Environment
from rlgraph.components.neural_networks.actor_component import ActorComponent
from rlgraph.components.common.environment_stepper import EnvironmentStepper
from rlgraph.spaces import FloatBox, IntBox
from rlgraph.tests import ComponentTest
from rlgraph.tests.test_util import config_from_path
from rlgraph.utils.ops import DataOpTuple


class TestEnvironmentStepper(unittest.TestCase):
    """
    Tests for the EnvironmentStepper Component using a simple RandomEnv.
    """
    def test_environment_stepper_on_random_env(self):
        state_space = FloatBox(shape=(1,))
        action_space = IntBox(2)
        preprocessor_spec = None
        neural_network_spec = config_from_path("configs/test_simple_nn.json")
        exploration_spec = None
        actor_component = ActorComponent(
            preprocessor_spec, dict(neural_network=neural_network_spec, action_space=action_space), exploration_spec
        )
        environment_stepper = EnvironmentStepper(
            environment_spec=dict(
                type="random_env", state_space=state_space, action_space=action_space, deterministic=True
            ),
            actor_component_spec=actor_component,
            state_space=state_space,
            reward_space="float32"
        )

        test = ComponentTest(component=environment_stepper, action_space=action_space)

        # Reset the stepper.
        test.test("reset")

        # Step 3 times through the Env and collect results.
        # 1st return value is the step-op (None), 2nd return value is the tuple of items (3 steps each), with each
        # step containing: Preprocessed state, actions, rewards, episode returns, terminals, (raw) next-states.
        expected_r = np.array([0.49850702, 0.7605307, 0.68535984])
        expected = (None, (
            np.array([[0.77132064], [0.74880385], [0.19806287]]),  # p(s)
            np.array([0, 0, 0]),  # a
            expected_r,  # r
            np.array([expected_r[:1].sum(), expected_r[:2].sum(), expected_r[:3].sum()]),  # episode's accumulated returns
            np.array([False, False, False]),
            np.array([[0.74880385], [0.19806287], [0.08833981]]),  # s' (raw)
        ))
        test.test(("step", 3), expected_outputs=expected)

        # Step again, check whether stitching of states/etc.. works.
        expected_r2 = np.array([0.51219225, 0.7217553, 0.71457577])
        expected = (None, (
            np.array([[0.08833981], [0.00394827], [0.61252606]]),  # p(s)
            np.array([0, 0, 0]),  # a
            expected_r2,  # r
            np.array([expected_r.sum() + expected_r2[0], expected_r.sum() + expected_r2[:2].sum(), expected_r.sum() + expected_r2[:3].sum()]),
            np.array([False, False, False]),
            np.array([[0.00394827], [0.61252606], [0.91777414]]),  # s' (raw)
        ))
        test.test(("step", 3), expected_outputs=expected)

    def test_environment_stepper_on_pong(self):
        environment_spec = dict(type="openai_gym", gym_env="Pong-v0", frameskip=4, seed=10)
        dummy_env = Environment.from_spec(environment_spec)
        state_space = dummy_env.state_space
        action_space = dummy_env.action_space
        agent_config = config_from_path("configs/dqn_agent_for_pong.json")
        actor_component = ActorComponent(
            agent_config["preprocessing_spec"],
            dict(neural_network=agent_config["network_spec"], action_space=action_space),
            agent_config["exploration_spec"]
        )
        environment_stepper = EnvironmentStepper(
            environment_spec=environment_spec,
            actor_component_spec=actor_component,
            state_space=state_space,
            reward_space="float64"
        )

        test = ComponentTest(component=environment_stepper, action_space=action_space)

        # Reset the stepper.
        test.test("reset")

        # Step 30 times through the Env and collect results.
        # 1st return value is the step-op (None), 2nd return value is the tuple of items (3 steps each), with each
        # step containing: Preprocessed state, actions, rewards, episode returns, terminals, (raw) next-states.
        time_steps = 2000
        out = test.test(("step", [time_steps, 0]), expected_outputs=None)

        # Check types of outputs.
        self.assertTrue(out[0] is None)
        self.assertTrue(isinstance(out[1], DataOpTuple))

        # Check types of single data.
        self.assertTrue(out[1][0].dtype == np.float32)
        self.assertTrue(out[1][0].min() >= 0.0)  # make sure we have pixels / 255
        self.assertTrue(out[1][0].max() <= 1.0)
        self.assertTrue(out[1][1].dtype == np.int32)
        self.assertTrue(out[1][2].dtype == np.float64)
        self.assertTrue(out[1][3].dtype == np.float64)
        self.assertTrue(out[1][4].dtype == np.bool_)
        self.assertTrue(out[1][5].dtype == np.uint8)
        self.assertTrue(out[1][5].min() >= 0)  # make sure we have pixels
        self.assertTrue(out[1][5].max() <= 255)

        # Check whether episode returns match single rewards (including terminal signals).
        episode_returns = 0.0
        for i in range(time_steps):
            episode_returns += out[1][2][i]
            self.assertAlmostEqual(episode_returns, out[1][3][i])
            # Terminal: Reset for next step.
            if out[1][4][i] is np.bool_(True):
                episode_returns = 0.0
