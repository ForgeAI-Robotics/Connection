"""On-site look package. It describes the current view and does not move."""

from __future__ import annotations

from contracts.steps import StepSpec


NAME = "look"

PHASES = (
    StepSpec(
        "DESCRIBING_SCENE",
        "describe",
        evidence="scene_description",
        deadline_sec=30,
    ),
)


def matches(task) -> bool:
    from contracts.look import is_look_task

    return is_look_task(task)
