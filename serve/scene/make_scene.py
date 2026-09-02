"""
make_scene.py - 自定义 Kitchen 子类
从本地 scene/config/ 目录读取 layout/style/objects YAML 配置
"""

import os
import yaml
import numpy as np
import mujoco

from robocasa.environments.kitchen.kitchen import Kitchen, KitchenArena
from robocasa.models.fixtures.fixture import FixtureType
import robocasa.utils.camera_utils as CamUtils

# 带门容器的门 joint 命名后缀(RoboCasa 约定):有其一即视为容器。
_DOOR_JOINT_SUFFIXES = ("_doorhinge", "_leftdoorhinge", "_rightdoorhinge", "_door_joint", "_slidejoint")


class MyKitchen(Kitchen):
    def __init__(self, scene_dir=None, *args, **kwargs):
        self._scene_dir = scene_dir
        super().__init__(*args, **kwargs)

    def _setup_model(self):
        # ① 机器人初始化
        from robosuite.models.robots import PandaOmron

        for robot in self.robots:
            if isinstance(robot.robot_model, PandaOmron):
                robot.init_qpos = (
                    -0.01612974,
                    -1.03446714,
                    -0.02397936,
                    -2.27550888,
                    0.03932365,
                    1.51639493,
                    0.69615947,
                )
                robot.init_torso_qpos = np.array([0.0])

        # ② 从本地 scene/ 目录读取 YAML
        layout_config = self._load_yaml("config/layout.yaml")
        style_config = self._load_yaml("config/style.yaml")

        self.layout_id = 7
        self.style_id = 1
        self._curr_gen_fixtures = self._ep_meta.get("gen_textures")

        # ③ 创建 KitchenArena。RoboCasa 1.0.1 only accepts registry IDs here.
        # The repository copies are byte-for-byte identical to the official
        # layout007.yaml / style001.yaml assets, so these IDs preserve the
        # intended local scene while remaining compatible with current API.
        self.mujoco_arena = KitchenArena(
            layout_id=self.layout_id,
            style_id=self.style_id,
            rng=self.rng,
            enable_fixtures=self.enable_fixtures,
            clutter_mode=self.clutter_mode,
            update_fxtr_cfg_dict=self.update_fxtr_cfg_dict,
        )

        # ④ 后续代码与父类完全一致
        self.mujoco_arena.set_origin([0, 0, 0])
        CamUtils.set_cameras(self)

        # 添加世界坐标固定的俯视相机
        import xml.etree.ElementTree as ET
        overhead_cam = ET.Element(
            "camera",
            name="overhead_cam",
            pos="3 -2 3",
            fovy="60",
        )
        self.mujoco_arena.worldbody.append(overhead_cam)

        # 侧面相机：朝 +y 方向看
        side_cam = ET.Element(
            "camera",
            name="side_cam",
            pos="3 -4.5 1",
            quat="0.707 0.707 0 0",
            fovy="60",
        )
        self.mujoco_arena.worldbody.append(side_cam)

        if self.renderer == "mjviewer":
            camera_config = CamUtils.LAYOUT_CAMS.get(
                self.layout_id, CamUtils.DEFAULT_LAYOUT_CAM
            )
            self.renderer_config = {"cam_config": camera_config}

        self.fixture_cfgs = self.mujoco_arena.get_fixture_cfgs()
        self.fixtures = {cfg["name"]: cfg["model"] for cfg in self.fixture_cfgs}

        from robosuite.models.tasks import ManipulationTask

        self.model = ManipulationTask(
            mujoco_arena=self.mujoco_arena,
            mujoco_robots=[robot.robot_model for robot in self.robots],
            mujoco_objects=list(self.fixtures.values()),
            enable_multiccd=True,
            enable_sleeping_islands=False,
        )

    def _setup_kitchen_references(self):
        """从 objects.yaml 的 fixture_refs 部分注册家具引用"""
        super()._setup_kitchen_references()

        config = self._load_yaml("config/objects.yaml")
        if config is None:
            return

        for ref_cfg in config.get("fixture_refs", []):
            ref_name = ref_cfg["name"]
            fixture_type = FixtureType[ref_cfg["id"]]
            fn_kwargs = {"id": fixture_type}

            if "ref" in ref_cfg:
                parent = self._get_registered_fixture(ref_cfg["ref"])
                if parent is not None:
                    fn_kwargs["ref"] = parent

            self.register_fixture_ref(ref_name, fn_kwargs)

    def _get_obj_cfgs(self):
        """从 objects.yaml 读取物体配置，解析 fixture 字符串引用"""
        config = self._load_yaml("config/objects.yaml")
        if config is None:
            return []

        cfgs = []
        for obj_cfg in config.get("objects", []):
            cfg = dict(obj_cfg)
            placement = cfg.get("placement", {})

            # fixture: "counter" → self.fixture_refs["counter"] 实际对象
            if "fixture" in placement and isinstance(placement["fixture"], str):
                fxtr = self._get_registered_fixture(placement["fixture"])
                if fxtr is not None:
                    placement["fixture"] = fxtr

            # sample_region_kwargs.ref: "stove" → 实际对象
            region_kwargs = placement.get("sample_region_kwargs", {})
            if "ref" in region_kwargs and isinstance(region_kwargs["ref"], str):
                fxtr = self._get_registered_fixture(region_kwargs["ref"])
                if fxtr is not None:
                    region_kwargs["ref"] = fxtr

            cfgs.append(cfg)

        return cfgs

    def _check_success(self):
        return False

    def _get_registered_fixture(self, ref_name):
        """从 fixture_refs 中取出已注册的 fixture 对象"""
        ref_value = self.fixture_refs.get(ref_name)
        if ref_value is None:
            return None
        return ref_value[0] if isinstance(ref_value, tuple) else ref_value

    def _load_yaml(self, filename):
        path = os.path.join(self._scene_dir, filename)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # ========================================================
    # 给功能代码(per-home/容器/四宫格/segmentation)用的接口:
    #   - 工具(arm.py/move.py)用 env.sim(robosuite MjSim API)+ env.step,真控制
    #   - 功能代码用下面的 raw mujoco 模型/helper(和原自定义 env 同名),复用现有实现
    # 这些不改机器人行为,只是把 robosuite env 暴露成功能代码期望的形状。
    # ========================================================

    @property
    def raw_model(self):
        """raw mujoco.MjModel(功能代码用新版 mujoco API;不叫 model 避免撞 robosuite 的 self.model)。"""
        return self.sim.model._model

    @property
    def raw_data(self):
        """raw mujoco.MjData。"""
        return self.sim.data._data

    def _obj_joint_name(self, obj_name):
        bid = self.obj_body_id[obj_name]
        jadr = int(self.raw_model.body_jntadr[bid])
        return mujoco.mj_id2name(self.raw_model, mujoco.mjtObj.mjOBJ_JOINT, jadr)

    def get_object_pos(self, obj_name):
        return self.sim.data.body_xpos[self.obj_body_id[obj_name]].copy()

    def set_object_pos(self, obj_name, pos, quat=(1.0, 0.0, 0.0, 0.0)):
        """把物体(freejoint)瞬移到世界坐标 pos,清零速度(避免残留漂移)。"""
        pos = np.asarray(pos, dtype=float)
        jname = self._obj_joint_name(obj_name)
        self.sim.data.set_joint_qpos(jname, np.concatenate([pos, np.asarray(quat, dtype=float)]))
        jid = mujoco.mj_name2id(self.raw_model, mujoco.mjtObj.mjOBJ_JOINT, jname)
        dadr = int(self.raw_model.jnt_dofadr[jid])
        self.raw_data.qvel[dadr:dadr + 6] = 0.0
        self.sim.forward()

    def get_body_pos(self, body_name, fallback=None):
        bid = mujoco.mj_name2id(self.raw_model, mujoco.mjtObj.mjOBJ_BODY, body_name)
        if bid < 0 and fallback:
            bid = mujoco.mj_name2id(self.raw_model, mujoco.mjtObj.mjOBJ_BODY, fallback)
        if bid < 0:
            return np.zeros(3)
        return self.sim.data.body_xpos[bid].copy()

    def get_body_quat(self, body_name, fallback=None):
        bid = mujoco.mj_name2id(self.raw_model, mujoco.mjtObj.mjOBJ_BODY, body_name)
        if bid < 0 and fallback:
            bid = mujoco.mj_name2id(self.raw_model, mujoco.mjtObj.mjOBJ_BODY, fallback)
        if bid < 0:
            return np.array([1.0, 0.0, 0.0, 0.0])
        return self.sim.data.body_xquat[bid].copy()

    # ---- 容器(带门柜)----
    def _container_door_joint_ids(self, container):
        jids = []
        for suffix in _DOOR_JOINT_SUFFIXES:
            jid = mujoco.mj_name2id(self.raw_model, mujoco.mjtObj.mjOBJ_JOINT, container + suffix)
            if jid >= 0:
                jids.append(jid)
        return jids

    def open_container_door(self, container):
        """转开容器门 joint 到 range abs 大端,返回开的门 joint 名列表。"""
        opened = []
        for jid in self._container_door_joint_ids(container):
            qadr = self.raw_model.jnt_qposadr[jid]
            lo, hi = float(self.raw_model.jnt_range[jid][0]), float(self.raw_model.jnt_range[jid][1])
            self.raw_data.qpos[qadr] = hi if abs(hi) >= abs(lo) else lo
            opened.append(mujoco.mj_id2name(self.raw_model, mujoco.mjtObj.mjOBJ_JOINT, jid))
        if opened:
            self.sim.forward()
        return opened

    def free_points(self, spacing=0.4, margin=0.32):
        """生成"可站立点"网格:房间地板范围内、避开地面层大件家具占地(+机器人半径 margin)。

        用途:① 探索遍历的站位 ② 抓取接近位姿的候选(物体坐标附近取最近的 free-point 站过去)。
        判据:fixture 底部(pos_z - size_z/2)< 0.5 = 挡地(counter/island/冰箱等);高墙柜(z≈1.85)不挡,
        机器人可站其下。只算占地面积够大的(>0.3㎡)避免被上百个碎小子件过度排除。size 是全长,half=size/2。
        """
        floor = self.fixtures.get("floor_room")
        if floor is None:
            return []
        fp = np.asarray(floor.pos, dtype=float)
        fs = np.asarray(floor.size, dtype=float)
        x0, x1 = fp[0] - fs[0] / 2 + margin, fp[0] + fs[0] / 2 - margin
        y0, y1 = fp[1] - fs[1] / 2 + margin, fp[1] + fs[1] / 2 - margin
        blockers = []
        for name, fx in self.fixtures.items():
            if name.startswith("floor"):
                continue
            p = np.asarray(fx.pos, dtype=float)
            s = np.asarray(fx.size, dtype=float)
            if (p[2] - s[2] / 2) >= 0.5:        # 高柜等不挡地
                continue
            if name.startswith("wall") and min(s[0], s[1]) < 0.15:
                pass  # 薄墙也当障碍(排除贴墙点)
            elif s[0] * s[1] < 0.3:             # 太小的子件跳过
                continue
            blockers.append((p[0], p[1], s[0] / 2, s[1] / 2))
        pts = []
        y = y0
        while y <= y1:
            x = x0
            while x <= x1:
                free = True
                for bx, by, hx, hy in blockers:
                    if abs(x - bx) < hx + margin and abs(y - by) < hy + margin:
                        free = False
                        break
                if free:
                    pts.append([round(float(x), 3), round(float(y), 3)])
                x += spacing
            y += spacing
        return pts

    def list_containers(self, min_height=1.2):
        """有门 joint 且足够高(相机够不到内部)的容器 [{name,pos}],按 x 排序。"""
        out = []
        for name, fxtr in self.fixtures.items():
            if not self._container_door_joint_ids(name):
                continue
            pos = np.asarray(fxtr.pos, dtype=float)
            if float(pos[2]) < float(min_height):
                continue
            out.append({"name": name, "pos": pos.tolist()})
        out.sort(key=lambda c: (c["pos"][0], c["pos"][1]))
        return out

    # ---- segmentation 渲染(懒加载独立 renderer)----
    def render_segmentation(self, camera_name="overhead_cam", width=160, height=120):
        if getattr(self, "_seg_renderer", None) is None:
            self._seg_renderer = mujoco.Renderer(self.raw_model, height=height, width=width)
            self._seg_renderer.enable_segmentation_rendering()
        cid = mujoco.mj_name2id(self.raw_model, mujoco.mjtObj.mjOBJ_CAMERA, camera_name)
        self._seg_renderer.update_scene(self.raw_data, camera=cid if cid >= 0 else -1)
        return self._seg_renderer.render()
