"""Reception wire builders; flow and parameters live in the skill definition."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace as _replace

from contracts.tasks import CONTRACT_VERSION, OBJECT_ID


NAME = "reception"
# The archived definition is packaged with the code; no operator config is read
# at import time. New tasks may bind an explicitly configured definition.
from brain.packages.reception_skill import load_skill
from contracts.steps import StepSpec
from contracts.reception_lan import NAVIGATION_LEGS

_BASELINE = load_skill()
SOP = _BASELINE.sop
PHASES = _BASELINE.phases()


# Joint debugging while manipulation is unavailable: the same four navigation legs and
# wire bodies, with no pick/place. Selected only by its exact trigger, never by the model.
NAV_ONLY_SOP = {
    "id": "reception.single_can.nav_only", "version": "1",
    "description": "仅导航联调：按单罐接待路线依次导航 table_2、relay2、relay3、table_1，不派发抓取与放置。",
    "constraints": "只验证大脑调度与导航；不含操控，任务成功不代表接待完成。路段与导航请求同单罐接待。",
    "triggers": ["开始接待（仅导航）", "开始接待(仅导航)", "仅导航接待"],
}

SIM_MANIPULATION_SOP = {
    "id": "reception.single_can.sim_manipulation", "version": "1",
    "description": "导航真机＋操控模拟：完整接待流程，抓取和放置由本地模拟端返回结果。",
    "constraints": "导航真实执行，操控不驱动物理设备；完成仅代表混合流程通过，不代表真实抓放成功。",
    "triggers": ["开始接待（导航真机＋操控模拟）", "开始接待(导航真机+操控模拟)"],
}

ROS_SIM_MANIPULATION_SOP = {
    **SIM_MANIPULATION_SOP,
    "id": "reception.single_can.ros_sim_manipulation",
    "description": "真实导航项目驱动 O6 物理仿真，操控由本地模拟端返回结果。",
    "constraints": "只用于 office_ros_8001 隔离仿真，不执行真机导航或真实抓放。",
    "triggers": ["开始接待（导航仿真＋操控模拟）"],
}

# Without a manipulation step every transition stays within one navigation source.
NAV_ONLY_PHASES = tuple(_replace(step, handoff_before="") for step in PHASES
                        if step.kind in {"local", "navigate"})


def matches(task) -> bool:
    text = task if isinstance(task, str) else (" ".join(map(str, task)) if task else "")
    return any(word in text for word in ("接待", "补货", "会议接待", "开始接待", "迎宾", "准备会议", "会议准备"))


def phase_ids():
    return [step.step_id for step in PHASES]


def inspection_enabled(config) -> bool:
    real = (config or {}).get("reception_real") or {}
    return real.get("dream_inspection_enabled") is True


def navigation_body(step: StepSpec, task_id: str, command_id: str) -> dict:
    # Older task snapshots have no resolved navigation field.
    leg = deepcopy(getattr(step, "navigation", None) or NAVIGATION_LEGS[step.key])
    return {
        "contract_version": CONTRACT_VERSION,
        "command_id": command_id,
        "task_id": task_id,
        "target_id": leg["target_id"],
        "route_phase": leg["route_phase"],
        "leg_index": leg["leg_index"],
        "frame_id": "map",
        "goal_xyt": list(leg["goal_xyt"]),
        "motion_mode": leg["motion_mode"],
        "require_final_orientation": True,
    }


def manipulation_body(step: StepSpec, task_id: str, command_id: str, navigation_command_id: str) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "command_id": command_id,
        "task_id": task_id,
        "operation": step.kind,
        "object_id": getattr(step, "object_id", "") or OBJECT_ID,
        "target_area": step.target_area,
        "navigation_proof": {
            "dream_command_id": navigation_command_id,
            "target_id": step.target_area,
            "state": "succeeded",
        },
    }


def inspection_body(task_id: str, command_id: str, navigation_command_id: str, *, object_id=OBJECT_ID) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "command_id": command_id,
        "task_id": task_id,
        "navigation_command_id": navigation_command_id,
        "target_object_id": object_id,
    }
