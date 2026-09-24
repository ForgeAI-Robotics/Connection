"""Immutable business-independent step specifications and persisted serialization."""
from dataclasses import dataclass, asdict, fields

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
    robot_name: str = ""
    object_id: str = ""

    @property
    def body(self) -> bool:
        return self.kind not in {"describe", "desk_check", "local", "verify"}


def step_spec_dict(step: StepSpec) -> dict:
    return asdict(step)


def steps_from_specs(specs) -> list[StepSpec]:
    known = {item.name for item in fields(StepSpec)}
    steps = []
    for spec in specs or []:
        if not isinstance(spec, dict) or not spec.get("step_id"):
            continue
        steps.append(StepSpec(**{key: spec[key] for key in known if key in spec}))
    return steps
