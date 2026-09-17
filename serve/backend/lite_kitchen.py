"""Self-contained MuJoCo kitchen for camera/VLM when RoboCasa assets are not ready."""

from __future__ import annotations

import os

import mujoco
import numpy as np

from backend.mujoco_backend import MujocoSim

LITE_SCENE_XML = """
<mujoco model="lite_kitchen">
  <compiler angle="radian" autolimits="true"/>
  <option gravity="0 0 -9.81" timestep="0.002"/>
  <visual>
    <global offwidth="1280" offheight="960"/>
    <headlight ambient="0.35 0.35 0.35" diffuse="0.65 0.65 0.65" specular="0.2 0.2 0.2"/>
  </visual>
  <asset>
    <texture name="grid" type="2d" builtin="checker" rgb1=".55 .52 .48" rgb2=".42 .40 .36" width="512" height="512"/>
    <material name="floor" texture="grid" texrepeat="6 6" reflectance="0"/>
    <material name="wood" rgba="0.62 0.42 0.24 1"/>
    <material name="leg" rgba="0.38 0.26 0.16 1"/>
    <material name="apple" rgba="0.82 0.12 0.10 1"/>
    <material name="cup" rgba="0.18 0.42 0.78 1"/>
    <material name="plate" rgba="0.93 0.93 0.90 1"/>
    <material name="ketchup" rgba="0.72 0.08 0.10 1"/>
    <material name="bowl" rgba="0.90 0.55 0.18 1"/>
    <material name="robot" rgba="0.18 0.20 0.24 1"/>
  </asset>
  <worldbody>
    <light name="key" pos="0.9 -0.2 2.4" dir="0 0.15 -1" diffuse="0.9 0.88 0.82" specular="0.3 0.3 0.3"/>
    <light name="fill" pos="-0.4 0.6 2.0" dir="0.2 -0.2 -1" diffuse="0.35 0.38 0.42"/>
    <geom name="floor" type="plane" size="4 4 0.1" material="floor"/>

    <body name="counter" pos="0.85 0 0.90">
      <geom type="box" size="0.85 0.42 0.035" material="wood"/>
      <geom type="box" size="0.05 0.40 0.43" pos="-0.72 0 -0.47" material="leg"/>
      <geom type="box" size="0.05 0.40 0.43" pos="0.72 0 -0.47" material="leg"/>
    </body>

    <body name="apple" pos="0.55 0.08 0.98">
      <freejoint/>
      <geom type="sphere" size="0.045" material="apple" mass="0.12"/>
    </body>
    <body name="cup" pos="0.85 -0.12 0.99">
      <freejoint/>
      <geom type="cylinder" size="0.035 0.055" material="cup" mass="0.08"/>
    </body>
    <body name="plate" pos="1.15 0.05 0.94">
      <freejoint/>
      <geom type="cylinder" size="0.10 0.012" material="plate" mass="0.20"/>
    </body>
    <body name="ketchup" pos="1.35 -0.10 1.00">
      <freejoint/>
      <geom type="cylinder" size="0.028 0.07" material="ketchup" mass="0.18"/>
    </body>
    <body name="bowl" pos="0.35 -0.15 0.97">
      <freejoint/>
      <geom type="cylinder" size="0.07 0.035" material="bowl" mass="0.16"/>
    </body>

    <body name="mobilebase0_base" pos="-0.15 -0.85 0.28">
      <geom type="cylinder" size="0.22 0.18" material="robot"/>
    </body>
    <body name="robot0_right_hand" pos="0.25 -0.45 1.05">
      <geom type="capsule" size="0.035 0.08" rgba="0.75 0.76 0.80 1"/>
    </body>

    <camera name="overhead_cam" pos="0.85 0 2.15" xyaxes="1 0 0 0 1 0" fovy="55"/>
    <camera name="head_cam" pos="0.85 -0.95 1.28" xyaxes="1 0 0 0 0.25 1" fovy="58"/>
    <camera name="robot0_frontview" pos="0.15 -0.90 1.18" xyaxes="1 0 0 0 0.22 1" fovy="60"/>
    <camera name="side_cam" pos="2.15 0 1.35" xyaxes="0 1 0 -0.25 0 1" fovy="55"/>
    <camera name="robot0_eye_in_hand" pos="0.30 -0.40 1.12" xyaxes="1 0 0 0 0.15 1" fovy="70"/>
    <camera name="robot0_agentview_center" pos="0.0 -1.15 1.35" xyaxes="1 0 0 0 0.35 1" fovy="58"/>
  </worldbody>
</mujoco>
"""


class _FlippedSim(MujocoSim):
    """robosuite sim.render is OpenGL-flipped; serve/server.py always flipud's it back."""

    def render(self, width, height, camera_name="overhead_cam"):
        image = super().render(width, height, camera_name=camera_name)
        return np.flipud(image)


class LiteKitchenEnv:
    def __init__(self, scene_dir, seed=42):
        self.scene_dir = os.path.abspath(scene_dir)
        self.rng = np.random.default_rng(seed)
        self.model = mujoco.MjModel.from_xml_string(LITE_SCENE_XML)
        self.data = mujoco.MjData(self.model)
        self.raw_model = self.model
        self.raw_data = self.data
        self.sim = _FlippedSim(self.model, self.data)
        self.action_dim = max(int(self.model.nu), 1)
        self.objects = ["apple", "cup", "plate", "ketchup", "bowl"]
        self.obj_body_id = {}
        self.obj_joint_id = {}
        self.fixtures = {
            "counter": type(
                "Fx",
                (),
                {
                    "pos": np.array([0.85, 0.0, 0.90]),
                    "size": np.array([1.70, 0.84, 0.07]),
                },
            )()
        }
        self.grasped_object = None
        for name in self.objects:
            body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, name)
            if body_id < 0:
                continue
            self.obj_body_id[name] = body_id
            joint_adr = self.model.body_jntadr[body_id]
            if joint_adr >= 0:
                self.obj_joint_id[name] = int(joint_adr)
        self.reset()

    def reset(self):
        mujoco.mj_resetData(self.model, self.data)
        self.sim.forward()
        idle = np.zeros(self.action_dim)
        for _ in range(80):
            self.step(idle)
        return {}

    def step(self, action=None):
        if action is not None and self.model.nu > 0:
            action = np.asarray(action, dtype=float)
            n = min(action.size, self.model.nu)
            self.data.ctrl[:n] = action[:n]
        mujoco.mj_step(self.model, self.data)

    def close(self):
        self.sim.close()

    def get_body_pos(self, body_name, fallback=None):
        body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, body_name)
        if body_id < 0 and fallback:
            body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, fallback)
        if body_id < 0:
            return np.zeros(3)
        return self.data.xpos[body_id].copy()

    def get_object_pos(self, obj_name):
        return self.data.xpos[self.obj_body_id[obj_name]].copy()

    def set_object_pos(self, obj_name, pos):
        joint_id = self.obj_joint_id.get(obj_name)
        if joint_id is None:
            return
        qadr = self.model.jnt_qposadr[joint_id]
        self.data.qpos[qadr : qadr + 3] = np.asarray(pos, dtype=float)
        self.data.qpos[qadr + 3 : qadr + 7] = [1.0, 0.0, 0.0, 0.0]
        self.sim.forward()
