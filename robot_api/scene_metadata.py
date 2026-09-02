"""Simulator-neutral scene metadata helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from .config import PROJECT_ROOT


DEFAULT_SCENE_CONFIG_DIR = PROJECT_ROOT / "assets" / "scene_config"
DEFAULT_NAV2_CONFIG_PATH = PROJECT_ROOT / "nav2" / "config.yaml"
DEFAULT_FREE_POINTS_PATH = PROJECT_ROOT / "nav2" / "maps" / "free_points.json"


def scene_config_dir() -> Path:
    configured = os.getenv("ROBOT_SCENE_CONFIG_DIR") or os.getenv("SCENE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_SCENE_CONFIG_DIR


def scene_config_path(filename: str) -> Path:
    return scene_config_dir() / filename


def waypoint_path() -> Path:
    return scene_config_path("waypoints.yaml")


def camera_config_path() -> Path:
    return scene_config_path("camera.yaml")


def scene_state_path() -> Path:
    return scene_config_path("scene_state.yaml")


def scene_state_initial_path() -> Path:
    return scene_config_path("scene_state_initial.yaml")


def nav2_config_path() -> Path:
    configured = os.getenv("ROBOT_NAV2_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_NAV2_CONFIG_PATH


def free_points_path() -> Path:
    configured = os.getenv("ROBOT_FREE_POINTS")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_FREE_POINTS_PATH


def load_yaml(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    return default if loaded is None else loaded


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_waypoints() -> list[dict[str, Any]]:
    data = load_yaml(waypoint_path(), {"waypoints": []})
    return list(data.get("waypoints") or [])


def load_camera_config() -> dict[str, Any]:
    return dict(load_yaml(camera_config_path(), {}) or {})


def load_free_points() -> list[dict[str, Any]]:
    data = load_json(free_points_path(), {"points": []})
    return [
        {
            "name": point["name"],
            "pos": [point["x"], point["y"]],
            "serves": [],
        }
        for point in data.get("points", [])
    ]


def load_reach_limits() -> tuple[float, float]:
    data = load_yaml(nav2_config_path(), {}) or {}
    waypoint_cfg = data.get("waypoints", {})
    return float(waypoint_cfg.get("min_dist", 0.1)), float(waypoint_cfg.get("max_reach", 1.0))


def build_navigation_guide() -> dict[str, Any]:
    waypoints = load_waypoints()
    if not waypoints:
        return {}

    guide: dict[str, Any] = {
        "说明": "导航时优先使用以下场景工作点提示，不要臆造未配置的工作点名称",
    }
    for waypoint in waypoints:
        name = waypoint.get("name")
        serves = [str(item) for item in (waypoint.get("serves") or [])]
        if not name or not serves:
            continue
        for served in serves:
            entry = guide.setdefault(served, {"工作点": name, "可服务物体": []})
            if not entry.get("工作点"):
                entry["工作点"] = name
            objects = entry.setdefault("可服务物体", [])
            for item in serves:
                if item not in objects:
                    objects.append(item)
    return guide
