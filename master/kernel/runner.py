"""Execute one contract. Budget 0: one submit, or a read-only query. No new route."""

from __future__ import annotations

from kernel.contracts import KernelError, ProgressEvent, SkillContract, evidence_filename


class Runner:
    def execute(self, contract: SkillContract, port, *, requery: bool = False) -> ProgressEvent:
        try:
            return self._execute(contract, port, requery=requery)
        except Exception as exc:
            # A transport exception is not evidence that an action failed to start.
            return self._progress(contract, {
                "command_id": contract.command_id, "terminal": "",
                "started": None, "stopped": False, "evidence": {}, "error": str(exc),
            })

    def _execute(self, contract: SkillContract, port, *, requery: bool = False) -> ProgressEvent:
        if contract.failure_budget != 0:
            raise KernelError("局部恢复预算必须为 0")
        if requery:
            observed = port.query(contract.command_id, contract.request)
            return self._progress(contract, observed)
        submitted = port.submit(contract.command_id, contract.request)
        if not isinstance(submitted, dict):
            submitted = {}
        if submitted.get("unclear") or submitted.get("accepted") is False:
            return self._progress(
                contract,
                {
                    "command_id": contract.command_id,
                    "terminal": None,
                    "timed_out": True,
                    "started": None,
                    "stopped": False,
                    "evidence": {},
                    "error": submitted.get("error") or "submit_unclear",
                },
            )
        observed = port.wait(contract.command_id, contract.request)
        if isinstance(observed, dict) and not observed.get("command_id"):
            observed = dict(observed)
            observed["command_id"] = contract.command_id
        return self._progress(contract, observed or {})

    def _progress(self, contract: SkillContract, observed: dict) -> ProgressEvent:
        evidence = dict(observed.get("evidence")) if isinstance(observed.get("evidence"), dict) else {}
        evidence["resources_released"] = observed.get("resources_released") is True
        terminal = observed.get("terminal") or ""
        return ProgressEvent(
            task_id=contract.task_id,
            step_id=contract.step_id,
            attempt_id=contract.attempt_id,
            command_id=contract.command_id,
            phase=contract.step_id,
            progress="query" if observed.get("timed_out") else (terminal or "pending"),
            evidence_ref=evidence_filename(contract.attempt_id),
            action_ended=bool(terminal),
            effect_ok=evidence.get("supports") is True,
            publisher_stopped=observed.get("stopped") is True,
            handoff_confirmed=False,
            timed_out=bool(observed.get("timed_out")),
            terminal=str(terminal or ""),
            evidence=evidence,
            safe_idle=False,
            error=str(observed.get("error") or ""),
            started=observed.get("started"),
        )
