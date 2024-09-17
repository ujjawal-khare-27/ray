import unittest

import ray
import ray.rllib.algorithms.ppo as ppo
from ray.rllib.algorithms.ppo.ppo_learner import (
    LEARNER_RESULTS_CURR_ENTROPY_COEFF_KEY,
)
from ray.rllib.algorithms.callbacks import DefaultCallbacks
from ray.rllib.core import DEFAULT_MODULE_ID
from ray.rllib.core.learner.learner import DEFAULT_OPTIMIZER, LR_KEY
from ray.rllib.core.rl_module.default_model_config import DefaultModelConfig
from ray.rllib.utils.metrics import LEARNER_RESULTS
from ray.rllib.utils.test_utils import check, check_train_results_new_api_stack


def get_model_config(lstm=False):
    return (
        dict(
            use_lstm=True,
            lstm_use_prev_action=True,
            lstm_use_prev_reward=True,
            lstm_cell_size=10,
            max_seq_len=20,
        )
        if lstm
        else {"use_lstm": False}
    )


class MyCallbacks(DefaultCallbacks):
    def on_train_result(self, *, algorithm, result: dict, **kwargs):
        stats = result[LEARNER_RESULTS][DEFAULT_MODULE_ID]
        # Entropy coeff goes to 0.05, then 0.0 (per iter).
        check(
            stats[LEARNER_RESULTS_CURR_ENTROPY_COEFF_KEY],
            0.05 if algorithm.iteration == 1 else 0.0,
        )

        # Learning rate should decrease by 0.0001/4 per iteration.
        check(
            stats[DEFAULT_OPTIMIZER + "_" + LR_KEY],
            0.0000075 if algorithm.iteration == 1 else 0.000005,
        )
        # Compare reported curr lr vs the actual lr found in the optimizer object.
        optim = algorithm.learner_group._learner.get_optimizer()
        actual_optimizer_lr = (
            optim.param_groups[0]["lr"]
            if algorithm.config.framework_str == "torch"
            else optim.lr
        )
        check(stats[DEFAULT_OPTIMIZER + "_" + LR_KEY], actual_optimizer_lr)


