"""PASS / FAIL / UNKNOWN. A boolean from the old verifier is not reused."""

from __future__ import annotations

from kernel.contracts import SkillContract, ProgressEvent


class Verifier:
    def judge(self, contract: SkillContract, progress: ProgressEvent) -> str:
        evidence = progress.evidence or {}
        if progress.timed_out or not progress.terminal:
            return "UNKNOWN"
        if evidence.get("contradicts") is True or progress.terminal in {"failed", "cancelled"}:
            return "FAIL"
        if contract.requires_object_evidence and evidence.get("grade") != "object":
            return "UNKNOWN"
        if contract.requires_safe_idle and not progress.safe_idle:
            return "UNKNOWN"
        if evidence.get("identity_ok") is not True or evidence.get("time_ok") is not True:
            return "UNKNOWN"
        if evidence.get("supports") is True and progress.terminal == "succeeded":
            return "PASS"
        return "UNKNOWN"
