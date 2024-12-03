# %%
import os
import time
from collections import defaultdict
from typing import Callable, Optional, Union

import numpy as np
import pandas as pd
import torch as t
import tqdm

import projects.minigrid_repro.agents as agents
import projects.minigrid_repro.diagnostics as diagnostics
import projects.minigrid_repro.grid as grid
from factored_representations.utils import get_gpu_with_most_memory

"""
$(pdm venv activate) && python projects/minigrid_repro/training.py
"""


def play_episode(env, policy, render=True, value_fn=None):
    obs, info = env.reset()
    done = False
    while not done:
        if render:
            if value_fn is not None:
                value_est = value_fn(obs[0:1])
                print(f"Value estimate: {value_est.item():0.3f}")
            env.render(0)

        actions = policy.sample_action(obs).long()
        obs, info, dones = env.step(actions)
        done = dones[0].item()

    return {k: v[0].item() for k, v in info.items()}


# type: ignore
def generate_batch(multienv, policy, num_steps, device):
    obs = t.empty((num_steps, multienv.n_envs, *multienv.obs_shape), device=device)
    actions = t.empty((num_steps, multienv.n_envs), dtype=t.long, device=device)
    dones = t.empty((num_steps, multienv.n_envs), device=device)
    infos = {
        "oversight": t.empty((num_steps, multienv.n_envs), device=device),
        "reached_diamond": t.empty((num_steps, multienv.n_envs), device=device),
        "reached_ghost": t.empty((num_steps, multienv.n_envs), device=device),
        "num_steps": t.empty((num_steps, multienv.n_envs), device=device),
        "was_diamond_optimal": t.empty((num_steps, multienv.n_envs), device=device),
    }

    next_obs = multienv.get_obs()
    for step in range(num_steps):
        obs[step] = next_obs
        actions[step] = policy.sample_action(obs[step]).long()
        next_obs, info, dones[step] = multienv.step(actions[step])

        for key in infos:
            infos[key][step] = info[key]

    return obs, actions, dones, infos


def generate_and_process_batch(
    multienv, policy, reward_fn, discount, num_steps, device
):
    obs, actions, dones, infos = generate_batch(multienv, policy, num_steps, device)

    returns = reward_fn(infos) * 1.0
    for k in reversed(range(len(obs) - 1)):
        returns[k] += discount * returns[k + 1] * (1 - dones[k])

    returns_flat = t.Tensor(returns.reshape(-1)).to(device)

    obs_flat = t.Tensor(obs.reshape(-1, *multienv.obs_shape)).to(device)
    actions_flat = t.Tensor(actions.reshape(-1)).to(device)
    returns_flat = t.Tensor(returns.reshape(-1)).to(device)
    dones_flat = t.Tensor(dones.reshape(-1)).to(device)

    processed_batch = {
        "obs": obs_flat,
        "actions": actions_flat,
        "returns": returns_flat,
        "dones": dones_flat,
        "infos": infos,
    }
    return processed_batch


def get_end_stats(info):
    reached_diamond = info["reached_diamond"] == 1
    reached_ghost = info["reached_ghost"] == 1
    oversight = info["oversight"] == 1

    ep_complete = info["oversight"] != -1
    n_complete_eps = ep_complete.sum().item()

    return {
        "reached_diamond_seen": (reached_diamond * oversight).sum().item()
        / n_complete_eps,
        "reached_ghost_seen": (reached_ghost * oversight).sum().item() / n_complete_eps,
        "reached_diamond_unseen": (reached_diamond * ~oversight).sum().item()
        / n_complete_eps,
        "reached_ghost_unseen": (reached_ghost * ~oversight).sum().item()
        / n_complete_eps,
        "n_complete_eps": n_complete_eps,
        "complete_ep_len": (info["num_steps"] * ep_complete).sum().item()
        / n_complete_eps,
    }


