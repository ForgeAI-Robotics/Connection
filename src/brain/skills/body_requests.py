"""Shared NAV/VLA wire builders for planned tasks and SOPs."""
from copy import deepcopy
from contracts.steps import StepSpec
from contracts.tasks import CONTRACT_VERSION, OBJECT_ID
from contracts.reception_lan import NAVIGATION_LEGS


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
