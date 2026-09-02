import os
import numpy as np
import h5py


class DataCollector:
    def __init__(self, save_dir="teleop/act/data/hdf5"):
        self.save_dir = save_dir
        os.makedirs(save_dir, exist_ok=True)
        self.images = []
        self.qpos = []
        self.actions = []

    def reset(self):
        self.images = []
        self.qpos = []
        self.actions = []

    def record(self, image, qpos, action):
        self.images.append(image)
        self.qpos.append(qpos)
        self.actions.append(action)

    def save(self, task_name="grasp", episode_id=None):
        if len(self.images) == 0:
            return None
        if episode_id is None:
            episode_id = len([f for f in os.listdir(self.save_dir) if f.endswith(".h5")])
        filename = os.path.join(self.save_dir, f"{task_name}_episode_{episode_id:04d}.h5")
        with h5py.File(filename, "w") as f:
            f.create_dataset("observations/images", data=np.array(self.images))
            f.create_dataset("observations/qpos", data=np.array(self.qpos))
            f.create_dataset("actions", data=np.array(self.actions))
        print(f"Saved: {filename} ({len(self.images)} steps)")
        return filename