class TestPPO(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ray.init()

    @classmethod
    def tearDownClass(cls):
        ray.shutdown()

    def test_ppo_compilation_w_connectors(self):
        """Test whether PPO can be built with all frameworks w/ connectors."""

        # Build a PPOConfig object.
        config = (
            ppo.PPOConfig()
            .training(
                num_epochs=2,
                # Setup lr schedule for testing.
                lr_schedule=[[0, 5e-5], [128, 0.0]],
                # Set entropy_coeff to a faulty value to proof that it'll get
                # overridden by the schedule below (which is expected).
                entropy_coeff=100.0,
                entropy_coeff_schedule=[[0, 0.1], [256, 0.0]],
                train_batch_size=128,
                model=dict(
                    # Settings in case we use an LSTM.
                    lstm_cell_size=10,
                    max_seq_len=20,
                ),
            )
            .env_runners(
                num_env_runners=1,
                # Test with compression.
                compress_observations=True,
                enable_connectors=True,
            )
            .callbacks(MyCallbacks)
            .evaluation(
                evaluation_duration=2,
                evaluation_duration_unit="episodes",
                evaluation_num_env_runners=1,
            )
        )  # For checking lr-schedule correctness.

        num_iterations = 2

        for env in ["FrozenLake-v1", "ALE/MsPacman-v5"]:
            print("Env={}".format(env))
            for lstm in [False, True]:
                print("LSTM={}".format(lstm))
                config.training(
                    model=dict(
                        use_lstm=lstm,
                        lstm_use_prev_action=lstm,
                        lstm_use_prev_reward=lstm,
                    )
                )

                algo = config.build(env=env)
                policy = algo.get_policy()
                entropy_coeff = algo.get_policy().entropy_coeff
                lr = policy.cur_lr
                check(entropy_coeff, 0.1)
                check(lr, config.lr)

                for i in range(num_iterations):
                    results = algo.train()
                    check_train_results(results)
                    print(results)

                algo.evaluate()

                check_inference_w_connectors(policy, env_name=env)
                algo.stop()

    def test_ppo_compilation_and_schedule_mixins(self):
        """Test whether PPO can be built with all frameworks."""

        # Build a PPOConfig object with the `SingleAgentEnvRunner` class.
        config = (
            ppo.PPOConfig()
            .training(
                # Setup lr schedule for testing.
                lr_schedule=[[0, 5e-5], [256, 0.0]],
                # Set entropy_coeff to a faulty value to proof that it'll get
                # overridden by the schedule below (which is expected).
                entropy_coeff=100.0,
                entropy_coeff_schedule=[[0, 0.1], [512, 0.0]],
                train_batch_size=256,
                minibatch_size=128,
                num_epochs=2,
                model=dict(
                    # Settings in case we use an LSTM.
                    lstm_cell_size=10,
                    max_seq_len=20,
                ),
            )
            .env_runners(num_env_runners=0)
            .training(
                num_epochs=2,
                # Setup lr schedule for testing lr-scheduling correctness.
                lr=[[0, 0.00001], [512, 0.0]],  # 512=4x128
                # Setup `entropy_coeff` schedule for testing whether it's scheduled
                # correctly.
                entropy_coeff=[[0, 0.1], [256, 0.0]],  # 256=2x128,
                train_batch_size=128,
            )
            .callbacks(MyCallbacks)
            .evaluation(
                # Also test evaluation with remote workers.
                evaluation_num_env_runners=2,
                evaluation_duration=3,
                evaluation_duration_unit="episodes",
                evaluation_parallel_to_training=True,
            )
        )

        num_iterations = 2

        # TODO (sven) Bring back "FrozenLake-v1"
        for env in [
            # "CliffWalking-v0",
            "CartPole-v1",
            "Pendulum-v1",
        ]:  # "ALE/Breakout-v5"]:
            print("Env={}".format(env))
            for lstm in [False]:
                print("LSTM={}".format(lstm))
                config.rl_module(model_config=get_model_config(lstm=lstm))

                algo = config.build(env=env)
                # TODO: Maybe add an API to get the Learner(s) instances within
                #  a learner group, remote or not.
                learner = algo.learner_group._learner
                optim = learner.get_optimizer()
                # Check initial LR directly set in optimizer vs the first (ts=0)
                # value from the schedule.
                lr = optim.param_groups[0]["lr"]
                check(lr, config.lr[0][1])

                # Check current entropy coeff value using the respective Scheduler.
                entropy_coeff = learner.entropy_coeff_schedulers_per_module[
                    DEFAULT_MODULE_ID
                ].get_current_value()
                check(entropy_coeff, 0.1)

                for i in range(num_iterations):
                    results = algo.train()
                    check_train_results_new_api_stack(results)
                    print(results)

                # algo.evaluate()
                algo.stop()

    def test_ppo_free_log_std(self):
        """Tests the free log std option works."""
        config = (
            ppo.PPOConfig()
            .api_stack(
                enable_rl_module_and_learner=True,
                enable_env_runner_and_connector_v2=True,
            )
            .environment("Pendulum-v1")
            .env_runners(
                num_env_runners=1,
            )
            .rl_module(
                model_config=DefaultModelConfig(
                    fcnet_hiddens=[10],
                    fcnet_activation="linear",
                    free_log_std=True,
                    vf_share_layers=True,
                ),
            )
            .training(
                gamma=0.99,
            )
        )

        algo = config.build()
        module = algo.get_module(DEFAULT_MODULE_ID)

        # Check the free log std var is created.
        matching = [v for (n, v) in module.named_parameters() if "log_std" in n]
        assert len(matching) == 1, matching
        log_std_var = matching[0]

        def get_value(log_std_var=log_std_var):
            return log_std_var.detach().cpu().numpy()[0]

        # Check the variable is initially zero.
        init_std = get_value()
        assert init_std == 0.0, init_std
        algo.train()

        # Check the variable is updated.
        post_std = get_value()
        assert post_std != 0.0, post_std
        algo.stop()


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))
