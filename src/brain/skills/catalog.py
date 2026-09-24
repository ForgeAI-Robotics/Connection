"""Skills that already have an execution path.

Voice and help are not listed. They have no executor in this kernel.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillEntry:
    skill_id: str
    applies: str
    execution: str
    verifies: str
    physical: bool = True


CATALOG = (
    SkillEntry(
        "navigate",
        "接待路段需要沿既有合同移动",
        "DreamClient.submit_navigation",
        "reached",
    ),
    SkillEntry(
        "inspect",
        "dream_inspection_enabled 为真时检查台面",
        "DreamClient.submit_inspection",
        "inspected",
    ),
    SkillEntry(
        "pick",
        "把合同里的物体从台面抓住",
        "VlaClient.submit_task",
        "object_held",
    ),
    SkillEntry(
        "place",
        "把已经抓住的合同物体放到目标台面",
        "VlaClient.submit_task",
        "object_at_target",
    ),
    SkillEntry(
        "describe",
        "用户只要现场描述，不移动、不抓取",
        "camera.capture_scene",
        "scene_description",
        False,
    ),
    SkillEntry("desk_check", "桌面整理最终位置复核", "execution.robot_api.get_objects", "desk_tidy", False),
)


def by_id(skill_id: str) -> SkillEntry | None:
    for entry in CATALOG:
        if entry.skill_id == skill_id:
            return entry
    return None


def registered_ids() -> tuple[str, ...]:
    return tuple(entry.skill_id for entry in CATALOG)


def is_physical(skill_id: str) -> bool:
    entry = by_id(skill_id)
    if entry is None:
        return True
    return entry.physical
