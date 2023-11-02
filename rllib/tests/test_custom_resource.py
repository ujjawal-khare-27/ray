import pytest

import ray
from ray import air
from ray import tune
from ray.tune.registry import get_trainable_cls


@pytest.mark.parametrize("algorithm", ["PPO", "IMPALA"])
def test_custom_resource(algorithm):
    if ray.is_initialized:
        ray.shutdown()

    ray.init(
        resources={"custom_resource": 1},
        include_dashboard=False,
    )

    config = (
        get_trainable_cls(algorithm)
        .get_default_config()
        .environment("CartPole-v1")
        .framework("torch")
        .rollouts(num_rollout_workers=1)
        .resources(num_gpus=0, custom_resources_per_worker={"custom_resource": 0.01})
    )
    stop = {"training_iteration": 1}

    tune.Tuner(
        algorithm,
        param_space=config,
        run_config=air.RunConfig(stop=stop, verbose=0),
        tune_config=tune.TuneConfig(num_samples=1),
    ).fit()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))
