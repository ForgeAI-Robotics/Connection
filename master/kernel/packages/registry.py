"""Choose an existing business package. Unknown work stays on the old queue."""

from __future__ import annotations

from kernel.contracts import KernelError
from kernel.packages import desk, look, reception


def match_name(task) -> str:
    if reception.matches(task):
        return reception.NAME
    if look.matches(task):
        return look.NAME
    if desk.matches(task):
        return desk.NAME
    return ""


def phases_for(name: str):
    if name == "reception":
        return reception.PHASES
    if name == "look":
        return look.PHASES
    raise KernelError(f"业务包没有可执行相位: {name}")
