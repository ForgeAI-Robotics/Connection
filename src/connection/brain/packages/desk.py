"""Desk tidy package. Skill names are the four already implemented in skill_executor."""

from __future__ import annotations


NAME = "desk"

TRIGGERS = ("整理桌面", "桌面整理", "清洁会议室", "整理会议室", "收拾桌")

# These names already exist in master/sop/skill_executor.py. No extra scene is added.
EXISTING_SKILLS = ("clean_trash", "tidy_milk", "tidy_cola", "tidy_penholder")


def matches(task) -> bool:
    text = task if isinstance(task, str) else (task[0] if task else "")
    return any(word in str(text) for word in TRIGGERS)


def plan_steps(planned, port):
    """Expand the four existing skills into interruptible, individually verified actions."""
    from connection.brain.packages.generic import steps_from_subtasks
    from connection.contracts.steps import StepSpec
    from connection.brain.skills.desk import SKILLS, pending_objects
    world, zones = port._read_world(), port._read_zones()
    if not world or not zones:
        raise ValueError("桌面观测不可用")
    tasks = []
    for item in planned.get("subtask_list") or []:
        label = item["subtask"]
        names = [name for name, spec in SKILLS.items()
                 if name in label or spec["label"].split("(")[0] in label]
        if len(names) != 1:
            raise ValueError(f"未知桌面技能: {label}")
        name = names[0]
        for obj in pending_objects(name, world, zones):
            tasks.extend([
                {"subtask": f"抓取 {obj}", "robot_name": item.get("robot_name")},
                {"subtask": f"放置 {obj} 到 {SKILLS[name]['zone']}", "robot_name": item.get("robot_name")},
            ])
    steps = steps_from_subtasks(tasks)
    steps.append(StepSpec("DESK_CHECK", "desk_check", evidence="desk_tidy"))
    return steps