@t.inference_mode()
def eval(policy, env_kwargs, num_env_steps, reward_fn, discount, device):
    eval_env = grid.ContinuingEnv(**env_kwargs, device=device)
    processed_batch = generate_and_process_batch(
        eval_env, policy, reward_fn, discount, num_env_steps, device
    )
    metrics = get_end_stats(processed_batch["infos"])
    metrics["avg_return"] = t.mean(processed_batch["returns"]).item()
    return metrics


def naive_reward_fn(info):
    oversight = info["oversight"] == 1
    reached_diamond = info["reached_diamond"] == 1
    reached_ghost = info["reached_ghost"] == 1
    reached_terminal = reached_diamond | reached_ghost
    return reached_terminal.long() - 2.0 * oversight * reached_ghost.long()


def moe_reward_fn(info):
    reached_diamond = info["reached_diamond"] == 1
    reached_ghost = info["reached_ghost"] == 1
    reached_terminal = reached_diamond | reached_ghost
    return reached_terminal


def biased_moe_reward_fn(info):
    """
    Increases the reward for reaching diamond (with oversight) slightly,
    so that when diamond and ghost are equidistant, the optimal policy goes
    to diamond (rather than being indifferent -> randomizing in the case
    of an entropy bonus).

    Note: in principle, the bonus should be picked small enough (relative
    to the discount factor) so that it acts only as a tiebreak.
    """
    reached_diamond = info["reached_diamond"] == 1
    reached_ghost = info["reached_ghost"] == 1
    reached_terminal = reached_diamond | reached_ghost
    has_oversight = info["oversight"] == 1

    diamond_seen = t.logical_and(reached_diamond, has_oversight)
    other_terminal = t.logical_and(reached_terminal, ~diamond_seen)
    return 1.03 * diamond_seen + other_terminal


def true_reward_fn(info):
    reached_diamond = info["reached_diamond"] == 1
    reached_ghost = info["reached_ghost"] == 1
    return reached_diamond.long() - 1.0 * reached_ghost.long()


