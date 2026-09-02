"""MotrixSim + 3DGS simulation environment for Franka Panda scene."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional, Tuple

_PROJECT_CUDA_HOME = Path(__file__).resolve().parents[2] / ".cuda" / "v12.4"
_CUDA_HOME = os.environ.get(
    "CUDA_HOME",
    str(_PROJECT_CUDA_HOME) if _PROJECT_CUDA_HOME.is_dir() else "/usr/local/cuda-12.4",
)
_NVCC = os.path.join(_CUDA_HOME, "bin", "nvcc.exe" if os.name == "nt" else "nvcc")
if os.path.isfile(_NVCC):
    os.environ["CUDA_HOME"] = _CUDA_HOME
    _cuda_bin = os.path.join(_CUDA_HOME, "bin")
    _lib64 = os.path.join(_CUDA_HOME, "lib", "x64") if os.name == "nt" else os.path.join(_CUDA_HOME, "lib64")
    if _cuda_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _cuda_bin + os.pathsep + os.environ.get("PATH", "")
    if _lib64 not in os.environ.get("LD_LIBRARY_PATH", ""):
        os.environ["LD_LIBRARY_PATH"] = _lib64 + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")

import numpy as np
import torch


def configure_torch_cuda_arch() -> None:
    if os.environ.get("TORCH_CUDA_ARCH_LIST"):
        return
    if not torch.cuda.is_available():
        return
    archs = {
        f"{major}.{minor}"
        for device_idx in range(torch.cuda.device_count())
        for major, minor in [torch.cuda.get_device_capability(device_idx)]
    }
    if archs:
        os.environ["TORCH_CUDA_ARCH_LIST"] = ";".join(sorted(archs))


configure_torch_cuda_arch()

import motrixsim as mx
from gaussian_renderer import BatchSplatConfig, MtxBatchSplatRenderer
from motrixsim import SceneData, forward_kinematic

from .gs_config import GSConfig


class SimEnv:
    def __init__(
        self,
        model_xml: str,
        gs_cfg: GSConfig,
        batch_size: int = 1,
        enable_renderers: bool = True,
    ):
        self._gs_cfg = gs_cfg
        self._batch_size = batch_size

        scene = self._load_scene(model_xml, gs_cfg)
        self.model = scene.build()
        self._configure_navigation_camera_tracking(gs_cfg)
        self.data = SceneData(self.model, batch=(batch_size,))

        self._gs_renderer = None
        self._bg_renderer = None
        if enable_renderers:
            self._setup_renderers(gs_cfg, batch_size)
        self._bg_imgs: Dict[Tuple[int, int, int], object] = {}

        self._link_name_to_idx: Dict[str, int] = {
            name: idx for idx, name in enumerate(self.model.link_names)
        }
        self.scene_objects: Dict[str, str] = dict(gs_cfg.scene_objects)
        self.scene_fixtures: Dict[str, dict] = dict(gs_cfg.scene_fixtures)
        self.object_names = tuple(self.scene_objects.keys())

        self._grasped_object = None

        self._composite_renderer = None
        self._composite_cfg = gs_cfg.composite_mesh_objects if gs_cfg.composite_mesh_objects else []
        self._composite_scene_xml = Path(gs_cfg.scene_xml) if gs_cfg.composite_mesh_objects else None
        self._composite_warning_reported = False
        self._render_debug_reported = False

        self.data.reset(self.model)
        forward_kinematic(self.model, self.data)
        self._camera_fixed_overrides: Dict[int, Tuple[np.ndarray, np.ndarray, float]] = {}
        self._camera_look_at_overrides: Dict[int, Tuple[np.ndarray, float]] = {}
        self._setup_camera_overrides()

    def _load_scene(self, model_xml: str, gs_cfg: GSConfig):
        if gs_cfg.scene_kind == "navigation":
            scene = mx.msd.from_file(model_xml)
            if not gs_cfg.robot_xml:
                raise ValueError("navigation scene requires robot_xml")
            robot = mx.msd.from_file(gs_cfg.robot_xml)
            if gs_cfg.follower_camera_pos is not None:
                x, y, z = gs_cfg.follower_camera_pos
                camera_mjcf = f"""<mujoco model="camera">
  <worldbody>
    <camera name="follower" pos="{x:g} {y:g} {z:g}"
      xyaxes="0 -1 0 0 0 1" trackposspeed="2" trackrotspeed="2" />
  </worldbody>
</mujoco>"""
                robot.attach(mx.msd.from_str(camera_mjcf), gs_cfg.base_link_name)
            scene.attach(robot)
            return scene

        scene = mx.msd.from_file(model_xml)
        try:
            floor_xml = """<mujoco model="floor">
  <worldbody>
    <geom name="floor" type="plane" size="10 10 0.01" rgba="0.5 0.5 0.5 1" contype="1" conaffinity="1"/>
  </worldbody>
