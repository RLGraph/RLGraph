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

import numpy as np

from rlgraph import get_backend
from rlgraph.agents import Agent
from rlgraph.components import Synchronizable, Memory, PrioritizedReplay
from rlgraph.components.algorithms.algorithm_component import AlgorithmComponent
from rlgraph.components.loss_functions.sac_loss_function import SACLossFunction
from rlgraph.components.neural_networks.q_function import QFunction
from rlgraph.components.optimizers.optimizer import Optimizer
from rlgraph.components.policies.policy import Policy
from rlgraph.execution.rules.sync_rules import SyncRules
from rlgraph.spaces import FloatBox, BoolBox, IntBox, ContainerSpace
from rlgraph.spaces.space_utils import sanity_check_space
from rlgraph.utils.decorators import rlgraph_api, graph_fn
from rlgraph.utils.ops import FlattenedDataOp

if get_backend() == "tf":
    import tensorflow as tf
#elif get_backend() == "pytorch":
#    import torch


class DADSAgent(Agent):
    """
    "Dynamics Aware Discovery of Skills" Agent implementation [1]
    Learns in two phases:
    1) Learns continuous skills in unsupervised fashion (without rewards).
    2) Uses a planner to apply these skills to a given optimization problem (with reward function).

    dads is a simple wrapper around any RL-algorithm (SAC is used in the paper), only adding an extra skill-vector
    input to the state space. Skills are selected during training time at (uniform) random and then fed  as continuous
    or one-hot vectors into the chosen algorithm's policy.

    [1]: Dynamics-Aware Unsupervised Discovery of Skills - Sharma et al - Google Brain - 2019
    https://arxiv.org/pdf/1907.01657.pdf
    """
    def __init__(
        self,
        state_space,
        action_space,
        *,
        rl_algorithm_spec=None,
        num_skill_dimensions=10,
        use_discrete_skills=False,
        execution_spec=None,
        summary_spec=None,
        saver_spec=None,
        auto_build=True,
        name="dads-agent"
    ):
        """
        Args:
            state_space (Union[dict,Space]): Spec dict for the state Space or a direct Space object.
            action_space (Union[dict,Space]): Spec dict for the action Space or a direct Space object.

            rl_algorithm_spec (Union[dict,AlgorithmComponent]): Spec dict for the underlying learning RL algorithm
                Component.
            num_skill_dimensions (int): The size of the skills vector.

            use_discrete_skills (bool): Whether to use discrete skills (one-hot vectors of size `num_skill_dimensions`).
                Default: False (continuous skills).

            execution_spec (Optional[dict,Execution]): The spec-dict specifying execution settings.
            summary_spec (Optional[dict]): Spec-dict to specify summary settings.
            saver_spec (Optional[dict]): Spec-dict to specify saver settings.

            auto_build (Optional[bool]): If True (default), immediately builds the graph using the agent's
                graph builder. If false, users must separately call agent.build(). Useful for debugging or analyzing
                components before building.

            name (str): Some name for this Agent object.
        """
        super(DADSAgent, self).__init__(
            state_space=state_space,
            action_space=action_space,
            execution_spec=execution_spec,
            summary_spec=summary_spec,
            saver_spec=saver_spec,
            name=name
        )

        self.num_skill_dimensions = num_skill_dimensions
        self.use_discrete_skills = use_discrete_skills

        self.underlying_state_space = None

        # Build the actual underlying AlgorithmComponent that will do the RL-learning from instrinsic rewards.
        self.rl_algorithm = AlgorithmComponent.from_spec(rl_algorithm_spec)

        #self.double_q = double_q
        ## Keep track of when to sync the target network (every n updates).
        #if isinstance(sync_rules, dict) and "sync_tau" not in sync_rules:
        #    sync_rules["sync_tau"] = 0.005  # The value mentioned in the paper
        #self.sync_rules = SyncRules.from_spec(sync_rules)
        #self.steps_since_target_net_sync = 0

        self.root_component = DADSAlgorithmComponent(
            agent=self,
            policy_spec=policy_spec,
            network_spec=network_spec,
            #q_function_spec=q_function_spec,  # q-functions
            preprocessing_spec=preprocessing_spec,
            memory_spec=memory_spec,
            discount=discount,
            memory_batch_size=memory_batch_size,
            optimizer_spec=optimizer_spec,
        )

        # Extend input Space definitions to this Agent's specific API-methods.
        self.preprocessed_state_space = self.root_component.preprocessor.get_preprocessed_space(self.state_space).\
            with_batch_rank()
        float_action_space = self.action_space.with_batch_rank().map(
            mapping=lambda flat_key, space: space.as_one_hot_float_space() if isinstance(space, IntBox) else space
        )
        self.input_spaces.update(dict(
            env_actions=self.action_space.with_batch_rank(),
            actions=float_action_space,
            preprocessed_states=self.preprocessed_state_space,
            rewards=FloatBox(add_batch_rank=True),
            terminals=BoolBox(add_batch_rank=True),
            next_states=self.preprocessed_state_space,
            states=self.state_space.with_batch_rank(add_batch_rank=True),
            importance_weights=FloatBox(add_batch_rank=True),
            deterministic=bool,
            policy_weights="variables:{}".format(self.root_component.policy.scope)
        ))

        if auto_build is True:
            self.build(build_options=dict(optimizers=self.root_component.all_optimizers))

    def set_weights(self, policy_weights, q_function_weights=None):
        return self.graph_executor.execute(("set_policy_weights", policy_weights))

    def get_weights(self):
        return dict(policy_weights=self.graph_executor.execute("get_policy_weights"))

    def get_action(self, states, internals=None, use_exploration=True, apply_preprocessing=True, extra_returns=None,
                   time_percentage=None):
        # Call super.
        ret = super(DADSAgent, self).get_action(
            states, internals, use_exploration, apply_preprocessing, extra_returns, time_percentage
        )
        actions = ret["actions"]

        # Convert Gumbel (relaxed one-hot) sample back into int type for all discrete composite actions.
        if isinstance(self.action_space, ContainerSpace):
            actions = actions.map(
                mapping=lambda key, action: np.argmax(action, axis=-1).astype(action.dtype)
                if isinstance(self.flat_action_space[key], IntBox) else action
            )
        elif isinstance(self.action_space, IntBox):
            actions = np.argmax(actions, axis=-1).astype(self.action_space.dtype)

        if "preprocessed_states" in extra_returns:
            return dict(actions=actions, preprocessed_states=ret["preprocessed_states"])
        else:
            return dict(actions=actions)

    def _observe_graph(self, preprocessed_states, actions, internals, rewards, terminals, **kwargs):
        next_states = kwargs.pop("next_states")
        self.graph_executor.execute(("insert_records", [preprocessed_states, actions, rewards, next_states, terminals]))

    def update(self, batch=None, time_percentage=None, **kwargs):
        if time_percentage is None:
            time_percentage = self.timesteps / (self.max_timesteps or 1e6)

        self.num_updates += 1

        if batch is None:
            #size = self.graph_executor.execute("get_memory_size")
            # TODO: is this necessary?
            #if size < self.batch_size:
            #    return 0.0, 0.0, 0.0
            ret = self.graph_executor.execute(("update_from_memory", [time_percentage]))
        else:
            # No sequence indices means terminals are used in place.
            batch_input = [
                batch["states"], batch["actions"], batch["rewards"], batch["terminals"], batch["next_states"],
                batch["importance_weights"], time_percentage
            ]
            ret = self.graph_executor.execute(("update_from_external_batch", batch_input))

        return \
            ret["actor_loss"] + ret["critic_loss"] + ret["alpha_loss"],\
            ret["actor_loss_per_item"] + ret["critic_loss_per_item"] + ret["alpha_loss_per_item"]

    def reset(self):
        """
        Resets our preprocessor, but only if it contains stateful PreprocessLayer Components (meaning
        the PreprocessorStack has at least one variable defined).

        Also syncs all target-q-nets to their corresponding q-net weights, using tau=1.0.
        """
        if self.root_component.preprocessing_required and len(self.root_component.preprocessor.variables) > 0:
            self.graph_executor.execute("reset_preprocessor")
        self.graph_executor.execute("reset_targets")

    def __repr__(self):
        return "DADSAgent(skill-dims={} rl-algo={})".format(self.rl_algorithm.__repr__(), self.num_skill_dimensions)


