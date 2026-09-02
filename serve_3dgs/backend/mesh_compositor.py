"""Pyrender-based textured mesh compositing for 3DGS navigation scenes."""

from __future__ import annotations

import os
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.spatial.transform import Rotation


def composite_rgb_depth(
    gs_rgb: np.ndarray,
    gs_depth: np.ndarray,
    mesh_rgb: np.ndarray,
    mesh_depth: np.ndarray,
) -> np.ndarray:
    gs_d = np.asarray(gs_depth, dtype=np.float32).squeeze()
    mesh_d = np.asarray(mesh_depth, dtype=np.float32).squeeze()
    valid_mesh = np.isfinite(mesh_d)
    valid_gs = np.isfinite(gs_d) & (gs_d > 1e-6)
    use_mesh = valid_mesh & ((~valid_gs) | (mesh_d < gs_d))
    out = gs_rgb.copy()
    out[use_mesh] = mesh_rgb[use_mesh]
    return out


class PyrenderMeshCompositor:
    def __init__(
        self,
        object_cfgs: List[dict],
        width: int,
        height: int,
        scene_xml: Optional[Path] = None,
    ):
        import pyrender
        import trimesh

        self.pyrender = pyrender
        self.width = int(width)
        self.height = int(height)
        self.scene = pyrender.Scene(bg_color=[0, 0, 0, 0], ambient_light=[0.55, 0.55, 0.55])
        self.renderer = pyrender.OffscreenRenderer(viewport_width=self.width, viewport_height=self.height)
        self.camera = pyrender.PerspectiveCamera(yfov=np.deg2rad(45.0), aspectRatio=float(width) / float(height))
        self.camera_node = self.scene.add(self.camera, pose=np.eye(4, dtype=np.float32))
        light = pyrender.DirectionalLight(color=np.ones(3), intensity=2.0)
        self.light_node = self.scene.add(light, pose=np.eye(4, dtype=np.float32))
        self.objects: list = []
        mjcf_assets = self._parse_mjcf_assets(scene_xml) if scene_xml is not None else {}

        for cfg in object_cfgs:
            cfg = dict(cfg)
            if "mesh" not in cfg:
                cfg.update(self._object_cfg_from_mjcf(cfg["link"], mjcf_assets))
            mesh_path = Path(cfg["mesh"])
            if not mesh_path.is_absolute():
                mesh_path = self._resolve_path(mesh_path, scene_xml)
            tri_scene = trimesh.load(mesh_path, force="scene")
            scale = np.asarray(cfg.get("scale", [1.0, 1.0, 1.0]), dtype=np.float32)
            if scale.shape == ():
                scale = np.repeat(float(scale), 3).astype(np.float32)
            tri_mesh = self._trimesh_from_scene(tri_scene, trimesh)
            if not np.allclose(scale, 1.0):
                tri_mesh.apply_scale(scale)
            mesh = pyrender.Mesh.from_trimesh(tri_mesh, smooth=True)
            node = self.scene.add(mesh, pose=np.eye(4, dtype=np.float32))
            local_pose = np.asarray(cfg.get("local_pose", np.eye(4, dtype=np.float32)), dtype=np.float32)
            self.objects.append({"link": str(cfg["link"]), "node": node, "local_pose": local_pose})

    def close(self) -> None:
        self.renderer.delete()

    def render(
        self,
        model,
        data,
        cam_id: int,
        width: int,
        height: int,
        system_camera=None,
        camera_pose=None,
        fovy: Optional[float] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        if int(width) != self.width or int(height) != self.height:
            raise ValueError("PyrenderMeshCompositor resolution is fixed after construction")

        if camera_pose is not None:
            cam_pose = np.asarray(camera_pose, dtype=np.float32)
            fovy = 45.0 if fovy is None else float(fovy)
        elif int(cam_id) == -1:
            if system_camera is None:
                raise ValueError("system_camera must be provided when rendering composite camera id -1")
            cam_pose = np.asarray(system_camera.pose, dtype=np.float32)
            fovy = 45.0
        else:
            cam = model.cameras[int(cam_id)]
            cam_pose = np.asarray(cam.get_pose(data), dtype=np.float32)
            fovy = float(getattr(cam, "fovy", 45.0)) if fovy is None else float(fovy)
        cam_mat = self._pose_to_matrix(cam_pose)
        self.camera.yfov = np.deg2rad(fovy)
        self.scene.set_pose(self.camera_node, pose=cam_mat)
        self.scene.set_pose(self.light_node, pose=cam_mat)

        for obj in self.objects:
            try:
                pose = np.asarray(model.get_link(obj["link"]).get_pose(data), dtype=np.float32)
            except Exception:
                continue
            self.scene.set_pose(obj["node"], pose=self._pose_to_matrix(pose) @ obj["local_pose"])

        rgb, depth = self.renderer.render(self.scene, flags=self.pyrender.RenderFlags.RGBA)
        rgb = np.asarray(rgb[..., :3], dtype=np.uint8)
        depth = np.asarray(depth, dtype=np.float32)
        depth[depth <= 0.0] = np.inf
        return rgb, depth

    @staticmethod
    def _pose_to_matrix(pose: np.ndarray) -> np.ndarray:
        pose = np.asarray(pose, dtype=np.float32).reshape(-1)
        mat = np.eye(4, dtype=np.float32)
        mat[:3, :3] = Rotation.from_quat(pose[3:7]).as_matrix().astype(np.float32)
        mat[:3, 3] = pose[:3]
        return mat

    @staticmethod
    def _trimesh_from_scene(tri_scene, trimesh):
        geometries = list(tri_scene.geometry.values())
        if not geometries:
            raise ValueError("Composite mesh file does not contain any geometry")
        if len(geometries) == 1:
            return geometries[0].copy()
        return trimesh.util.concatenate([geom.copy() for geom in geometries])

    @staticmethod
    def _resolve_path(path: Path, scene_xml: Optional[Path]) -> Path:
        if scene_xml is not None and not path.is_absolute():
            return (scene_xml.parent / path).resolve()
        return path.resolve()

    @staticmethod
    def _parse_mjcf_assets(scene_xml: Path) -> dict:
        root = ET.parse(scene_xml).getroot()
        assets = {}
        materials = {}
        for material in root.findall("./asset/material"):
            name = material.get("name")
            rgba = material.get("rgba")
            if name and rgba:
                values = [float(x) for x in rgba.split()]
                if len(values) == 3:
                    values.append(1.0)
                materials[name] = values[:4]
        for mesh in root.findall("./asset/mesh"):
            name = mesh.get("name")
            if not name:
                continue
            scale_text = mesh.get("scale", "1 1 1")
            scale = [float(x) for x in scale_text.split()]
            if len(scale) == 1:
                scale = scale * 3
            assets[name] = {"file": mesh.get("file", ""), "scale": scale[:3]}
        bodies = {body.get("name"): body for body in root.findall(".//body") if body.get("name")}
        compiler = root.find("./compiler")
        meshdir = compiler.get("meshdir", "") if compiler is not None else ""
        return {"assets": assets, "materials": materials, "bodies": bodies, "meshdir": meshdir, "base_dir": scene_xml.parent}

    @staticmethod
    def _object_cfg_from_mjcf(link_name: str, parsed: dict) -> dict:
        body = parsed["bodies"].get(link_name)
        if body is None:
            raise ValueError(f"Could not find MJCF body for composite object: {link_name}")
        visual_geom = None
        for geom in body.findall("./geom"):
            if geom.get("type") == "mesh" and geom.get("contype", "1") == "0":
                visual_geom = geom
                break
        if visual_geom is None:
            for geom in body.findall("./geom"):
                if geom.get("type") == "mesh":
                    visual_geom = geom
                    break
        if visual_geom is None or not visual_geom.get("mesh"):
            raise ValueError(f"Could not find mesh geom for composite object: {link_name}")

        mesh_name = visual_geom.get("mesh")
        asset = parsed["assets"].get(mesh_name)
        if asset is None:
            raise ValueError(f"Could not find mesh asset '{mesh_name}' for composite object: {link_name}")

        local_pose = np.eye(4, dtype=np.float32)
        if visual_geom.get("pos"):
            local_pose[:3, 3] = np.asarray([float(x) for x in visual_geom.get("pos").split()], dtype=np.float32)
        if visual_geom.get("quat"):
            q = np.asarray([float(x) for x in visual_geom.get("quat").split()], dtype=np.float32)
            local_pose[:3, :3] = Rotation.from_quat(q, scalar_first=True).as_matrix().astype(np.float32)

        color = None
        if visual_geom.get("material"):
            color = parsed["materials"].get(visual_geom.get("material"))
        if color is None and visual_geom.get("rgba"):
            color = [float(x) for x in visual_geom.get("rgba").split()]
            if len(color) == 3:
                color.append(1.0)

        mesh_file = (Path(parsed["base_dir"]) / parsed["meshdir"] / asset["file"]).resolve()
        return {"mesh": mesh_file.as_posix(), "scale": asset["scale"], "local_pose": local_pose, "color": color}
