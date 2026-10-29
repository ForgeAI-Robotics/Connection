"""Real navigation plus local, explicitly simulated manipulation receipts.

Runtime remains the only task ledger. Cached terminal receipts can be restored
from it; a missing receipt after restart is UNKNOWN, never reconstructed success.
No VLA HTTP connection exists. Real navigation uses the shared body authority;
virtual holding never supplies physical O6 evidence.
"""
from copy import deepcopy
from datetime import datetime
import threading
import time

from brain.adapters.execution import BodyAdapter, interpret_observation, _timestamp_has_timezone, _unconfirmed
from brain.adapters.http_client import HttpContractError
from brain.adapters.vla_client import VlaClient
from contracts.reception_lan import validate_request, InvalidRequest
from contracts.tasks import CONTRACT_VERSION
from contracts.vla import NATIVE_OPERATIONS, MANIPULATION_OPERATIONS, validate_request as validate_native_request
from contracts.submission import submission_refusal
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
            if payload.get('operation') in NATIVE_OPERATIONS:
                validate_native_request(payload)
            else:
                validate_request("vla", payload)
        except (InvalidRequest, ValueError) as exc:
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
                pick = raw["operation"] in {"pick", "grasp_cola"}
                raw.update(state="succeeded", completed_at=datetime.now().astimezone().isoformat(), result={
                    "success": True, "simulated": True, "physical_execution": False,
                    "evidence_level": "simulated_object", "object_id": raw["object_id"],
                    "object_grasped": pick, "holding": raw["object_id"] if pick else None,
                    "released": not pick, "object_at_target": not pick,
                    "policy_stopped": True, "navigation_port_ready": True,
                    "message": "simulated manipulation completed; no physical action executed",
                })
                if raw['operation'] == 'open_fridge':
                    raw['result'].update(door_open=True, door_closed=False, door_stable_open=True,
                                         hands_clear_of_door=True, released=False, object_at_target=False)
                elif raw['operation'] == 'grasp_cola':
                    raw['result']['clear_of_fridge'] = True
            return deepcopy(raw)

    def control_status(self, task_id=None):
        """Virtual state derived only from cached receipts, never physical ownership."""
        with self._lock:
            records = [self.task(cid) for cid, record in self._commands.items()
                       if task_id is None or record['task_id'] == task_id]
            active = next((r['command_id'] for r in records if r['state'] == 'running'), None)
            completed = sorted((r for r in records if r['state'] == 'succeeded'), key=lambda r:r['completed_at'])
            holding = completed[-1]['result'].get('holding') if completed else None
            return {'simulated': True, 'physical_execution': False, 'active_command_id': active, 'holding': holding}

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
                    if request.get("skill") not in MANIPULATION_OPERATIONS:
                        continue
                    raw = (attempt.get("progress", {}).get("evidence") or {}).get("downstream") or {}
                    if raw.get("simulated") is not True:
                        continue
                    command_id = attempt.get("command_id")
                    view = interpret_observation(command_id, raw, request, allow_simulated=True)
                    evidence = view.get("evidence") or {}
                    if (view.get("terminal") and view.get("stopped")
                            and evidence.get("identity_ok") and evidence.get("time_ok")
                            and raw.get("task_id") == record.get("task_id")):
                        self._commands[command_id] = deepcopy(raw)
                        self._requests[command_id] = deepcopy(request["body"])


