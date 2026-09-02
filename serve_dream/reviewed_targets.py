"""Resolve natural-language DREAM targets from the reviewed navigation SOP."""

from __future__ import annotations

import os
import re

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOP_PATH = os.path.join(_HERE, "dream_navigation_sop.yaml")


def load_sop(path=DEFAULT_SOP_PATH):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _norm(value):
    return re.sub(r"[\s_-]+", "", str(value or "").strip().lower())


def resolve_reviewed_target(name, sop=None):
    """Return a reviewed point spec for table1/table2, or None without guessing."""
    sop = sop or load_sop()
    wanted = _norm(name)
    for target_id, spec in (sop.get("reviewed_targets") or {}).items():
        aliases = [target_id] + list(spec.get("aliases") or [])
        normalized_aliases = {_norm(alias) for alias in aliases}
        if wanted in normalized_aliases or any(
            alias and alias in wanted for alias in normalized_aliases
        ):
            result = dict(spec)
            result["target_id"] = target_id
            result["frame_id"] = sop.get("frame_id", "map")
            result["yaw_unit"] = sop.get("yaw_unit", "radians")
            return result
    return None


def resolve_reviewed_route_target(name, sop=None):
    """Resolve a reviewed door phase before falling back to table targets."""
    sop = sop or load_sop()
    wanted = _norm(name)
    contract = door_contract(sop)
    if not contract:
        return None
    phase_aliases = {
        "door_approach": (
            "doorapproach", "door1doorapproach", "门前点", "门外点",
            "门外接近", "门口接近", "窄门接近",
        ),
        "door_lateral_exit": (
            "doorlateralexit", "door1doorlateralexit", "横移进门",
            "横向移动进门", "侧移进门", "横移穿门", "门出口点",
        ),
    }
    for phase, aliases in phase_aliases.items():
        if any(_norm(alias) in wanted for alias in aliases):
            result = dict(contract[phase])
            result["target_id"] = (sop.get("door_transit") or {}).get(
                "target_id", "door_1"
            )
            result["route_phase"] = phase
            result["frame_id"] = sop.get("frame_id", "map")
            result["yaw_unit"] = sop.get("yaw_unit", "radians")
            return result
    result = resolve_reviewed_target(name, sop)
    if result and result.get("target_id") == "table_1":
        result["route_phase"] = "table1_approach"
        result["leg_index"] = 4
    return result


def door_contract(sop=None):
    sop = sop or load_sop()
    section = sop.get("door_transit") or {}
    legs = section.get("legs") or []
    if len(legs) != 2:
        return None
    return {
        "door_approach": dict(legs[0]),
        "door_lateral_exit": dict(legs[1]),
    }


def as_robot_api_target(spec, *, task_id=None, leg_index=1, command_id=None):
    goal = spec.get("goal_xyt") or []
    if len(goal) != 3:
        raise ValueError("审核目标缺少 goal_xyt=[x,y,yaw]")
    target = {
        "x": float(goal[0]), "y": float(goal[1]), "yaw": float(goal[2]),
        "yaw_unit": spec.get("yaw_unit", "radians"),
        "target_id": spec["target_id"],
        "leg_index": int(leg_index),
        "frame_id": spec.get("frame_id", "map"),
        "motion_mode": spec.get("motion_mode", "forward_path"),
        "require_final_orientation": bool(spec.get("require_final_orientation", True)),
    }
    if spec.get("route_phase"):
        target["route_phase"] = str(spec["route_phase"])
    if task_id:
        target["task_id"] = task_id
    if command_id:
        target["command_id"] = command_id
    return target
