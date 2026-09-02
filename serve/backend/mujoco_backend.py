import json
import os
from dataclasses import dataclass

import mujoco
import numpy as np

from scene.scene_generator import build_scene_xml, GENERATED_SCENE, GENERATED_META


@dataclass
class Fixture:
    name: str
    root_body: str
    pos: list
    size: list
    type: str


class MujocoSim:
    def __init__(self, model, data):
        self.model = model
        self.data = data
        self._renderer = None
        self._renderer_size = None
        self._scene_option = mujoco.MjvOption()
        self._scene_option.geomgroup[:] = 0
        self._scene_option.geomgroup[0] = 1
        self._scene_option.geomgroup[1] = 1
        self._scene_option.geomgroup[2] = 1

    def forward(self):
        mujoco.mj_forward(self.model, self.data)

    def render(self, width, height, camera_name="overhead_cam"):
        size = (int(width), int(height))
        if self._renderer is None or self._renderer_size != size:
            if self._renderer is not None:
                self._renderer.close()
            self._renderer = mujoco.Renderer(self.model, height=size[1], width=size[0])
            self._renderer_size = size

        camera_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, camera_name)
        if camera_id < 0:
            camera_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_CAMERA, "overhead_cam")
        self._renderer.update_scene(
            self.data,
            camera=camera_id if camera_id >= 0 else None,
            scene_option=self._scene_option,
        )
        return self._renderer.render()

    def close(self):
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None


