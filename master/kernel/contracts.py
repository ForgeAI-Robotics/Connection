"""Phase-1 contracts. Snapshots written into the ledger are not edited later."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


CONTRACT_VERSION = "fq/reception-lan/v1"
OBJECT_ID = "cola_can_1"

NON_TERMINAL_STATES = {
    "running",
    "paused",
    "waiting_human",
    "verifying",
    "cancelling",
    "recovery_required",
}
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


class KernelError(RuntimeError):
    pass


class IllegalTransition(KernelError):
    pass


class Rejected(KernelError):
    pass


class StaleWrite(KernelError):
    """Another writer already advanced this ledger revision."""


def task_token(task_id) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "", str(task_id))[-12:]
    return token or "task"


def make_command_id(prefix: str, task_id: str, attempt_number: int) -> str:
    """First physical send has no suffix. Later attempts of the same step use -r{n}."""
    base = f"{prefix}-{task_token(task_id)}"
    if int(attempt_number) <= 1:
        return base
    return f"{base}-r{int(attempt_number) - 1}"


def evidence_filename(attempt_id: str) -> str:
    return f"{attempt_id}.jpg"


@dataclass
class SkillContract:
    package: str
    skill: str
    version: str
    task_id: str
    step_id: str
    attempt_id: str
    goal: str
    object_id: str
    preconditions: str
    evidence: str
    failure_budget: int
    deadline_sec: float
    command_prefix: str
    command_id: str
    postconditions: tuple
    handoff: str
    tools: tuple
    requires_object_evidence: bool
    requires_safe_idle: bool
    request: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["postconditions"] = list(self.postconditions)
        payload["tools"] = list(self.tools)
        return payload


@dataclass
class ProgressEvent:
    task_id: str
    step_id: str
    attempt_id: str
    command_id: str
    phase: str
    progress: str
    evidence_ref: str
    action_ended: bool
    effect_ok: bool
    publisher_stopped: bool
    handoff_confirmed: bool
    timed_out: bool
    terminal: str
    evidence: dict
    safe_idle: bool
    error: str = ""
    started: object = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "ProgressEvent":
        data = dict(payload or {})
        known = {item.name for item in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{key: data[key] for key in known if key in data})


@dataclass
class AttemptRecord:
    attempt_id: str
    step_id: str
    task_id: str
    command_id: str
    scene: dict
    contract: dict
    request: dict
    evidence_ref: str
    delta: dict
    verdict: str
    outcome: str
    intent: str

    def as_dict(self) -> dict:
        return asdict(self)
