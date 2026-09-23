"""Desk tidy package. Skill names are the four already implemented in skill_executor."""

from __future__ import annotations


NAME = "desk"

TRIGGERS = ("整理桌面", "桌面整理", "清洁会议室", "整理会议室", "收拾桌")

# These names already exist in master/sop/skill_executor.py. No extra scene is added.
EXISTING_SKILLS = ("clean_trash", "tidy_milk", "tidy_cola", "tidy_penholder")


def matches(task) -> bool:
    text = task if isinstance(task, str) else (task[0] if task else "")
    return any(word in str(text) for word in TRIGGERS)