class DADSAlgorithmComponent(AlgorithmComponent):
    def __init__(self, agent, memory_spec, scope="sac-agent-component", **kwargs):
        # Setup our policy
        # - non-deterministic
        # - Continuous actions: Use squashed normal.
        # - Discrete actions: Use Gumbel-softmax.
        policy_spec = kwargs.pop("policy_spec", None)
        if policy_spec is None:
            policy_spec = dict(
                deterministic=False, distributions_spec=dict(
                    bounded_distribution_type="squashed", discrete_distribution_type="gumbel_softmax",
                    gumbel_softmax_temperature=gumbel_softmax_temperature
                )
            )
        policy_spec = Policy.set_policy_deterministic(policy_spec, deterministic=False)

        if q_function_optimizer_spec is None:
            _q_optimizer_spec = kwargs.get("optimizer_spec")
        else:
            _q_optimizer_spec = q_function_optimizer_spec

        super(DADSAlgorithmComponent, self).__init__(
            agent, policy_spec=policy_spec, scope=scope, **kwargs
        )

        # Create a sync-rules object.
        self.q_function_sync_rules = SyncRules.from_spec(q_function_sync_rules)
        self.q_function = QFunction.from_spec(q_function_spec)

        self.memory = Memory.from_spec(memory_spec)
        # Copy value function n times to reach num_q_functions.
        # SAC usually (if num_q_functions==2) contains one state value function and one state-action (Q) value function.
        self.q_functions = [self.q_function] + [
            self.q_function.copy(scope="{}-{}".format(self.q_function.scope, i + 2), trainable=True)
            for i in range(num_q_functions - 1)
        ]

        # Set number of return values for get_q_values graph_fn.
        self.graph_fn_num_outputs["_graph_fn_get_q_values"] = num_q_functions

        # Produce target q-functions from respective base q-functions (which now also contain
        # the Synchronizable component).
        self.target_q_functions = [q.copy(scope="target-" + q.scope, trainable=False) for q in self.q_functions]
        # Make all target q_functions synchronizable if not done yet.
        if "synchronizable" not in self.target_q_functions[0].sub_components:
            for t in self.target_q_functions:
                t.add_components(Synchronizable(
                    sync_tau=self.q_function_sync_rules.sync_tau,
                    sync_every_n_calls=self.q_function_sync_rules.sync_every_n_updates
                ), expose_apis="sync")

        # Change name to avoid scope-collision.
        if isinstance(_q_optimizer_spec, dict):
            _q_optimizer_spec["scope"] = "q-function-optimizer"
        else:
            _q_optimizer_spec.scope = _q_optimizer_spec.name = "q-function-optimizer"
            _q_optimizer_spec.propagate_scope()

        self.q_function_optimizer = Optimizer.from_spec(_q_optimizer_spec)
        self.all_optimizers.append(self.q_function_optimizer)

        self.target_entropy = target_entropy
        self.alpha_optimizer = self.optimizer.copy(scope="alpha-" + self.optimizer.scope) if self.target_entropy is not None else None
        self.initial_alpha = initial_alpha
        self.log_alpha = None

        self.loss_function = SACLossFunction(
            target_entropy=target_entropy, discount=self.discount, num_q_functions=num_q_functions
        )

        self.steps_since_last_sync = None
        self.env_action_space = None

        self.add_components(self.memory, self.loss_function, self.alpha_optimizer, self.q_function_optimizer)
        self.add_components(*self.q_functions)
        self.add_components(*self.target_q_functions)

    def check_input_spaces(self, input_spaces, action_space=None):
        for s in ["states", "env_actions", "preprocessed_states", "rewards", "terminals"]:
            sanity_check_space(input_spaces[s], must_have_batch_rank=True)

        self.env_action_space = input_spaces["env_actions"].flatten()

    def create_variables(self, input_spaces, action_space=None):
        self.steps_since_last_sync = self.get_variable("steps_since_last_sync", dtype="int", initializer=0)
        self.log_alpha = self.get_variable("log_alpha", dtype="float", initializer=np.log(self.initial_alpha))

    @rlgraph_api
    def get_policy_weights(self):
        return self.policy.variables()

    @rlgraph_api
    def get_q_weights(self):
        merged_weights = {"q_{}".format(i): q.variables() for i, q in enumerate(self.q_functions)}
        for i, tq in enumerate(self.target_q_functions):
            merged_weights["target_q_{}".format(i)] = tq.variables()
        return merged_weights

    @rlgraph_api
    def set_policy_weights(self, policy_weights):
        return self.policy.sync(policy_weights)

    """ TODO: need to define the input space
    @rlgraph_api(must_be_complete=False)
    def set_q_weights(self, q_weights):
        split_weights = self._q_vars_splitter.call(q_weights)
        assert len(split_weights) == len(self.q_functions)
        update_ops = [q.sync(q_weights) for q_weights, q in zip(split_weights, self.q_functions)]
        update_ops.extend([q.sync(q_weights) for q_weights, q in zip(split_weights, self.target_q_functions)])
        return tuple(update_ops)
    """

    @rlgraph_api
    def insert_records(self, preprocessed_states, env_actions, rewards, next_states, terminals):
        records = dict(
            states=preprocessed_states, env_actions=env_actions, rewards=rewards, next_states=next_states,
            terminals=terminals
        )
        return self.memory.insert_records(records)

    @rlgraph_api
    def update_from_memory(self, time_percentage=None):
        records, sample_indices, importance_weights = self.memory.get_records(self.memory_batch_size)
        result = self.update_from_external_batch(
            records["states"], records["env_actions"], records["rewards"], records["terminals"],
            records["next_states"], importance_weights, time_percentage
        )

        if isinstance(self.memory, PrioritizedReplay):
            update_pr_step_op = self.memory.update_records(sample_indices, result["critic_loss_per_item"])
            result["step_op"] = self._graph_fn_group(result["step_op"], update_pr_step_op)

        return result

    @rlgraph_api
    def update_from_external_batch(self, preprocessed_states, env_actions, rewards, terminals, next_states,
                                   importance_weights, time_percentage=None
    ):
        actor_loss, actor_loss_per_item, critic_loss, critic_loss_per_item, alpha_loss, alpha_loss_per_item = \
            self.get_losses(preprocessed_states, env_actions, rewards, terminals, next_states, importance_weights)

        policy_vars = self.policy.variables()
        merged_q_vars = {"q_{}".format(i): q.variables() for i, q in enumerate(self.q_functions)}
        critic_step_op = self.q_function_optimizer.step(
            merged_q_vars, critic_loss, critic_loss_per_item, time_percentage
        )

        actor_step_op = self.optimizer.step(
            policy_vars, actor_loss, actor_loss_per_item, time_percentage
        )

        if self.target_entropy is not None:
            #self.update_alpha(alpha_loss, alpha_loss_per_item, time_percentage)
            alpha_step_op = self.alpha_optimizer.step(
                (self.log_alpha,), alpha_loss, alpha_loss_per_item, time_percentage
            )
        else:
            alpha_step_op = self._graph_fn_no_op()
        # TODO: optimizer for alpha
        sync_op = self.sync_targets()

        # Increase the global training step counter.
        alpha_step_op = self._graph_fn_training_step(alpha_step_op)

        return dict(
            actor_step_op=actor_step_op,
            critic_step_op=critic_step_op,
            sync_op=sync_op,
            alpha_step_op=alpha_step_op,
            actor_loss=actor_loss,
            actor_loss_per_item=actor_loss_per_item,
            critic_loss=critic_loss,
            critic_loss_per_item=critic_loss_per_item,
            alpha_loss=alpha_loss,
            alpha_loss_per_item=alpha_loss_per_item
        )

    @graph_fn(flatten_ops=True, split_ops=True, add_auto_key_as_first_param=True)
    def _graph_fn_one_hot(self, key, env_actions):
        # If int-box, ont-hot flatten.
        if isinstance(self.env_action_space[key], IntBox):
            env_actions = tf.one_hot(env_actions, depth=self.env_action_space[key].num_categories, axis=-1)
        # Force a shape of (1,) rather than 0D.
        elif self.env_action_space[key].shape == ():
            env_actions = tf.expand_dims(env_actions, axis=-1)
        return env_actions

    @rlgraph_api(flatten_ops={1})  # `returns` are determined in ctor
    def _graph_fn_get_q_values(self, preprocessed_states, actions, target=False):
        if isinstance(actions, FlattenedDataOp):
            if get_backend() == "tf":
                actions = tf.concat(list(actions.values()), axis=-1)
            elif get_backend() == "pytorch":
                actions = torch.cat(list(actions.values()), dim=-1)

        q_funcs = self.q_functions if target is False else self.target_q_functions

        # We do not concat states yet because we might pass states through a conv stack before merging it
        # with actions.
        return tuple(q.call(preprocessed_states, actions) for q in q_funcs)

    @rlgraph_api
    def get_losses(self, preprocessed_states, env_actions, rewards, terminals, next_states, importance_weights):
        # TODO: internal states
        samples_next = self.policy.get_action_and_log_likelihood(next_states, deterministic=False)
        next_sampled_actions = samples_next["action"]
        log_probs_next_sampled = samples_next["log_likelihood"]
        q_values_next_sampled = self.get_q_values(next_states, next_sampled_actions, target=True)
        actions = self._graph_fn_one_hot(env_actions)
        q_values = self.get_q_values(preprocessed_states, actions)

        samples = self.policy.get_action_and_log_likelihood(preprocessed_states, deterministic=False)
        sampled_actions = samples["action"]
        log_probs_sampled = samples["log_likelihood"]
        q_values_sampled = self.get_q_values(preprocessed_states, sampled_actions)

        alpha = self._graph_fn_compute_alpha()

        return self.loss_function.loss(
            alpha,
            log_probs_next_sampled,
            q_values_next_sampled,
            q_values,
            log_probs_sampled,
            q_values_sampled,
            rewards,
            terminals
        )

    @rlgraph_api
    def reset_targets(self):
        """
        Resets all targets to the exact source values (tau=1.0).
        """
        ops = (target_q.sync(q.variables(), tau=1.0) for q, target_q in zip(self.q_functions, self.target_q_functions))
        return tuple(ops)

    @rlgraph_api
    def sync_targets(self):
        return self._graph_fn_group(
            *[target.sync(source.variables()) for source, target in zip(self.q_functions, self.target_q_functions)]
        )

    @rlgraph_api
    def get_memory_size(self):
        return self.memory.get_size()

    @graph_fn
    def _graph_fn_compute_alpha(self):
        backend = get_backend()
        if backend == "tf":
            return tf.exp(self.log_alpha)
        elif backend == "pytorch":
            return torch.exp(self.log_alpha)

    # TODO: Move this into generic AlgorithmComponent.
    @graph_fn
    def _graph_fn_training_step(self, other_step_op=None):
        if self.agent is not None:
            add_op = tf.assign_add(self.agent.graph_executor.global_training_timestep, 1)
            op_list = [add_op] + [other_step_op] if other_step_op is not None else []
            with tf.control_dependencies(op_list):
                return tf.no_op() if other_step_op is None else other_step_op
        else:
            return tf.no_op() if other_step_op is None else other_step_op

    # TODO: Move this into generic AlgorithmComponent.
    @graph_fn
    def _graph_fn_no_op(self):
        return tf.no_op()