class HybridReceptionAdapter(BodyAdapter):
    def __init__(self, dream, vla, *, navigation=None):
        super().__init__(dream, vla)
        self.navigation = navigation

    @property
    def gate_open(self):
        return self.navigation.gate_open if self.navigation is not None else super().gate_open

    @property
    def requires_authoritative_handoff(self):
        return self.navigation is not None

    def submit(self, command_id, request):
        if request.get('skill') == 'navigate' and self.navigation is not None:
            local = self.vla.control_status(task_id=request['body']['task_id'])
            if local.get('active_command_id') or (request.get('required_holding')
                    and local.get('holding') != request['required_holding']):
                return self._refused(command_id, request, HttpContractError('本任务模拟操控／虚拟持物未核清',
                    status_code=409, payload={'success':False,'error':{'code':'SAFETY_NOT_READY',
                        'message':'本任务模拟操控／虚拟持物未核清'}}))
            # Preserve the exact Runtime request. BodyControl derives the simulated
            # holding scope from the authoritative backend, never a caller flag.
            return self.navigation.submit(command_id, request)
        if request.get('skill') == 'navigate' and request.get('requires_vla_clearance'):
            status = self.vla.control_status()
            if status.get('active_command_id') or (request.get('required_holding')
                    and status.get('holding') != request['required_holding']):
                return self._refused(command_id, request, HttpContractError('模拟操控状态或持物未核清',
                    status_code=409, payload={'success':False,'error':{'code':'SAFETY_NOT_READY',
                                                                   'message':'模拟操控状态或持物未核清'}}))
            # Only the local simulation state is checked here; there is no real VLA lease.
            return super().submit(command_id, {**request, 'requires_vla_clearance':False})
        if request.get('skill') not in NATIVE_OPERATIONS:
            return super().submit(command_id, request)
        try:
            raw = self.vla.submit_task(request['body'])
        except HttpContractError as exc:
            return self._refused(command_id, request, exc)
        return {'accepted':True,'unclear':False,'completed':False,'command_id':command_id,'raw':raw}

    @staticmethod
    def _refused(command_id, request, error):
        refusal = submission_refusal(error.status_code, error.payload, command_id=command_id,
                                     task_id=request.get('task_id'))
        return {'accepted':False,'unclear':refusal is None,'completed':False,'command_id':command_id,
                'error':str(error),'raw':error.payload,'submission_rejected':refusal}

    def query(self, command_id, request):
        if request.get('skill') == 'navigate' and self.navigation is not None:
            return self.navigation.query(command_id, request)
        if request.get('skill') not in NATIVE_OPERATIONS:
            return super().query(command_id, request)
        try:
            raw = self.vla.task(command_id)
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)
        return interpret_observation(command_id, raw, request, allow_simulated=True)

    def wait(self, command_id, request):
        if request.get('skill') == 'navigate' and self.navigation is not None:
            return self.navigation.wait(command_id, request)
        if request.get('skill') not in NATIVE_OPERATIONS:
            return super().wait(command_id, request)
        try:
            raw = self.vla.wait_task(command_id, timeout_sec=float(request.get('deadline_sec') or 30),
                                     poll_interval_sec=float(request.get('poll_interval_sec') or 1.0))
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)
        return interpret_observation(command_id, raw, request, allow_simulated=True)

    @classmethod
    def from_config(cls, config, *, body_control=None):
        from brain.adapters.dream_client import DreamClient
        real = config.get("reception_real") or {}
        dream = DreamClient(real.get("dream_base_url"),
                            contract_version=real.get("contract_version") or CONTRACT_VERSION,
                            request_timeout_sec=float(real.get("request_timeout_sec", 10)))
        navigation = None
        if body_control is not None:
            from brain.adapters.body_control import ControlledBodyAdapter
            navigation = ControlledBodyAdapter(dream, None, body_control)
        return cls(dream, SimulatedManipulationClient(dream), navigation=navigation)

    def cancel(self, command_id, request):
        if request.get('skill') == 'navigate' and self.navigation is not None:
            return self.navigation.cancel(command_id, request)
        return super().cancel(command_id, request)

    def restore_receipts(self, record):
        self.vla.restore_receipts(record)

    def handoff_with_context(self, context):
        answer = {"available": False, "confirmed": False, "simulated": True,
                  "physical_control_transfer": False, "kind": context["kind"],
                  "reason": "source_command_unavailable"}
        command_id, request = context.get("source_command_id"), context.get("source_request")
        if not command_id or not request:
            return answer
        source = self.query(command_id, request)
        answer["source"] = source
        reason = self._source_handoff_blocker(source)
        if reason:
            answer["reason"] = reason
            return answer
        try:
            nav = self.dream.status()
        except HttpContractError as exc:
            answer.update(reason="control_status_unavailable", error=str(exc))
            return answer
        ready_key = ('interface_online' if self.navigation is not None
                     and getattr(self.navigation.authority, 'mode', '') != 'source_gate' else 'navigation_transport_ready')
        idle = (nav.get(ready_key) is True
                and "active_command_id" in nav and nav["active_command_id"] in (None, "")
                and "active_command_state" in nav and nav["active_command_state"] is None)
        if request.get('required_holding'):
            local = self.vla.control_status(task_id=request['body']['task_id'])
            idle = idle and not local.get('active_command_id') and local.get('holding') == request['required_holding']
        if self.navigation is not None:
            # A simulated manipulation receipt cannot clear an unresolved physical lease.
            idle = idle and not self.navigation.authority.recovery_status()['blocked']
        answer.update(available=True, confirmed=idle, navigation_status=nav,
                      reason="simulated_manipulation_boundary" if idle else "transport_not_ready")
        return answer
