"""3DGS asset path configuration."""

import json
import yaml
from pathlib import Path
from typing import Dict, Optional, Tuple

FQPLANNER_ROOT = Path(__file__).resolve().parents[2]
NAV_ASSET_DIR = FQPLANNER_ROOT / "assets" / "scene_3dgs"
DEFAULT_NAV_CONFIG = NAV_ASSET_DIR / "config.json"
DEFAULT_DIRECT_VIEWER_CAMERAS = ("overhead_cam", "head_cam", "right_arm_cam", "left_arm_cam")
DEFAULT_NAV_VIEWER_CAMERAS = ("follower", "head_cam", "right_arm_cam", "left_arm_cam")
ASSETS_CONFIG_PATH = FQPLANNER_ROOT / "assets" / "config.yaml"


def load_assets_config() -> dict:
    """Load assets/config.yaml, return empty dict if not found."""
    if ASSETS_CONFIG_PATH.is_file():
        with open(ASSETS_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


ROBOT_ASSETS_DIR = FQPLANNER_ROOT / "assets"


class GSConfig:
    """Manages 3DGS asset paths for MotrixSim backend."""

    def __init__(
        self,
        assets_dir: Optional[str] = None,
        scene: str = "",
        robot_gs_dir: Optional[str] = None,
        scene_config: Optional[str] = None,
        robot_name: str = "",
    ):
        # load global assets/config.yaml as defaults
        _global = load_assets_config()

        self.robot_name = robot_name or _global.get("robot", "xlerobot")
        scene = scene or _global.get("scene", "robot_nav")
        self.scene = scene
        self._assets_dir = Path(assets_dir) if assets_dir else None
        self._robot_gs_dir = Path(robot_gs_dir) if robot_gs_dir else None
        self.scene_kind = "direct"
        self.robot_xml: Optional[str] = None
        self.follower_camera_pos: Optional[Tuple[float, float, float]] = None
        self.scene_objects: Dict[str, str] = {}
        self.scene_fixtures: Dict[str, dict] = {}
        self.composite_mesh_objects: list = []

        # resolve robot xml path and load robot-specific config
        self._robot_dir = ROBOT_ASSETS_DIR / self.robot_name
        self._robot_config: dict = {}
        if self._robot_dir.is_dir():
            robot_xml_path = self._robot_dir / f"{self.robot_name.lower()}.xml"
            if not robot_xml_path.exists():
                robot_xml_path = self._robot_dir / f"{self.robot_name}.xml"
            self.robot_xml = robot_xml_path.as_posix() if robot_xml_path.exists() else None
            robot_cfg_path = self._robot_dir / "config.yaml"
            if robot_cfg_path.is_file():
                with open(robot_cfg_path, "r", encoding="utf-8") as f:
                    self._robot_config = yaml.safe_load(f) or {}

        self.base_link_name = self._robot_config.get("base_link", "Base_Link")
        self._mobile_base = self._robot_config.get("mobile_base", False)
        _viewer_cameras = _global.get("viewer_cameras")
        self.default_viewer_cameras = (
            tuple(_viewer_cameras) if _viewer_cameras else DEFAULT_DIRECT_VIEWER_CAMERAS
        )

        if scene == "robot_nav" or (scene and scene.endswith(".json")):
            config_path = Path(scene_config or scene)
            if str(config_path) == "robot_nav":
                config_path = DEFAULT_NAV_CONFIG
            self._load_navigation_config(config_path)
        elif scene == "robot_only":
            self.mjcf_path = self.robot_xml or str(FQPLANNER_ROOT / "assets" / "xlerobot" / "xlerobot.xml")
            self._background_ply = None
        else:
            self.mjcf_path = scene
            self._background_ply = None

        # composite mesh overlay (Hunyuan3D objects). Default OFF via assets/config.yaml
        # `composite_mesh` — it reloads meshes every frame (slow) and currently fails
        # (hunyuan_bottle_body missing). Set composite_mesh: true to enable.
        if not _global.get("composite_mesh", False):
            self.composite_mesh_objects = []

    def _resolve_navigation_path(self, path: str | Path, base_dir: Path) -> Path:
        path = Path(path)
        return path if path.is_absolute() else base_dir / path

    def _load_navigation_config(self, config_path: Path) -> None:
        config_path = self._resolve_navigation_path(config_path, NAV_ASSET_DIR)
        with config_path.open("r", encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError(f"navigation scene config must be a JSON object: {config_path}")

        scene_path = config.get("scene")
        if not scene_path:
            raise ValueError(f"navigation scene config missing 'scene': {config_path}")

        self.scene_kind = "navigation"
        config_dir = config_path.parent
        self.mjcf_path = self._resolve_navigation_path(scene_path, config_dir).as_posix()
        self.robot_xml = (self.robot_xml
                          or (FQPLANNER_ROOT / "assets" / "xlerobot" / "xlerobot.xml").as_posix())
        # follower camera only for mobile base robots
        if self._mobile_base:
            self.follower_camera_pos = (-2.0, 0.0, 1.0)
        robot_cameras = self._robot_config.get("cameras")
        if robot_cameras:
            self.default_viewer_cameras = tuple(robot_cameras)
        elif self._mobile_base:
            self.default_viewer_cameras = DEFAULT_NAV_VIEWER_CAMERAS

        scene_gaussians = config.get("scene_gaussians", {}) or {}
        scene_ply = scene_gaussians.get("scene")
        self._background_ply = (
            self._resolve_navigation_path(scene_ply, config_dir).as_posix()
            if scene_ply
            else None
        )
        self.scene_objects = self._parse_scene_objects(
            config.get("objects") or config.get("scene_objects") or []
        )
        self.scene_fixtures = self._parse_scene_fixtures(config.get("fixtures") or {})
        self.composite_mesh_objects = config.get("composite_mesh_objects") or []

        robot_gs_dir = config.get("robot_gs_dir")
        if robot_gs_dir and self._robot_gs_dir is None:
            candidate = self._resolve_navigation_path(robot_gs_dir, config_dir)
            self._robot_gs_dir = candidate if candidate.exists() else None

    def _parse_scene_objects(self, raw) -> Dict[str, str]:
        if isinstance(raw, dict):
            return {str(name): str(link or name) for name, link in raw.items()}
        objects: Dict[str, str] = {}
        for item in raw or []:
            if isinstance(item, str):
                objects[item] = item
            elif isinstance(item, dict):
                name = item.get("name")
                if name:
                    objects[str(name)] = str(item.get("link") or name)
        return objects

    def _parse_scene_fixtures(self, raw) -> Dict[str, dict]:
        if isinstance(raw, dict):
            return {str(name): dict(value or {}) for name, value in raw.items()}
        fixtures: Dict[str, dict] = {}
        for item in raw or []:
            if isinstance(item, dict) and item.get("name"):
                entry = dict(item)
                name = str(entry.pop("name"))
                fixtures[name] = entry
        return fixtures

    @property
    def scene_xml(self) -> str:
        return self.mjcf_path

    @property
    def body_gaussians(self) -> Dict[str, str]:
        d: Dict[str, str] = {}
        if self._robot_gs_dir and self._robot_gs_dir.is_dir():
            for ply in sorted(self._robot_gs_dir.glob("*.ply")):
                d[ply.stem] = ply.as_posix()
        return d

    @property
    def background_ply(self) -> Optional[str]:
        return self._background_ply
