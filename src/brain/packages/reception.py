"""Reception business package. Phase order stays here, not in the runner."""

from __future__ import annotations

from dataclasses import dataclass

from contracts.tasks import CONTRACT_VERSION, OBJECT_ID


NAME = "reception"
SOP = {
    "id": "reception.single_can", "version": "1",
    "description": "单罐接待：table_2 抓取 cola_can_1，经 relay2、relay3 到 table_1 放置。",
    "constraints": "固定一罐、固定物体与桌子；不包含开灯、人数补货、改路线或额外动作。",
    "triggers": ["开始接待", "启动接待", "执行接待", "开始单罐接待"],
}


from contracts.steps import StepSpec


from contracts.reception_lan import NAVIGATION_LEGS

PHASES = (
    StepSpec("INITIALIZING", "local"),
    StepSpec("FETCHING_WORLD", "local"),
    StepSpec(
        "NAVIGATING_TO_TABLE2",
        "navigate",
        prefix="nav-table2",
        key="table2",
        evidence="reached",
        requires_gate=True,
        deadline_sec=900,
    ),
    StepSpec(
        "DREAM_INSPECTING_TABLE2",
        "inspect",
        prefix="inspect-table2",
        evidence="inspected",
        optional=True,
        deadline_sec=300,
    ),
    StepSpec(
        "VLA_PICKING",
        "pick",
        prefix="vla-pick",
        evidence="object_held",
        handoff_before="to_vla",
        requires_object_evidence=True,
        writes="in_gripper",
        target_area="table_2",
        deadline_sec=600,
    ),
    StepSpec("VERIFYING_GRASP", "verify", evidence="object_held"),
    StepSpec(
        "NAVIGATING_TO_RELAY2",
        "navigate",
        prefix="nav-relay2",
        key="relay2",
        evidence="reached",
        handoff_before="to_nav",
        deadline_sec=900,
    ),
    StepSpec(
        "LATERAL_TO_RELAY3",
        "navigate",
        prefix="nav-relay3",
        key="relay3",
        evidence="reached",
        deadline_sec=300,
    ),
    StepSpec(
        "NAVIGATING_TO_TABLE1",
        "navigate",
        prefix="nav-table1",
        key="table1",
        evidence="reached",
        deadline_sec=900,
    ),
    StepSpec(
        "VLA_PLACING",
        "place",
        prefix="vla-place",
        evidence="object_at_target",
        handoff_before="to_vla",
        requires_object_evidence=True,
        requires_safe_idle=True,
        writes="table_1",
        target_area="table_1",
        deadline_sec=1800,
    ),
    StepSpec("VERIFYING_PLACE", "verify", evidence="object_at_target"),
)


def matches(task) -> bool:
    text = task if isinstance(task, str) else (" ".join(map(str, task)) if task else "")
    return any(word in text for word in ("接待", "补货", "会议接待", "开始接待", "迎宾", "准备会议", "会议准备"))


def phase_ids():
    return [step.step_id for step in PHASES]


def inspection_enabled(config) -> bool:
    real = (config or {}).get("reception_real") or {}
    return real.get("dream_inspection_enabled") is True


def navigation_body(step: StepSpec, task_id: str, command_id: str) -> dict:
    leg = NAVIGATION_LEGS[step.key]
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
        "object_id": OBJECT_ID,
        "target_area": step.target_area,
        "navigation_proof": {
            "dream_command_id": navigation_command_id,
            "target_id": step.target_area,
            "state": "succeeded",
        },
    }


def inspection_body(task_id: str, command_id: str, navigation_command_id: str) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "command_id": command_id,
        "task_id": task_id,
        "navigation_command_id": navigation_command_id,
        "target_object_id": OBJECT_ID,
    }
