"""Generic plans become runtime steps. The planner still writes the plan."""

from __future__ import annotations

from dataclasses import asdict, fields

from contracts.steps import StepSpec


NAME = "generic"


def steps_from_subtasks(subtask_list, *, kind="sim") -> list[StepSpec]:
    steps = []
    order = 0
    for item in subtask_list or []:
        if not isinstance(item, dict):
            raise ValueError("计划步骤必须是对象")
        text = str(item.get("subtask") or "").strip()
        if not text:
            raise ValueError("计划步骤不能为空")
        order += 1
        from brain.adapters.execution import parse_sim_action
        action = parse_sim_action(text)
        object_id = action[1] if action and action[0] in {"grasp", "place"} else ""
        steps.append(
            StepSpec(
                step_id=f"STEP_{order}",
                kind=kind,
                prefix=f"{kind}-{order}",
                key=text,
                evidence="sim_effect",
                deadline_sec=120,
                robot_name=str(item.get("robot_name") or "FQrobot"),
                object_id=object_id,
            )
        )
    return steps


from contracts.steps import step_spec_dict, steps_from_specs
