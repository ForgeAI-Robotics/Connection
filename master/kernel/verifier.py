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
            return self._bound_rule(contract, evidence)
        return "UNKNOWN"

    def _bound_rule(self, contract: SkillContract, evidence: dict) -> str:
        """Published rules stay on the contract. They are not fields of the wire body."""
        checks = []
        distrust = False
        for rule in contract.bound_rules or ():
            if not isinstance(rule, dict):
                continue
            if rule.get("evidence"):
                checks.append(rule["evidence"])
            if rule.get("distrust_claim"):
                distrust = True
        if not checks and not distrust:
            return "PASS"
        if checks and evidence.get("effect") not in checks:
            return "FAIL"
        if distrust and evidence.get("claimed_ok") is not True:
            # 真实适配器不写 claimed_ok。缺了就是还没单独复核，不能当成 PASS。
            return "FAIL" if "claimed_ok" in evidence else "UNKNOWN"
        return "PASS"