class MujocoKitchenEnv:
    def __init__(self, scene_dir, seed=42):
        self.scene_dir = os.path.abspath(scene_dir)
        self.rng = np.random.default_rng(seed)
        self.scene_xml = build_scene_xml(self.scene_dir, seed=seed)
        self.model = mujoco.MjModel.from_xml_path(self.scene_xml)
        self.data = mujoco.MjData(self.model)
        self.sim = MujocoSim(self.model, self.data)
        self.action_dim = self.model.nu
        self.objects = []
        self.obj_body_id = {}
        self.obj_joint_id = {}
        self.fixtures = {}
        self.grasped_object = None
        self.virtual_ee_pos = np.zeros(3)
        self.base_free_joint_id = -1
        self.base_height = None
        self._load_registry()
        self._load_robot_registry()
        self.reset()

    def _load_registry(self):
        with open(GENERATED_META, "r", encoding="utf-8") as f:
            meta = json.load(f)

        self.objects = [item["name"] for item in meta["objects"]]
        for item in meta["objects"]:
            body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, item["root_body"])
            if body_id >= 0:
                self.obj_body_id[item["name"]] = body_id
                joint_adr = self.model.body_jntadr[body_id]
                if joint_adr >= 0:
                    self.obj_joint_id[item["name"]] = int(joint_adr)

        for item in meta["fixtures"]:
            self.fixtures[item["name"]] = Fixture(
                name=item["name"],
                root_body=item["root_body"],
                pos=item["pos"],
                size=item["size"],
                type=item["type"],
            )

    def _load_robot_registry(self):
        body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "chassis")
        if body_id < 0:
            return
        joint_adr = self.model.body_jntadr[body_id]
        if joint_adr < 0:
            return
        joint_id = int(joint_adr)
        if self.model.jnt_type[joint_id] == mujoco.mjtJoint.mjJNT_FREE:
            self.base_free_joint_id = joint_id
            qadr = self.model.jnt_qposadr[joint_id]
            # 记录初始高度（仅用于参考，不再钉 z）
            self.base_height = self._compute_ground_height(body_id) or float(self.model.qpos0[qadr + 2])

    def _compute_ground_height(self, chassis_body_id):
        """计算 chassis freejoint 的 z 高度，使轮子/脚轮恰好触地

        对于每个接触 geom：chassis_z + body_z + geom_z - radius = 0
        所以：chassis_z = -(body_z + geom_z) + radius
        取最大值（最高的 chassis 位置使所有接触 geom 都在地面以上）。
        """
        model = self.model
        max_z = None
        for i in range(model.ngeom):
            body_id = int(model.geom_bodyid[i])
            if body_id == chassis_body_id:
                continue
            if int(model.body_parentid[body_id]) != chassis_body_id:
                continue
            if model.geom_contype[i] == 0 and model.geom_conaffinity[i] == 0:
                continue
            body_z = model.body_pos[body_id][2]
            geom_z = model.geom_pos[i][2]
            r = model.geom_size[i][0]
            chassis_z = -(body_z + geom_z) + r
            if max_z is None or chassis_z > max_z:
                max_z = chassis_z
        return max_z

    def reset(self):
        mujoco.mj_resetData(self.model, self.data)
        self.data.ctrl[:] = 0.0
        self._set_initial_arm_pose()
        self._set_initial_objects()
        self.sim.forward()
        if self.base_free_joint_id >= 0:
            self.settle_base(steps=2000)
        self.virtual_ee_pos = self.get_body_pos("Fixed_Jaw_2", fallback="Moving_Jaw_2")
        return {}

    def _set_initial_arm_pose(self):
        arm_pose = {
            "Rotation_R": 0.0,
            "Pitch_R": 2.0,
            "Elbow_R": 0.5,
            "Wrist_Pitch_R": -0.3,
            "Wrist_Roll_R": 0.0,
            "Jaw_R": 0.0,
            "Rotation_L": 0.0,
            "Pitch_L": 2.0,
            "Elbow_L": 0.5,
            "Wrist_Pitch_L": -0.3,
            "Wrist_Roll_L": 0.0,
            "Jaw_L": 0.0,
        }
        for joint_name, value in arm_pose.items():
            joint_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, joint_name)
            if joint_id < 0:
                continue
            qadr = self.model.jnt_qposadr[joint_id]
            self.data.qpos[qadr] = value
            actuator_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, joint_name)
            if actuator_id >= 0:
                self.data.ctrl[actuator_id] = value

    def _set_initial_objects(self):
        with open(GENERATED_META, "r", encoding="utf-8") as f:
            meta = json.load(f)
        initial_pos = {item["name"]: item["pos"] for item in meta["objects"]}
        for obj_name in self.objects:
            joint_id = self.obj_joint_id.get(obj_name)
            if joint_id is None:
                continue
            qadr = self.model.jnt_qposadr[joint_id]
            pos = np.asarray(initial_pos[obj_name], dtype=float)
            self.data.qpos[qadr:qadr + 3] = pos
            self.data.qpos[qadr + 3:qadr + 7] = [1.0, 0.0, 0.0, 0.0]

    def step(self, action=None):
        if action is not None:
            action = np.asarray(action, dtype=float)
            n = min(action.size, self.model.nu)
            self.data.ctrl[:n] = action[:n]
        if self.grasped_object:
            self.set_object_pos(self.grasped_object, self.virtual_ee_pos)
        mujoco.mj_step(self.model, self.data)
        if self.grasped_object:
            self.set_object_pos(self.grasped_object, self.virtual_ee_pos)
            self.sim.forward()

    def _stabilize_base(self):
        """只锁 pitch/roll，不锁 z，让物理引擎自然处理轮子-地面接触"""
        if self.base_free_joint_id < 0:
            return
        qadr = self.model.jnt_qposadr[self.base_free_joint_id]
        dadr = self.model.jnt_dofadr[self.base_free_joint_id]
        # 恢复 quaternion，只保留 yaw（防止 pitch/roll 积累）
        yaw = self._base_yaw_from_qpos(qadr)
        self.data.qpos[qadr + 3:qadr + 7] = [np.cos(yaw / 2.0), 0.0, 0.0, np.sin(yaw / 2.0)]
        # 只清 pitch/roll 角速度
        self.data.qvel[dadr + 3] = 0.0  # wx: roll
        self.data.qvel[dadr + 4] = 0.0  # wy: pitch
        self.sim.forward()

    def settle_base(self, steps=2000):
        """启动时调用，让机器人自然沉降到地面"""
        for _ in range(steps):
            mujoco.mj_step(self.model, self.data)
        self._stabilize_base()

    def _base_yaw_from_qpos(self, qadr):
        w, x, y, z = self.data.qpos[qadr + 3:qadr + 7]
        return float(np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))

    def close(self):
        self.sim.close()

    def get_body_pos(self, body_name, fallback=None):
        body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, body_name)
        if body_id < 0 and fallback:
            body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, fallback)
        if body_id < 0:
            return self.virtual_ee_pos.copy()
        return self.data.xpos[body_id].copy()

    def get_body_quat(self, body_name, fallback=None):
        body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, body_name)
        if body_id < 0 and fallback:
            body_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, fallback)
        if body_id < 0:
            return np.array([1.0, 0.0, 0.0, 0.0])
        return self.data.xquat[body_id].copy()

    def get_object_pos(self, obj_name):
        return self.data.xpos[self.obj_body_id[obj_name]].copy()

    def set_object_pos(self, obj_name, pos):
        joint_id = self.obj_joint_id[obj_name]
        qadr = self.model.jnt_qposadr[joint_id]
        self.data.qpos[qadr:qadr + 3] = np.asarray(pos, dtype=float)
        self.data.qpos[qadr + 3:qadr + 7] = [1.0, 0.0, 0.0, 0.0]
        self.sim.forward()
