"""Explicit planner inputs; no transports, files or task mutation."""
from dataclasses import dataclass, field

@dataclass(frozen=True)
class PlanningInput:
    task: str
    robot_names: tuple
    capabilities: dict
    scene: dict
    experiences: str = ""
    rules: tuple = ()
    history: tuple = ()


@dataclass(frozen=True)
class Plan:
    steps: tuple
    reasoning: str = ""

    def __iter__(self):
        return iter(self.steps)
