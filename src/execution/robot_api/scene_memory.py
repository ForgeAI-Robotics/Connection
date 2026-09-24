"""Simulator-neutral scene state memory."""

from __future__ import annotations

import shutil
from datetime import datetime
from typing import Any

import numpy as np
import yaml

from .scene_metadata import (
    scene_config_path,
    scene_state_initial_path,
    scene_state_path,
    waypoint_path,
)


def _load_waypoint_coords() -> dict[str, list[float]]:
    with waypoint_path().open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {wp["name"]: wp["pos"][:2] for wp in data.get("waypoints", [])}


def load_state() -> dict[str, Any]:
    with scene_state_path().open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"locations": {}}


def save_state(state: dict[str, Any]):
    state["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with scene_state_path().open("w", encoding="utf-8") as f:
        yaml.dump(state, f, allow_unicode=True, default_flow_style=False)


def reset_to_initial():
    shutil.copy(scene_state_initial_path(), scene_state_path())
    print("[SceneMemory] scene state reset to initial state")


def get_object_location(obj_name: str) -> str:
    state = load_state()
    for location, info in state.get("locations", {}).items():
        if obj_name in (info.get("objects") or []):
            return location
    return "unknown"


def get_object_coords(obj_name: str) -> list[float] | None:
    location = get_object_location(obj_name)
    if location in {"unknown", "robot_hand"}:
        return None
    coords = _load_waypoint_coords()
    wp_coords = coords.get(location)
    if wp_coords:
        return [wp_coords[0], wp_coords[1], 0.9]
    return None


def move_object(obj_name: str, to_location: str):
    state = load_state()
    from_location = "unknown"
    locations = state.setdefault("locations", {})
    for location, info in locations.items():
        objects = info.get("objects") or []
        if obj_name in objects:
            objects.remove(obj_name)
            info["objects"] = objects
            from_location = location
            break

    if to_location not in locations:
        locations[to_location] = {"fixture": None, "objects": []}
    objects = locations[to_location].get("objects") or []
    if obj_name not in objects:
        objects.append(obj_name)
    locations[to_location]["objects"] = objects

    save_state(state)
    print(f"[SceneMemory] {obj_name}: {from_location} -> {to_location}")


def get_location_objects(location: str) -> list[str]:
    state = load_state()
    return state.get("locations", {}).get(location, {}).get("objects") or []


def get_all_locations() -> dict[str, list[str]]:
    state = load_state()
    return {
        location: info.get("objects") or []
        for location, info in state.get("locations", {}).items()
    }


def build_initial_state(env) -> dict[str, Any]:
    with waypoint_path().open("r", encoding="utf-8") as f:
        waypoints_data = yaml.safe_load(f) or {}
    with scene_config_path("objects.yaml").open("r", encoding="utf-8") as f:
        objects_data = yaml.safe_load(f) or {}

    waypoints = waypoints_data.get("waypoints", [])
    object_fixtures = {
        item["name"]: item.get("placement", {}).get("fixture")
        for item in objects_data.get("objects", [])
    }
    fixture_keywords = {
        "counter",
        "island",
        "sink",
        "stove",
        "fridge",
        "microwave",
        "oven",
        "cabinet",
    }

    locations = {}
    for waypoint in waypoints:
        fixture = None
        for served in waypoint.get("serves", []):
            if served.lower() in fixture_keywords:
                fixture = served
                break
        locations[waypoint["name"]] = {"fixture": fixture, "objects": []}
    locations["robot_hand"] = {"fixture": None, "objects": []}

    coords = {waypoint["name"]: waypoint["pos"][:2] for waypoint in waypoints}
    for obj_name in list(env.obj_body_id.keys()):
        try:
            pos = env.get_object_pos(obj_name)
            point = np.array(pos[:2])
            best_wp = min(coords, key=lambda name: np.linalg.norm(point - np.array(coords[name])))
            entry = locations[best_wp]
            entry["objects"].append(obj_name)
            if entry["fixture"] is None:
                entry["fixture"] = object_fixtures.get(obj_name)
        except Exception as exc:
            print(f"[SceneMemory] could not locate {obj_name}, skipped: {exc}")

    return {"last_updated": "initial", "locations": locations}


def coords_to_waypoint(pos: list[float]) -> str:
    if pos is None:
        return "unknown"
    point = np.array(pos[:2])
    coords = _load_waypoint_coords()

    best_name = "unknown"
    best_dist = float("inf")
    for name, wp_coords in coords.items():
        dist = np.linalg.norm(point - np.array(wp_coords))
        if dist < best_dist:
            best_dist = dist
            best_name = name

    print(f"[SceneMemory] place position {point.tolist()} -> nearest waypoint {best_name} (dist={best_dist:.2f})")
    return best_name
