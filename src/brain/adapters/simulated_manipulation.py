"""Real navigation plus local, explicitly simulated manipulation receipts.

Runtime remains the only task ledger. Cached terminal receipts can be restored
from it; a missing receipt after restart is UNKNOWN, never reconstructed success.
No VLA HTTP connection or physical controller transfer exists in this mode.
"""
from copy import deepcopy
from datetime import datetime
import threading
import time

from brain.adapters.execution import BodyAdapter, interpret_observation, _timestamp_has_timezone
from brain.adapters.http_client import HttpContractError
from brain.adapters.vla_client import VlaClient
from contracts.reception_lan import validate_request, InvalidRequest
from contracts.tasks import CONTRACT_VERSION
from shared.brain_journal import CallBook


class SimulatedManipulationClient:
    # Reuse the real client's bounded polling and abandoned-wait handling.
    wait_task = VlaClient.wait_task
    abandon_waits = VlaClient.abandon_waits

    def __init__(self, dream, *, delay_sec=1.0):
        self.dream = dream
        self.delay_sec = delay_sec
        self.calls = CallBook("vla_simulated")
        self._wait_abandoned = threading.Event()
        self._lock = threading.RLock()
        self._commands = {}
        self._requests = {}
        self._ready_at = {}

    @staticmethod
    def _reject(code, message):
        raise HttpContractError(message, status_code=409,
                                payload={"success": False, "error": {"code": code, "message": message}})

    def submit_task(self, payload):
        try:
            validate_request("vla", payload)
        except InvalidRequest as exc:
            self._reject("INVALID_REQUEST", str(exc))
        command_id = payload["command_id"]
        with self._lock:
            if command_id in self._requests:
                if self._requests[command_id] != payload:
                    raise HttpContractError("simulated command identity conflict", status_code=409)
                return self.task(command_id)
        # A mock pick/place still requires a real, identity-bound arrival receipt.
        proof = payload["navigation_proof"]
        nav = self.dream.command(proof["dream_command_id"])
        result = nav.get("result") or {}
        if not (nav.get("command_id") == proof["dream_command_id"]
                and nav.get("task_id") == payload["task_id"]
                and nav.get("contract_version") == payload["contract_version"]
                and nav.get("target_id") == payload["target_area"]
                and nav.get("state") == "succeeded"
                and _timestamp_has_timezone(nav.get("completed_at"))
                and result.get("success") is True and result.get("reached") is True
                and result.get("navigation_stopped") is True):
            self._reject("NAVIGATION_PROOF_INVALID", "模拟操控的原导航到达及停止回执未确认")
        with self._lock:
            # Recheck after the read-only remote call, including a concurrent submit.
            if command_id in self._requests:
                if self._requests[command_id] != payload:
                    raise HttpContractError("simulated command identity conflict", status_code=409)
                return self.task(command_id)
            now = datetime.now().astimezone().isoformat()
            raw = {**deepcopy(payload), "state": "running", "started_at": now,
                   "completed_at": None, "result": None, "simulated": True}
            self._requests[command_id] = deepcopy(payload)
            self._commands[command_id] = raw
            self._ready_at[command_id] = time.monotonic() + self.delay_sec
            self.calls.start(payload["operation"], command_id=command_id, task_id=payload["task_id"]).accepted(raw)
            return deepcopy(raw)

    def task(self, command_id):
        with self._lock:
            if command_id not in self._commands:
                raise HttpContractError("simulated command receipt unavailable", status_code=404)
            raw = self._commands[command_id]
            if raw["state"] == "running" and time.monotonic() >= self._ready_at[command_id]:
                pick = raw["operation"] == "pick"
                raw.update(state="succeeded", completed_at=datetime.now().astimezone().isoformat(), result={
                    "success": True, "simulated": True, "physical_execution": False,
                    "evidence_level": "simulated_object", "object_id": raw["object_id"],
                    "object_grasped": pick, "holding": raw["object_id"] if pick else None,
                    "released": not pick, "object_at_target": not pick,
                    "policy_stopped": True, "navigation_port_ready": True,
                    "message": "simulated manipulation completed; no physical action executed",
                })
            return deepcopy(raw)

    def cancel(self, task_id, command_id):
        with self._lock:
            raw = self.task(command_id)
            if raw["task_id"] != task_id:
                raise HttpContractError("simulated cancel identity mismatch", status_code=409)
            if raw["state"] == "running":
                raw.update(state="cancelled", completed_at=datetime.now().astimezone().isoformat(), result={
                    "success": False, "object_id": raw["object_id"], "simulated": True,
                    "physical_execution": False, "evidence_level": "simulated_object",
                    "policy_stopped": True, "navigation_port_ready": True,
                    "message": "simulated manipulation cancelled",
                })
                self._commands[command_id] = raw
            return deepcopy(raw)

    def restore_receipts(self, record):
        with self._lock:
            for step in (record.get("steps") or {}).values():
                for attempt in step.get("attempts") or []:
                    request = attempt.get("request") or {}
                    if request.get("skill") not in {"pick", "place"}:
                        continue
                    raw = (attempt.get("progress", {}).get("evidence") or {}).get("downstream") or {}
                    if raw.get("simulated") is not True:
                        continue
                    command_id = attempt.get("command_id")
                    view = interpret_observation(command_id, raw, request)
                    evidence = view.get("evidence") or {}
                    if (view.get("terminal") and view.get("stopped")
                            and evidence.get("identity_ok") and evidence.get("time_ok")
                            and raw.get("task_id") == record.get("task_id")):
                        self._commands[command_id] = deepcopy(raw)
                        self._requests[command_id] = deepcopy(request["body"])


class HybridReceptionAdapter(BodyAdapter):
    @classmethod
    def from_config(cls, config):
        from brain.adapters.dream_client import DreamClient
        real = config.get("reception_real") or {}
        dream = DreamClient(real.get("dream_base_url"),
                            contract_version=real.get("contract_version") or CONTRACT_VERSION,
                            request_timeout_sec=float(real.get("request_timeout_sec", 10)))
        return cls(dream, SimulatedManipulationClient(dream))

    def restore_receipts(self, record):
        self.vla.restore_receipts(record)

    def handoff_with_context(self, context):
        answer = {"available": False, "confirmed": False, "simulated": True,
                  "physical_control_transfer": False, "kind": context["kind"],
                  "reason": "source_stop_unconfirmed"}
        command_id, request = context.get("source_command_id"), context.get("source_request")
        if not command_id or not request:
            return answer
        source = self.query(command_id, request)
        answer["source"] = source
        evidence = source.get("evidence") or {}
        if not (source.get("terminal") == "succeeded" and source.get("stopped") is True
                and source.get("resources_released") is True
                and evidence.get("identity_ok") is True and evidence.get("time_ok") is True):
            return answer
        try:
            nav = self.dream.status()
        except HttpContractError as exc:
            answer.update(reason="control_status_unavailable", error=str(exc))
            return answer
        idle = (nav.get("navigation_transport_ready") is True
                and "active_command_id" in nav and nav["active_command_id"] in (None, "")
                and "active_command_state" in nav and nav["active_command_state"] is None)
        answer.update(available=True, confirmed=idle, navigation_status=nav,
                      reason="simulated_manipulation_boundary" if idle else "transport_not_ready")
        return answer
