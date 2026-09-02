import os
import numpy as np
import torch
from torch.utils.data import Dataset
import h5py


class ACTDataset(Dataset):
    def __init__(self, data_dir, camera_names=["head_cam"], chunk_size=40, action_dim=6):
        self.data_dir = data_dir
        self.camera_names = camera_names
        self.chunk_size = chunk_size
        self.action_dim = action_dim
        self.episodes = [os.path.join(data_dir, f) for f in sorted(os.listdir(data_dir)) if f.endswith(".h5")]

    def __len__(self):
        return len(self.episodes)

    def __getitem__(self, idx):
        with h5py.File(self.episodes[idx], "r") as f:
            images = f["observations/images"][:]
            qpos = f["observations/qpos"][:]
            actions = f["actions"][:]

        images = torch.from_numpy(images).float()
        qpos = torch.from_numpy(qpos).float()
        actions = torch.from_numpy(actions).float()

        T = actions.shape[0]
        chunk_actions = torch.zeros((self.chunk_size, self.action_dim))
        is_pad = torch.ones(self.chunk_size, dtype=torch.bool)
        actual_len = min(T, self.chunk_size)
        chunk_actions[:actual_len] = actions[:actual_len]
        is_pad[:actual_len] = False

        return images[0], qpos[0], chunk_actions, is_pad


def compute_mean_std(data_dir):
    all_qpos = []
    all_actions = []
    for f in sorted(os.listdir(data_dir)):
        if f.endswith(".h5"):
            with h5py.File(os.path.join(data_dir, f), "r") as hf:
                all_qpos.append(hf["observations/qpos"][:])
                all_actions.append(hf["actions"][:])
    all_qpos = np.concatenate(all_qpos, axis=0)
    all_actions = np.concatenate(all_actions, axis=0)
    return {
        "qpos_mean": all_qpos.mean(axis=0),
        "qpos_std": all_qpos.std(axis=0) + 1e-6,
        "action_mean": all_actions.mean(axis=0),
        "action_std": all_actions.std(axis=0) + 1e-6,
    }
