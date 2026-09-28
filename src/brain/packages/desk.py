"""Desk tidy package. Skill names are the four already implemented in skill_executor."""

from __future__ import annotations


NAME = "desk"

TRIGGERS = ("整理桌面", "桌面整理", "清洁会议室", "整理会议室", "收拾桌")

# These names already exist in data/business/skill_executor.py. No extra scene is added.
EXISTING_SKILLS = ("clean_trash", "tidy_milk", "tidy_cola", "tidy_penholder")


def matches(task) -> bool:
    text = task if isinstance(task, str) else (task[0] if task else "")
    return any(word in str(text) for word in TRIGGERS)


def plan_steps(planned, port):
    """Expand the four existing skills into interruptible, individually verified actions."""
    from brain.packages.generic import steps_from_subtasks
    from contracts.steps import StepSpec
    from execution.robot_api.desk import SKILLS, pending_objects
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
            if world[obj].get("grasped") is not True:
                tasks.append({"subtask": f"抓取 {obj}", "robot_name": item.get("robot_name")})
            tasks.append({"subtask": f"放置 {obj} 到 {SKILLS[name]['zone']}", "robot_name": item.get("robot_name")})
    steps = steps_from_subtasks(tasks)
    steps.append(StepSpec("DESK_CHECK", "desk_check", evidence="desk_tidy"))
    return steps


def normalize_subtasks(subtasks, world, zones):
    """Validate the whole fixed-workspace plan before the first grasp is dispatched."""
    from brain.adapters.execution import parse_sim_action
    from contracts.tasks import Rejected
    normalized = []
    for item in subtasks:
        text = item.get("subtask", "")
        action = parse_sim_action(text)
        if not action:
            raise Rejected(f"Desk 不支持计划步骤：{text}")
        kind = action[0]
        if kind == "navigate":
            if action[1] not in zones:
                raise Rejected(f"Desk 没有导航能力或该目标区域：{action[1]}")
            # A known zone is already reachable in Desk's stationary workspace.
            continue
        if kind not in {"grasp", "place"} or action[1] not in world:
            raise Rejected(f"Desk 不支持计划动作或物体：{text}")
        if kind == "place" and action[2] not in zones:
            raise Rejected(f"Desk 没有目标区域：{action[2]}")
        canonical = f"抓取 {action[1]}" if kind == "grasp" else f"放置 {action[1]} 到 {action[2]}"
        normalized.append(dict(item, subtask=canonical))
    return normalized
