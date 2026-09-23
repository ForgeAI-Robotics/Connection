"""Reception business package. Phase order stays here, not in the runner."""

from __future__ import annotations

from dataclasses import dataclass

from kernel.contracts import CONTRACT_VERSION, OBJECT_ID


@dataclass(frozen=True)
class StepSpec:
    step_id: str
    kind: str
    prefix: str = ""
    key: str = ""
    evidence: str = ""
    handoff_before: str = ""
    requires_gate: bool = False
    requires_object_evidence: bool = False
    requires_safe_idle: bool = False
    optional: bool = False
    writes: str = ""
    target_area: str = ""
    deadline_sec: float = 30.0

    @property
    def body(self) -> bool:
        return self.kind in {"navigate", "inspect", "pick", "place"}


# Copied from reception_real.NAVIGATION_LEGS so the request body keeps the same fields.
NAVIGATION_LEGS = {
    "table2": {
        "target_id": "table_2",
        "route_phase": "",
        "leg_index": 1,
        "goal_xyt": [0.9903405869861586, 1.3761315438191244, -0.39236607751253016],
        "motion_mode": "forward_path",
    },
    "relay2": {
        "target_id": "door_1",
        "route_phase": "door_approach",
        "leg_index": 2,
        "goal_xyt": [3.733075988421528, 6.215369909530748, 2.718279944258407],
        "motion_mode": "forward_path",
    },
    "relay3": {
        "target_id": "door_1",
        "route_phase": "door_lateral_exit",
        "leg_index": 3,
        "goal_xyt": [4.185939449618811, 7.560143924693016, 2.7689146673931306],
        "motion_mode": "lateral_path_aligned",
    },
    "table1": {
        "target_id": "table_1",
        "route_phase": "table1_approach",
        "leg_index": 4,
        "goal_xyt": [3.0873798986272165, 8.279995338440145, 1.175238157458919],
        "motion_mode": "forward_path",
    },
}


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
