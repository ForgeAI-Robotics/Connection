"""Train ACT policy."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import argparse
import pickle
from copy import deepcopy
import numpy as np
import torch
from torch.utils.data import DataLoader, random_split
from tqdm import tqdm

from teleop.act.models import ACTPolicy
from teleop.act.data.dataset import ACTDataset, compute_mean_std
from teleop.act.configs.grasp_task import TASK_CONFIG


def forward_pass(data, policy):
    images, qpos, action, is_pad = data
    images = images.cuda()
    qpos = qpos.cuda()
    action = action.cuda()
    is_pad = is_pad.cuda()
    return policy(qpos, images, action, is_pad)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch_size", type=int, default=None)
    parser.add_argument("--lr", type=float, default=None)
    args = parser.parse_args()

    config = deepcopy(TASK_CONFIG)
    if args.epochs: config["num_epochs"] = args.epochs
    if args.batch_size: config["batch_size"] = args.batch_size
    if args.lr: config["lr"] = args.lr

    data_dir = config["data_dir"]
    if not os.path.exists(data_dir) or len([f for f in os.listdir(data_dir) if f.endswith(".h5")]) == 0:
        print(f"No data found in {data_dir}")
        print("Run 'python teleop/act/collect.py' first to collect demonstrations.")
        return

    stats = compute_mean_std(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "dataset_stats.pkl"), "wb") as f:
        pickle.dump(stats, f)

    dataset = ACTDataset(data_dir, config["camera_names"], config["policy_config"]["chunk_size"], config["action_dim"])
    val_size = max(1, int(0.1 * len(dataset)))
    train_size = len(dataset) - val_size
    train_dataset, val_dataset = random_split(dataset, [train_size, val_size])

    train_loader = DataLoader(train_dataset, batch_size=config["batch_size"], shuffle=True, num_workers=0)
    val_loader = DataLoader(val_dataset, batch_size=config["batch_size"], shuffle=False, num_workers=0)

    policy_config = config["policy_config"]
    policy_config["state_dim"] = config["state_dim"]
    policy_config["action_dim"] = config["action_dim"]
    policy = ACTPolicy(policy_config)
    policy.cuda()
    optimizer = policy.configure_optimizers()

    ckpt_dir = os.path.join(data_dir, "checkpoints")
    os.makedirs(ckpt_dir, exist_ok=True)

    best_val_loss = np.inf
    for epoch in tqdm(range(config["num_epochs"])):
        policy.train()
        for batch in train_loader:
            loss_dict = forward_pass(batch, policy)
            loss = loss_dict["loss"]
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

        if (epoch + 1) % config["validate_every"] == 0:
            policy.eval()
            val_losses = []
            with torch.inference_mode():
                for batch in val_loader:
                    loss_dict = forward_pass(batch, policy)
                    val_losses.append(loss_dict["loss"].item())
            val_loss = np.mean(val_losses)
            print(f"Epoch {epoch+1}, Val loss: {val_loss:.5f}")
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                torch.save(policy.state_dict(), os.path.join(ckpt_dir, "policy_best.ckpt"))

        if (epoch + 1) % config["save_every"] == 0:
            torch.save(policy.state_dict(), os.path.join(ckpt_dir, f"policy_epoch_{epoch+1}.ckpt"))

    torch.save(policy.state_dict(), os.path.join(ckpt_dir, "policy_last.ckpt"))
    print(f"Training done. Best val loss: {best_val_loss:.5f}")


if __name__ == "__main__":
    main()