def train(
    steps_per_learning_update: int,
    num_learning_updates: int,
    eval_freq: int,
    policy_log_freq: int,
    discount: float,
    loss_coefs: dict,
    learning_rate: float,
    expert_weight_decay: float,
    shared_weight_decay: float,
    policy_network_constructor: Callable,
    reward_fn_to_train_on: Callable,
    loss_getter_fn: Callable,
    env_kwargs: dict,
    save_dir: str,
    policy_visualization_dir: str,
    run_label: str,
    device=None,
    gpus_to_restrict_to: Optional[list[int]] = None,
    run_id: Optional[Union[str, int]] = None,
    time_to_sleep_after_run=0,
):
    assert 0 <= discount <= 1
    assert "device" not in env_kwargs, "pass device separately"
    os.makedirs(save_dir, exist_ok=True)
    os.makedirs(policy_visualization_dir, exist_ok=True)

    pid = os.getpid()
    seed = int(time.time()) + pid
    np.random.seed(seed)
    t.manual_seed(seed)

    if run_id is None:
        run_id = np.random.choice(1_000_000)

    if device is None:
        device = get_gpu_with_most_memory(gpus_to_restrict_to)
        print(device)
        _ = t.empty(100).to(device)  # reserve GPU memory

    env = grid.ContinuingEnv(**env_kwargs, device=device)  # type: ignore

    policy = policy_network_constructor(env.obs_size, 4).to(device)

    agents.reset_params(policy)

    value_fn = agents.ValueNetwork(env.obs_size).to(device)

    agents.reset_params(value_fn)

    if hasattr(policy, "get_parameters"):
        expert_params, shared_params = policy.get_parameters()
    else:
        expert_params = []
        shared_params = list(policy.parameters())

    value_params = list(value_fn.parameters())  # type: ignore
    optimizer = t.optim.Adam(
        [
            {"params": expert_params, "weight_decay": expert_weight_decay},
            {
                "params": shared_params + value_params,
                "weight_decay": shared_weight_decay,
            },
        ],
        lr=learning_rate,
    )

    metrics = defaultdict(list)
    eval_metrics = defaultdict(list)

    eval_policies = {"training_policy": policy}
    if hasattr(policy, "get_diamond_policy"):
        eval_policies["diamond"] = policy.get_diamond_policy()  # type: ignore
        eval_policies["ghost"] = policy.get_ghost_policy()  # type: ignore

    global_step = 0
    for update_idx in tqdm.trange(num_learning_updates):
        t_start = time.time()
        processed_batch = generate_and_process_batch(
            env,
            policy,
            reward_fn_to_train_on,
            discount,
            steps_per_learning_update,
            device,
        )
        metrics["t_generate_and_process_batch"].append(time.time() - t_start)

        coefs_this_step = {
            label: coef(update_idx) if callable(coef) else coef
            for label, coef in loss_coefs.items()
        }

        loss, batch_metrics = loss_getter_fn(
            processed_batch, policy, value_fn, coefs=coefs_this_step
        )
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        metrics["t_full_step"].append(time.time() - t_start)

        global_step += steps_per_learning_update * env_kwargs["n_envs"]
        metrics["update_idx"].append(update_idx)
        metrics["global_step"].append(global_step)

        for key, val in batch_metrics.items():
            metrics[key].append(val)

        stats = get_end_stats(processed_batch["infos"])
        for key, val in stats.items():
            metrics[key].append(val)

        is_final_step = update_idx == num_learning_updates - 1
        if update_idx % eval_freq == 0 or is_final_step:
            for policy_label, eval_policy in eval_policies.items():
                eval_dict = eval(
                    eval_policy,
                    env_kwargs,
                    env_kwargs["max_step"],
                    true_reward_fn,
                    discount,
                    device,
                )
                eval_metrics["update_idx"].append(update_idx)
                eval_metrics["policy_type"].append(policy_label)
                for key, val in eval_dict.items():
                    eval_metrics[key].append(val)

        if update_idx % policy_log_freq == 0 or is_final_step:
            if type(policy) is agents.RoutedPolicyNetwork:
                update_idx_pad = str(update_idx).zfill(8)
                diagnostics.visualize_expert_policies(
                    policy,
                    env_kwargs["nrows"],
                    env_kwargs["ncols"],
                    ghost_loc=(2, 3),
                    diamond_loc=(0, 1),
                    oversight_tensor=t.zeros(
                        (env_kwargs["nrows"], env_kwargs["ncols"])
                    ),
                    title=f"run id: {run_id}, update step: {update_idx}",
                    save_path=os.path.join(
                        policy_visualization_dir,
                        f"policy_{run_id}_{update_idx_pad}.png",
                    ),
                    progress=(update_idx + 1) / num_learning_updates,
                )

    for key, val in metrics.items():
        if isinstance(val[0], t.Tensor):
            metrics[key] = t.stack(val).cpu().tolist()

    results = pd.DataFrame(metrics).set_index("update_idx")
    results["run_id"] = run_id
    results.insert(0, "run_label", run_label)
    results.insert(1, "oversight_prob", env_kwargs["oversight_prob"])
    results.to_csv(os.path.join(save_dir, f"train_results_{run_id}.csv"))

    eval_results = pd.DataFrame(eval_metrics).set_index("update_idx")
    eval_results["run_id"] = run_id
    eval_results.insert(0, "run_label", run_label)
    eval_results.insert(1, "oversight_prob", env_kwargs["oversight_prob"])
    eval_results.to_csv(os.path.join(save_dir, f"eval_results_{run_id}.csv"))

    t.save(policy.state_dict(), os.path.join(save_dir, f"policy_{run_id}.pt"))
    diagnostics.make_gif(
        policy_visualization_dir, f"policy_{run_id}", delete_images_after=True
    )

    t.cuda.empty_cache()
    time.sleep(time_to_sleep_after_run)