</mujoco>"""
            scene.attach(mx.msd.from_str(floor_xml))
        except (RuntimeError, Exception):
            pass
        return scene

    def _configure_navigation_camera_tracking(self, gs_cfg: GSConfig) -> None:
        if gs_cfg.scene_kind != "navigation" or gs_cfg.follower_camera_pos is None:
            return
        try:
            camera = self.model.cameras["follower"]
            camera.rotation_track = "look_at_link"
            camera.position_track = "fixed_local"
            camera.track_target_link = self.model.get_link(gs_cfg.base_link_name)
        except Exception as exc:
            raise RuntimeError(f"failed to configure navigation follower camera: {exc}") from exc

    def _setup_renderers(self, gs_cfg: GSConfig, batch_size: int) -> None:
        body_gaussians = self._filter_body_gaussians(gs_cfg.body_gaussians)
        if body_gaussians:
            print(f"Loaded {len(body_gaussians)} body 3DGS assets: {', '.join(sorted(body_gaussians))}", flush=True)
            self._gs_renderer = MtxBatchSplatRenderer(
                BatchSplatConfig(body_gaussians=body_gaussians, background_ply=None, minibatch=batch_size),
                self.model,
            )
        elif gs_cfg.body_gaussians:
            print("No body 3DGS assets matched model link names; rendering background 3DGS only.", flush=True)
        else:
            print("No body 3DGS assets configured; rendering background 3DGS only.", flush=True)
        if gs_cfg.background_ply:
            self._bg_renderer = MtxBatchSplatRenderer(
                BatchSplatConfig(body_gaussians={}, background_ply=gs_cfg.background_ply, minibatch=batch_size),
                self.model,
            )

    def _filter_body_gaussians(self, body_gaussians: Dict[str, str]) -> Dict[str, str]:
        link_names = set(self.model.link_names)
        return {
            name: path
            for name, path in body_gaussians.items()
            if name in link_names and os.path.exists(path)
        }

    def _ensure_composite_renderer(self, width: int, height: int) -> bool:
        if self._composite_renderer is not None and self._composite_renderer.width == width and self._composite_renderer.height == height:
            return True
        if not self._composite_cfg or not self._composite_scene_xml:
            return False
        from .mesh_compositor import PyrenderMeshCompositor

        if self._composite_renderer is not None:
            try:
                self._composite_renderer.close()
            except Exception:
                pass
        try:
            self._composite_renderer = PyrenderMeshCompositor(
                self._composite_cfg,
                width=width,
                height=height,
                scene_xml=self._composite_scene_xml,
            )
            print(f"Composite mesh rendering enabled for {len(self._composite_cfg)} object(s) at {width}x{height}", flush=True)
            return True
        except Exception as exc:
            print(f"Composite mesh renderer init failed: {exc}", flush=True)
            self._composite_renderer = None
            return False

    def object_link(self, object_name: str) -> str:
        return self.scene_objects.get(object_name, object_name)

    def step(self, n: int = 1) -> None:
        for _ in range(n):
            mx.step(self.model, self.data)

    def forward_kinematic(self) -> None:
        forward_kinematic(self.model, self.data)

    def _setup_camera_overrides(self) -> None:
        """Pre-compute corrected camera poses for cameras with wrong MJCF orientations."""
        scene_center = np.array(
            [-1.25, 0.73, 0.52] if self._gs_cfg.scene_kind == "navigation" else [0.0, 0.0, 0.5],
            dtype=np.float32,
        )
        fixed_camera_configs = {
            "overhead_cam": {"pos": [0.0, 0.0, 4.0], "fovy": 60.0},
        }
        if self._gs_cfg.scene_kind == "navigation":
            fixed_camera_configs.update({
                "base_cam": {"pos": [-6.25, 0.73, 1.70], "fovy": 60.0},
                "chassis_cam": {"pos": [-1.25, -5.00, 1.70], "fovy": 60.0},
            })
        look_at_camera_configs = {
            "head_cam": {"target": scene_center, "fovy": 70.0},
            "right_arm_cam": {"target": scene_center, "fovy": 60.0},
            "left_arm_cam": {"target": scene_center, "fovy": 60.0},
            "chassis_cam": {"target": scene_center, "fovy": 70.0},
            "base_cam": {"target": scene_center, "fovy": 70.0},
            "right_ee_cam": {"target": scene_center, "fovy": 60.0},
            "left_ee_cam": {"target": scene_center, "fovy": 60.0},
        }
        for i, cam in enumerate(self.model.cameras.cameras):
            name = getattr(cam, "name", "")
            if name in fixed_camera_configs:
                cfg = fixed_camera_configs[name]
                pos = np.array(cfg["pos"], dtype=np.float32)
                quat_xyzw = self._look_at_quat_xyzw(pos, scene_center)
                self._camera_fixed_overrides[i] = (pos[None], quat_xyzw[None], cfg["fovy"])
            elif name in look_at_camera_configs:
                cfg = look_at_camera_configs[name]
                self._camera_look_at_overrides[i] = (
                    np.array(cfg["target"], dtype=np.float32),
                    float(cfg["fovy"]),
                )

    @classmethod
    def _look_at_quat_xyzw(cls, pos: np.ndarray, target: np.ndarray) -> np.ndarray:
        look = np.asarray(target, dtype=np.float32) - np.asarray(pos, dtype=np.float32)
        look = look / (np.linalg.norm(look) + 1e-12)
        world_up = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        right = np.cross(look, world_up)
        if np.linalg.norm(right) < 1e-6:
            right = np.cross(look, np.array([0.0, 1.0, 0.0], dtype=np.float32))
        right = right / (np.linalg.norm(right) + 1e-12)
        up = np.cross(right, look)
        rot_mat = np.column_stack([right, up, -look]).astype(np.float32)
        quat_wxyz = cls._matrix_to_quat_wxyz(rot_mat)
        return quat_wxyz[[1, 2, 3, 0]]

    @staticmethod
    def _matrix_to_quat_wxyz(mat: np.ndarray) -> np.ndarray:
        m = np.asarray(mat, dtype=np.float64)
        trace = float(np.trace(m))
        if trace > 0.0:
            s = np.sqrt(trace + 1.0) * 2.0
            w = 0.25 * s
            x = (m[2, 1] - m[1, 2]) / s
            y = (m[0, 2] - m[2, 0]) / s
            z = (m[1, 0] - m[0, 1]) / s
        elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
            s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
            w = (m[2, 1] - m[1, 2]) / s
            x = 0.25 * s
            y = (m[0, 1] + m[1, 0]) / s
            z = (m[0, 2] + m[2, 0]) / s
        elif m[1, 1] > m[2, 2]:
            s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
            w = (m[0, 2] - m[2, 0]) / s
            x = (m[0, 1] + m[1, 0]) / s
            y = 0.25 * s
            z = (m[1, 2] + m[2, 1]) / s
        else:
            s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
            w = (m[1, 0] - m[0, 1]) / s
            x = (m[0, 2] + m[2, 0]) / s
            y = (m[1, 2] + m[2, 1]) / s
            z = 0.25 * s
        q = np.array([w, x, y, z], dtype=np.float64)
        return q / (np.linalg.norm(q) + 1e-12)

    def get_link_poses(self) -> np.ndarray:
        poses = self.model.get_link_poses(self.data)
        return np.asarray(poses)

    def get_body_xpos(self, body_name: str) -> np.ndarray:
        idx = self._link_name_to_idx[body_name]
        poses = self.model.get_link_poses(self.data)
        if poses.ndim == 3:
            return np.asarray(poses[0, idx, :3])
        return np.asarray(poses[idx, :3])

    def get_body_xquat(self, body_name: str) -> np.ndarray:
        idx = self._link_name_to_idx[body_name]
        poses = self.model.get_link_poses(self.data)
        if poses.ndim == 3:
            return np.asarray(poses[0, idx, 3:7])
        return np.asarray(poses[idx, 3:7])

    def get_camera_pose(self, cam_id: int = 0) -> Tuple[np.ndarray, np.ndarray, float]:
        if cam_id in self._camera_fixed_overrides:
            return self._camera_fixed_overrides[cam_id]
        cam = self.model.cameras[int(cam_id)]
        pose = np.asarray(cam.get_pose(self.data), dtype=np.float32)
        if pose.ndim == 1:
            pose = pose[None, :]
        if pose.ndim >= 3:
            pose = pose.reshape(pose.shape[0], -1, pose.shape[-1])[:, 0, :]
        pos = pose[:, :3].astype(np.float32)
        quat = pose[:, 3:7].astype(np.float32)
        if cam_id in self._camera_look_at_overrides:
            target, fovy = self._camera_look_at_overrides[cam_id]
            quat = np.stack([self._look_at_quat_xyzw(p, target) for p in pos], axis=0)
            return pos, quat.astype(np.float32), fovy
        quat /= np.linalg.norm(quat, axis=1, keepdims=True) + 1e-12
        return pos, quat, float(getattr(cam, "fovy", 45.0))

    def render_frame(
        self,
        cam_id: int = 0,
        width: int = 640,
        height: int = 480,
        cache_background: bool = True,
    ) -> np.ndarray:
        if self._gs_renderer is None and self._bg_renderer is None:
            return np.zeros((height, width, 3), dtype=np.uint8)
        if self._composite_cfg:
            cache_background = False

        self.forward_kinematic()
        link_poses = self.model.get_link_poses(self.data)
        body_pos = link_poses[..., :3]
        body_quat = link_poses[..., 3:7]

        cam_pos, cam_quat, fovy = self.get_camera_pose(cam_id)
        cam_xmat = np.stack([self._quat_to_xmat(q) for q in cam_quat], axis=0)

        active_renderer = self._gs_renderer or self._bg_renderer
        device = active_renderer.device
        cam_pos_t = torch.from_numpy(cam_pos[:, None, :]).to(device=device, dtype=torch.float32)
        cam_xmat_t = torch.from_numpy(cam_xmat[:, None, :, :]).to(device=device, dtype=torch.float32)
        fovy_np = np.full((cam_pos.shape[0], 1), fovy, dtype=np.float32)

        bg_imgs = None
        bg_depth_t = None
        bg_cache_key = (int(cam_id), int(width), int(height))
        if self._bg_renderer is not None and cache_background:
            bg_imgs = self._bg_imgs.get(bg_cache_key)
        if self._bg_renderer is not None and bg_imgs is None:
            bg_gsb = self._bg_renderer.batch_update_gaussians(body_pos, body_quat)
            bg_imgs, bg_depth_t = self._bg_renderer.batch_env_render(
                bg_gsb, cam_pos_t, cam_xmat_t, int(height), int(width), fovy_np
            )
            if cache_background:
                self._bg_imgs[bg_cache_key] = bg_imgs

        if self._gs_renderer is not None:
            gsb = self._gs_renderer.batch_update_gaussians(body_pos, body_quat)
            rgb_t, depth_t = self._gs_renderer.batch_env_render(
                gsb, cam_pos_t, cam_xmat_t, int(height), int(width), fovy_np, bg_imgs=bg_imgs
            )
        elif bg_imgs is not None:
            rgb_t = bg_imgs
            depth_t = bg_depth_t
        else:
            return np.zeros((height, width, 3), dtype=np.uint8)
        rgb = rgb_t.detach().cpu().numpy() if isinstance(rgb_t, torch.Tensor) else np.asarray(rgb_t)
        if os.environ.get("FQPLANNER_RENDER_DEBUG") == "1" and not self._render_debug_reported:
            finite = np.isfinite(rgb)
            print(
                "3DGS render tensor: "
                f"shape={rgb.shape} min={np.nanmin(rgb):.6g} max={np.nanmax(rgb):.6g} "
                f"mean={np.nanmean(rgb):.6g} finite={finite.mean():.6f}",
                flush=True,
            )
            self._render_debug_reported = True
        rgb = rgb[0, 0]
        rgb_u8 = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)

        if self._composite_cfg and depth_t is not None and self._ensure_composite_renderer(int(width), int(height)):
            gs_depth = depth_t.detach().cpu().numpy() if isinstance(depth_t, torch.Tensor) else np.asarray(depth_t)
            gs_depth = gs_depth[0, 0]
            try:
                camera_pose = np.concatenate([cam_pos[0], cam_quat[0]], axis=0)
                mesh_rgb, mesh_depth = self._composite_renderer.render(
                    self.model,
                    self.data,
                    cam_id,
                    int(width),
                    int(height),
                    camera_pose=camera_pose,
                    fovy=fovy,
                )
                from .mesh_compositor import composite_rgb_depth
                rgb_u8 = composite_rgb_depth(rgb_u8, gs_depth.astype(np.float32), mesh_rgb, mesh_depth)
            except Exception as exc:
                if not self._composite_warning_reported:
                    print(f"Composite mesh render failed: {exc}", flush=True)
                    self._composite_warning_reported = True

        return rgb_u8

    @staticmethod
    def _quat_to_xmat(quat_xyzw: np.ndarray) -> np.ndarray:
        x, y, z, w = np.asarray(quat_xyzw, dtype=np.float64)
        n = x * x + y * y + z * z + w * w
        if n < 1e-12:
            return np.eye(3, dtype=np.float32)
        s = 2.0 / n
        xx, yy, zz = x * x * s, y * y * s, z * z * s
        xy, xz, yz = x * y * s, x * z * s, y * z * s
        wx, wy, wz = w * x * s, w * y * s, w * z * s
        return np.array(
            [
                [1.0 - yy - zz, xy - wz, xz + wy],
                [xy + wz, 1.0 - xx - zz, yz - wx],
                [xz - wy, yz + wx, 1.0 - xx - yy],
            ],
            dtype=np.float32,
        )

    @property
    def grasped_object(self) -> Optional[str]:
        return self._grasped_object

    @grasped_object.setter
    def grasped_object(self, value: Optional[str]):
        self._grasped_object = value
