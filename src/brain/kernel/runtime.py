"""The only writer of task state for the phase-1 kernel."""

from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from datetime import datetime

from contracts.tasks import (
    CONTRACT_VERSION,
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    AttemptRecord,
    IllegalTransition,
    KernelError,
    ProgressEvent,
    Rejected,
    SkillContract,
    StaleWrite,
    evidence_filename,
    make_command_id,
)
from brain.kernel.memory import append_observation, event_window_settings, read_subject
from contracts.steps import step_spec_dict, steps_from_specs
from contracts.submission import submission_refusal, refusal_observation
from brain.kernel.runner import Runner
from brain.kernel.verifier import Verifier
from shared.task_text import failure_text, phase_labels, scope_manipulation_advice
from contracts.vla import NATIVE_OPERATIONS
from contracts.vla_direct import o6_feedback_fault


_TRANSITIONS = {
    ("running", "submission_rejected"): "recovery_required",
    ("recovery_required", "submission_rejected"): "recovery_required",
    ("running", "action_finished"): "verifying",
    ("recovery_required", "manual_skip"): "running",
    ("paused", "manual_skip"): "running",
    ("waiting_human", "manual_skip"): "running",
    ("waiting_human", "skip_final"): "failed",
    ("paused", "skip_final"): "failed",
    ("recovery_required", "skip_final"): "failed",
    ("verifying", "finish_with_skips"): "failed",
    ("verifying", "pass_continue"): "running",
    ("verifying", "pass_final"): "succeeded",
    ("verifying", "fail"): "recovery_required",
    ("verifying", "unknown"): "recovery_required",
    ("running", "unknown"): "recovery_required",
    ("running", "gate"): "waiting_human",
    ("waiting_human", "human_continue"): "running",
    ("waiting_human", "vla_completion_unconfirmed"): "recovery_required",
    ("waiting_human", "vla_terminal_failed"): "recovery_required",
    ("waiting_human", "pause_ack"): "paused",
    ("waiting_human", "pause_nack"): "recovery_required",
    ("running", "pause_ack"): "paused",
    ("running", "pause_nack"): "recovery_required",
    ("verifying", "pause_ack"): "paused",
    ("verifying", "pause_nack"): "recovery_required",
    ("paused", "resume_pause"): "running",
    ("paused", "continue_retry"): "running",
    ("paused", "continue_verify"): "verifying",
    ("recovery_required", "continue_retry"): "running",
    ("recovery_required", "continue_verify"): "verifying",
    ("paused", "resume_verify"): "verifying",
    ("running", "requery_stopped"): "recovery_required",
    ("cancelling", "cancel_cleared"): "cancelled",
    ("cancelling", "cancel_unclear"): "recovery_required",
    ("recovery_required", "requery_not_started"): "running",
    ("recovery_required", "requery_ended"): "verifying",
    ("recovery_required", "retry_allowed"): "running",
    ("recovery_required", "retry_handoff"): "running",
    ("running", "handoff_unconfirmed"): "recovery_required",
    ("verifying", "handoff_unconfirmed"): "recovery_required",
}


def _blank_record():
    return {
        "task_id": None,
        "task_desc": "",
        "state": None,
        "cursor": 0,
        "phase": "",
        "breakpoint_phase": "",
        "steps": {},
        "open_command_id": None,
        "command_unknown": False,
        "not_started_confirmed": False,
        "stopped_confirmed": False,
        "resources_cleared": True,
        "object_location": None,
        "holding": None,
        "dispatch_counts": {},
        "blocked_reason": "",
        "safe_idle": False,
        "contract_version": CONTRACT_VERSION,
        "package": "reception",
        "failure_budget": 0,
        "phase_order": [],
        "pending_progress": None,
        "revision": 0,
        "dispatch_closed": False,
        "control_request": "",
        "observations": [],
        "release_id": "",
        "release_rules": [],
    }


def _failure_receipt(raw):
    """Copy the peer's error/result envelope, including unfamiliar nested fields."""
    if not isinstance(raw, dict):
        return {}
    return {key: deepcopy(raw[key]) for key in ('command_id', 'task_id', 'state', 'error', 'result')
            if key in raw}


def status_from_record(record) -> dict:
    state = (record or {}).get("state")
    terminal = state in TERMINAL_STATES
    advice = scope_manipulation_advice(deepcopy((record or {}).get("recovery_advice")), record)
    if advice:
        advice["applicable"] = advice.get("record_revision") == (record or {}).get("revision")
        if advice.get('action') == 'revise_plan':
            from brain.recovery import can_revise
            advice['applicable'] = advice['applicable'] and can_revise(record)
    tasks = []
    auto_options = (record or {}).get('door_verification') or {}
    auto_grasp = (auto_options.get('enabled') is True and auto_options.get('grasp_evidence_contract') in {'fq/vla-grasp-cola-wrist-arm-evidence/v1','fq/vla-grasp-cola-wrist-return-evidence/v2'})
    auto_close = (auto_options.get('enabled') is True
                  and auto_options.get('close_evidence_contract') == 'fq/vla-close-fridge-head-arm-evidence/v1')
    failure_history = []
    retry_count = 0
    manual_retry_count = 0
    for order, spec in enumerate((record or {}).get("phase_specs") or [], 1):
        if (auto_grasp and spec.get('kind') == 'verify' and order > 1
                and record['phase_specs'][order-2].get('kind') == 'grasp_cola'):
            continue
        bucket = (record or {}).get("steps", {}).get(spec["step_id"]) or {}
        attempts = bucket.get("attempts") or []
        submitted = [a for a in attempts if a.get("submitted")]
        retry_count += max(0, len(submitted) - 1)
        manual_retry_count += sum(a.get("authorized_by") in {"human_continue", "human_recovery_plan"}
                                  for a in submitted[1:])
        for old in attempts:
            old_progress = old.get("progress") or {}
            if old.get("verdict") != "FAIL" and old_progress.get("terminal") not in {"failed", "cancelled"}:
                continue
            raw = (old_progress.get("evidence") or {}).get("downstream") or {}
            rejection = (old_progress.get("evidence") or {}).get("submission_rejected") or {}
            error = raw.get("error") if isinstance(raw, dict) else None
            error = error if isinstance(error, dict) else {}
            result = raw.get('result') if isinstance(raw, dict) else None
            result = result if isinstance(result, dict) else {}
            message = rejection.get("message") or error.get("message") or result.get('message') or old_progress.get("error") or old.get("outcome") or "fail"
            code = rejection.get("code") or error.get("code")
            failure_history.append({"step_id": spec["step_id"], "command_id": old.get("command_id"),
                                    "attempt_id": old.get("attempt_id"), "code": code,
                                    "downstream": _failure_receipt(raw),
                                    "kind": "rejected" if rejection else "unknown" if old.get("verdict") == "UNKNOWN" else "cancelled" if old_progress.get("terminal") == "cancelled" else "execution_failed",
                                    "message": message, "display_message": failure_text(message, code, record=record, details=error.get('details'))})
            fault = o6_feedback_fault(raw, task_id=record.get('task_id'), command_id=old.get('command_id'))
            if fault is not None:
                failure_history[-1]['feedback_fault'] = fault
        latest = attempts[-1] if attempts else {}
        verdict = latest.get("verdict")
        evidence = (latest.get("progress") or {}).get("evidence") or {}
        marker = bucket.get("status")
        manual_skip = marker == "manual_skipped"
        conditions_assumed = manual_skip and any(item.get('step_id') == spec['step_id']
            and item.get('task_id') == record.get('task_id') and item.get('command_id') == latest.get('command_id')
            and item.get('conditions_satisfied') is True for item in record.get('manual_skips') or [])
        simulated = ((record or {}).get("execution_backend") in {"reception_real_sim_manipulation",
                                                                "reception_ros_sim_manipulation"}
                     and spec.get("kind") in {"pick", "place", "open_fridge", "grasp_cola", "close_fridge", "close_fridge_test", "verify"})
        simulated = simulated or ((record or {}).get('execution_backend') == 'reception_sim_navigation_real'
                                   and spec.get('kind') == 'navigate')
        done = verdict == "PASS" or marker in {"done", "skipped", "manual_skipped"}
        tasks.append({"order": len(tasks)+1, "robot_name": spec.get("robot_name") or "FQrobot",
                      "display_name": spec.get('display_name') or '',
                      "subtask": spec.get("key") or spec["step_id"], "done": done,
                      "step_id": spec["step_id"], "skipped": manual_skip, "simulated": simulated,
                      "status": "skipped" if manual_skip else "success" if done else "failure" if verdict == "FAIL" else "unknown" if verdict == "UNKNOWN" else "pending",
                      "conditions_assumed": conditions_assumed,
                      "result": ("人工跳过；按条件满足继续" if conditions_assumed else "人工跳过；未确认该步骤完成") if manual_skip else evidence.get("text") or evidence.get("detail") or latest.get("outcome") or "",
                      "reported_success": evidence.get("reported_success"),
                      "evidence_level": evidence.get("reported_evidence_level") or evidence.get("grade"),
                      "effect_verified": not manual_skip and (verdict == "PASS" or (spec.get("kind") == "verify"
                          and marker == "done" and bool(bucket.get("verified_attempt_id"))))})
    cursor = int((record or {}).get("cursor") or 0)
    specs = (record or {}).get("phase_specs") or []
    current = specs[cursor] if 0 <= cursor < len(specs) else {}
    attempts = ((record or {}).get("steps", {}).get(current.get("step_id")) or {}).get("attempts") or []
    progress = (attempts[-1].get("progress") or {}) if attempts else {}
    evidence = progress.get("evidence") or {}
    downstream = evidence.get("downstream") or {}
    remote_error = downstream.get("error") if isinstance(downstream, dict) else None
    receipt = evidence.get("submission_rejected")
    failure = None
    if state == "recovery_required":
        if isinstance(receipt, dict):
            failure = {"kind": "rejected", "code": receipt.get("code"),
                       "message": receipt.get("message"), "not_started": True}
        elif isinstance(remote_error, dict):
            failure = {"kind": "execution_failed" if progress.get("terminal") in {"failed", "cancelled"} else "unknown",
                       "code": remote_error.get("code"),
                       "message": remote_error.get("message"), "not_started": False}
        elif progress.get('terminal') in {'failed', 'cancelled'}:
            result = downstream.get('result') if isinstance(downstream, dict) else None
            result = result if isinstance(result, dict) else {}
            failure = {'kind': 'execution_failed', 'not_started': False,
                       'message': result.get('message') or progress.get('error') or progress['terminal']}
        elif progress.get("error"):
            failure = {"kind": "unknown", "message": progress["error"], "not_started": False}
    if failure:
        failure['downstream'] = _failure_receipt(downstream)
        fault = o6_feedback_fault(downstream, task_id=record.get('task_id'),
                                  command_id=(attempts[-1].get('command_id') if attempts else None))
        if fault is not None:
            failure['feedback_fault'] = fault
        failure["display_message"] = failure_text(failure.get("message"), failure.get("code"), record=record,
                                                 details=remote_error.get('details') if isinstance(remote_error, dict) else None)
    latest = attempts[-1] if attempts else {}
    command_stage = "prepared"
    if latest.get("submitted"):
        command_stage = "submitted"
    if progress.get("terminal"):
        command_stage = "terminal"
    if latest.get("verdict") == "UNKNOWN" or ((record or {}).get("command_unknown")
                                               and state not in {"running", "verifying"}):
        command_stage = "unknown"
    if latest.get("verdict") == "FAIL":
        command_stage = "cancelled" if progress.get("terminal") == "cancelled" else "failed"
    if latest.get("verdict") == "PASS":
        command_stage = "succeeded"
    if receipt:
        command_stage = "rejected"
    can_skip = (state in {"running", "verifying", "waiting_human", "recovery_required", "paused"}
                and current.get("kind") not in {None, "local", "verify"}
                and (record or {}).get("control_request") != "cancel"
                and (not attempts or attempts[-1].get("verdict") != "PASS"))
    return {
        "attempt_summary": {"retry_count": retry_count, "manual_retry_count": manual_retry_count,
                            "failure_count": len(failure_history)},
        "failure_history": failure_history,
        "can_skip": can_skip,
        "skip_step_id": current.get("step_id") if can_skip else None,
        "control_step_id": current.get("step_id"),
        "control_command_id": (attempts[-1].get("command_id") if attempts else None),
        "current_command": {"command_id": latest.get("command_id"),
                            "attempt_id": latest.get("attempt_id"), "stage": command_stage},
        "manual_skips": deepcopy((record or {}).get("manual_skips") or []),
        "flow_finished": bool((record or {}).get("flow_finished")),
        "active": bool(state) and not terminal,
        "task_id": (record or {}).get("task_id"),
        "task": (record or {}).get("task_desc") or "",
        "state": state,
        "terminal": terminal if state else True,
        "all_done": state == "succeeded",
        "failed": state in {"failed", "recovery_required"},
        "has_failures": state in {"failed", "recovery_required"} or bool((record or {}).get("manual_skips")),
        "blocks_new_motion": bool(state) and not terminal,
        "task_cancellation": deepcopy((record or {}).get("task_cancellation")),
        "can_resume": state in {"paused", "waiting_human", "recovery_required"},
        "pause_requested": (record or {}).get("control_request") == "pause",
        "execution_active": state == "running",
        "source": "kernel",
        "subtask_list": tasks,
        "phase_labels": phase_labels(record),
        "completed": sum(item["done"] for item in tasks),
        "total": len(tasks),
        "task_type": (record or {}).get("package"),
        "execution_backend": (record or {}).get("execution_backend"),
        "answer": (((record or {}).get("pending_progress") or {}).get("evidence") or {}).get("text", "") if state == "succeeded" else "",
        "blocked_reason": (record or {}).get("blocked_reason") or "",
        "failure_detail": failure,
        "command_unknown": bool((record or {}).get("command_unknown")),
        "phase": (record or {}).get("phase") or "",
        "selection": deepcopy((record or {}).get("selection")),
        "recovery_advice": advice,
        "recovery_policy": deepcopy((record or {}).get("recovery_policy")),
        "recovery_observation": deepcopy((record or {}).get("recovery_observation")),
        "plan_revision": (record or {}).get("plan_revision", 0),
        "plan_revisions": deepcopy((record or {}).get("plan_revisions", [])),
        "advice_confirmations": deepcopy((record or {}).get("advice_confirmations", [])),
        "handoff_observation": deepcopy((record or {}).get("handoff_observation")),
        "vla_confirmation": dict(deepcopy((record or {}).get('vla_confirmation') or {}),
                                 auto_verification=auto_options.get('mode') == 'auto' and (
                                     (auto_grasp and auto_options.get('grasp_evidence_contract') == 'fq/vla-grasp-cola-wrist-return-evidence/v2'
                                      and ((record or {}).get('vla_confirmation') or {}).get('operation') == 'grasp_cola')
                                     or (auto_close and ((record or {}).get('vla_confirmation') or {}).get('operation') in {'close_fridge','close_fridge_test'})),
                                 finish_intent=deepcopy((latest.get('effect_controls') or {}).get('finish')),
                                 finish_rejection=next((deepcopy(r) for r in reversed(latest.get('effect_rejections') or [])
                                                        if r.get('action') == 'finish'), None))
            if (record or {}).get('open_command_id')
            and (record or {}).get('open_command_id') == ((record or {}).get('vla_confirmation') or {}).get('command_id')
            and not terminal else None,
    }


def _same_command(left, right) -> bool:
    return (left or "") == (right or "")


def classify_query(result, *, requires_object_evidence: bool) -> str:
    if not isinstance(result, dict):
        return "unknown"
    started = result.get("started")
    stopped = result.get("stopped")
    if started is False and stopped is True:
        return "not_started"
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    terminal = result.get("terminal") or ""
    grade_ok = (not requires_object_evidence) or evidence.get("grade") == "object"
    if (
        terminal
        and evidence.get("supports") is True
        and evidence.get("identity_ok") is True
        and evidence.get("time_ok") is True
        and grade_ok
        and not result.get("timed_out")
    ):
        return "ended"
    if stopped is True:
        return "stopped"
    return "unknown"


class TaskRuntime:
    def __init__(self, store, port, *, policy, release_reader, config=None, package: str | None = None, event_sink=None):
        self.policy = policy
        self.release_reader = release_reader
        self.event_sink = event_sink
        self.store = store
        self.port = port
        self.config = config or {}
        self.runner = Runner()
        self.verifier = Verifier()
        self._lock = threading.RLock()
        self.planning_done = threading.Event()
        self.planning_done.set()
        loaded = store.load_state()
        if loaded is not None and loaded.get('state') not in NON_TERMINAL_STATES | TERMINAL_STATES:
            raise ValueError('任务账本状态无效，不能作为新任务覆盖')
        self.record = loaded if isinstance(loaded, dict) else _blank_record()
        self._owned_task_id = self.record.get("task_id")
        selected = package if package else (self.record.get("package") or "reception")
        self._bind_package(selected)

    def _bind_package(self, name: str, phases=None):
        self.package_name = name or "reception"
        if phases is not None:
            self.phases = list(phases)
            return
        if self.record.get("package") == self.package_name and "phase_specs" in self.record:
            self.phases = steps_from_specs(self.record.get("phase_specs") or [])
            return
        if self.package_name in {"generic", "desk"}:
            self.phases = []
            return
        self.phases = list(self.policy.phases(self.package_name))

    @property
    def state(self):
        return self.record.get("state")

    def _exclusive(self):
        writer = getattr(self.store, "writer", None)
        if callable(writer):
            return writer()
        return self._lock

    def _reload_locked(self, *, check_identity=True):
        if not callable(getattr(self.store, "writer", None)):
            return
        loaded = self.store.load_state()
        if isinstance(loaded, dict):
            if check_identity and self._owned_task_id and loaded.get("task_id") != self._owned_task_id:
                raise Rejected("任务已更换，旧 Runtime 不得推进新任务")
            old_plan_revision = self.record.get('plan_revision', 0)
            self.record = loaded
            if old_plan_revision != loaded.get('plan_revision', 0):
                self._bind_package(loaded.get('package'))

    def _halted(self) -> bool:
        if self.state in TERMINAL_STATES or self.state in {
            "paused", "waiting_human", "cancelling", "recovery_required",
        }:
            return True
        if self.record.get("dispatch_closed") and self.state in {"running", "verifying"}:
            return True
        if self.record.get("requery_hold") and self.state == "running":
            return True
        return False

    def public_status(self) -> dict:
        with self._exclusive():
            self._reload_locked()
            return status_from_record(self.record)

    def snapshot(self):
        with self._exclusive():
            self._reload_locked()
            return deepcopy(self.record)

    def bind_execution(self, selection, backend, target, port):
        """Bind a selected package before planning any actions, on the Runtime owner."""
        with self._exclusive():
            self._reload_locked()
            if self.state not in {"running", "paused"} or self.record.get("control_request") == "cancel":
                return False
            if self.record.get("plan_ready") or self.record.get("open_command_id") or self.record.get("dispatch_counts"):
                raise Rejected("执行已开始，不能更换业务包或执行目标")
            self._bind_package(selection["package"], [])
            self.port = port
            self.record["package"] = self.package_name
            self.record["selection"] = deepcopy(selection)
            self.record["execution_backend"] = backend
            self.record["execution_target"] = deepcopy(target)
            from brain.recovery import bind_policy
            self.record["recovery_policy"] = bind_policy(self.policy, self.package_name, backend, port, self.config)
            bound = self.release_reader(self.config, self.package_name)
            self.record["release_id"], self.record["release_rules"] = bound["version_id"], bound["rules"]
            self._save("execution_selected")
            return True

    def record_advice(self, context, advice):
        """Late model output cannot affect a changed task, command or control request."""
        with self._exclusive():
            try:
                self._reload_locked()
            except Rejected:
                return False
            if any(self.record.get(key) != context.get(key) for key in
                   ("task_id", "revision", "state", "open_command_id")):
                return False
            if advice.get('action') == 'revise_plan':
                from brain.recovery import validate_plan
                try:
                    validate_plan(self.record, advice.get('plan'))
                except Rejected as exc:
                    from brain.reasoning import fallback_advice
                    advice = dict(fallback_advice(context), source='rules', error=str(exc))
            self.record["recovery_advice"] = {
                **deepcopy(advice), "task_id": self.record["task_id"],
                "advice_id": uuid.uuid4().hex,
                "step_id": status_from_record(self.record).get("control_step_id"),
                "control_command_id": status_from_record(self.record).get("control_command_id") or "",
                "execution_target": deepcopy(self.record.get("execution_target")),
                "facts": {key: deepcopy(context.get(key)) for key in
                          ("state", "phase", "blocked_reason", "command_unknown", "original_query", "established_facts", "attempts", "recovery_planning")},
                "command_id": self.record.get("open_command_id"),
                "based_on_revision": self.record["revision"],
                "record_revision": self.record["revision"] + 1,
                "allowed_actions": list(context["allowed_actions"]),
                "automatic": False,
            }
            self._save("recovery_advised")
            return True

    def claim_advice(self, advice_id, expected_revision):
        """Persist one human authorization before any I/O; never replay a claim."""
        from brain.reasoning import recovery_context, RECOVERY_STATES
        with self._exclusive():
            self._reload_locked()
            for receipt in self.record.get("advice_confirmations", []):
                if receipt["advice_id"] == advice_id:
                    return deepcopy(receipt), False
            advice = self.record.get("recovery_advice") or {}
            current = status_from_record(self.record)
            if (self.state not in RECOVERY_STATES or not self.record.get("phase_specs")
                    or (self.record.get("package") != "reception" and
                        (self.record.get('recovery_policy') or {}).get('mode') not in {'preserve_order', 'reorder_remaining'})
                    or advice.get("advice_id") != advice_id
                    or advice.get("record_revision") != expected_revision
                    or self.record["revision"] != expected_revision
                    or advice.get("step_id") != current.get("control_step_id")
                    or advice.get("control_command_id") != (current.get("control_command_id") or "")
                    or advice.get("execution_target") != self.record.get("execution_target")
                    or advice.get("action") not in recovery_context(self.record)["allowed_actions"]):
                raise Rejected("建议已失效或当前任务不支持建议确认，请刷新后再操作")
            if advice.get('action') == 'revise_plan':
                from brain.recovery import validate_plan
                validate_plan(self.record, advice.get('plan'))
            receipt = {"advice_id": advice_id, "task_id": self.record["task_id"],
                       "step_id": advice["step_id"], "command_id": advice["control_command_id"],
                       "action": advice["action"], "status": "processing", "automatic": False,
                       "confirmed_at": datetime.now().astimezone().isoformat(),
                       "advice": deepcopy(advice)}
            self.record.setdefault("advice_confirmations", []).append(receipt)
            self._save("advice_confirmed")
            return deepcopy(receipt), True

    def finish_advice(self, advice_id, result):
        with self._exclusive():
            try:
                self._reload_locked()
            except Rejected:
                return
            for receipt in self.record.get("advice_confirmations", []):
                if receipt["advice_id"] == advice_id:
                    receipt.update(status="processed" if result.get("accepted") else "rejected",
                                   result=deepcopy(result), finished_at=datetime.now().astimezone().isoformat())
                    self._save("advice_control_result")
                    return deepcopy(receipt)

    def _recovery_claim(self, advice_id, action):
        receipt = next((r for r in self.record.get('advice_confirmations', [])
                        if r.get('advice_id') == advice_id), None)
        if not receipt or receipt.get('status') != 'processing' or receipt.get('action') != action:
            raise Rejected('缺少有效的本次恢复授权')
        return receipt

    def observe_recovery_scene(self, advice_id):
        """One approved read; record its scope without certifying physical effects."""
        from brain.recovery import available_actions, latest_attempt
        with self._exclusive():
            self._reload_locked()
            self._recovery_claim(advice_id, 'observe_scene')
            if 'observe_scene' not in available_actions(self.record):
                raise Rejected('当前任务不支持现场状态刷新')
            snapshot = deepcopy(self.record)
        started = datetime.now().astimezone()
        reader = getattr(self.port, 'observe_recovery', None)
        if not callable(reader):
            raise Rejected('当前执行端没有只读现场能力')
        value = reader()
        if (not isinstance(value, dict)
                or any(not isinstance(value.get(key), str) or not value[key] for key in ('scope', 'source'))
                or value.get('success') is False):
            raise Rejected('现场状态回包缺少来源或范围')
        with self._exclusive():
            self._reload_locked()
            if self.record['revision'] != snapshot['revision'] or self.state != snapshot['state']:
                raise Rejected('读取现场期间任务已变化，未应用迟到结果')
            observation = {'observation_id': uuid.uuid4().hex, 'task_id': snapshot['task_id'],
                           'command_id': latest_attempt(snapshot).get('command_id'),
                           'plan_revision': snapshot.get('plan_revision', 0),
                           'observed_at': started.isoformat(), 'data': deepcopy(value),
                           'timestamp_scope': 'HTTP read start, not sensor capture time',
                           'advice_id': advice_id}
            self.record['recovery_observation'] = observation
            self.record.setdefault('recovery_observations', []).append(deepcopy(observation))
            self._save('recovery_scene_observed')
            return {'accepted': True, 'observation_only': True,
                    'observation_id': observation['observation_id'], 'state': self.state,
                    'message': '现场接口状态已记录，正在重新分析；未下发动作'}

    def apply_recovery_plan(self, advice_id):
        """Atomically revise only the unfinished suffix after a fresh original query."""
        from brain.recovery import validate_plan, refuses_retry
        with self._exclusive():
            self._reload_locked()
            receipt = self._recovery_claim(advice_id, 'revise_plan')
            proposal = deepcopy(receipt['advice']['plan'])
            validate_plan(self.record, proposal)
            snapshot = deepcopy(self.record)
            step = self._current_step()
            attempt = self._latest(step.step_id)
            command_id, request = attempt['command_id'], deepcopy(attempt['request'])
        result = self.port.query(command_id, request)
        with self._exclusive():
            self._reload_locked()
            if self.record['revision'] != snapshot['revision'] or self.state != snapshot['state']:
                raise Rejected('核查原命令期间任务已变化，未应用恢复方案')
            reordered = validate_plan(self.record, proposal)
            evidence = result.get('evidence') or {} if isinstance(result, dict) else {}
            if (not isinstance(result, dict) or result.get('command_id') != command_id
                    or result.get('timed_out') or result.get('terminal') != 'failed'
                    or result.get('stopped') is not True or result.get('resources_released') is not True
                    or not isinstance(evidence, dict)
                    or evidence.get('identity_ok') is not True or evidence.get('time_ok') is not True
                    or refuses_retry(result, self.record.get('execution_backend'))):
                raise Rejected('原动作未确认失败并停止、资源未释放或下游禁止重试；方案未执行')
            # Do not overwrite the failed receipt: save the fresh check with the revision.
            before = deepcopy(self.record['phase_specs'])
            cursor = int(self.record.get('cursor') or 0)
            after = before[:cursor] + reordered
            version = self.record.get('plan_revision', 0) + 1
            change = {'version': version, 'advice_id': advice_id,
                      'reason': receipt['advice']['reason'], 'summary': receipt['advice']['summary'],
                      'observation_id': proposal['observation_id'], 'original_command_id': command_id,
                      'original_check': deepcopy(result), 'before': before, 'after': deepcopy(after),
                      'applied_at': datetime.now().astimezone().isoformat()}
            # A query may resolve an originally UNKNOWN send. Keep its original
            # progress untouched and retain the identity-checked failure separately.
            latest = self._latest(step.step_id)
            latest.update(finished=True, verdict='FAIL', outcome='failed',
                          recovery_original_check=deepcopy(result))
            self.record.setdefault('plan_revisions', []).append(change)
            self.record.update(plan_revision=version, phase_specs=after,
                               phase_order=[s['step_id'] for s in after],
                               open_command_id=None, pending_progress=None, command_unknown=False,
                               stopped_confirmed=True, resources_cleared=True,
                               dispatch_closed=False, control_request='', requery_hold=False,
                               blocked_reason='', breakpoint_phase='', phase=after[cursor]['step_id'])
            self._bind_package(self.package_name, steps_from_specs(after))
            self.apply('continue_retry')
            self._save('recovery_plan_applied')
            return {'accepted': True, 'plan_revised': True, 'plan_revision': version,
                    'state': self.state, 'message': '恢复方案已接受，按原核验要求执行；受理不等于完成'}

    def query_advice_original(self):
        """Refresh original evidence without resuming, verifying or issuing any action."""
        with self._exclusive():
            self._reload_locked()
            command_id = self.record.get("open_command_id")
            if self.state not in {"recovery_required", "paused"} or not command_id:
                raise Rejected("当前没有可查询的原命令")
            step = self._step_of_command(command_id)
            attempt = self._latest(step.step_id) if step else None
            if not attempt or attempt["command_id"] != command_id:
                raise Rejected("原命令与步骤不匹配")
            revision, state = self.record["revision"], self.state
            contract = self._contract(step, attempt["attempt_id"], command_id, deepcopy(attempt["request"]))
        observed = self.port.query(command_id, contract.request)
        if not isinstance(observed, dict) or observed.get("command_id") != command_id:
            raise Rejected("查询回执的原命令身份不符，未改变任务")
        progress = self.runner._progress(contract, observed)
        with self._exclusive():
            self._reload_locked()
            if (self.record["revision"] != revision or self.state != state
                    or self.record.get("open_command_id") != command_id):
                raise Rejected("查询期间任务已变化，未应用迟到结果")
            evidence = progress.evidence or {}
            known = (progress.command_id == command_id and not progress.timed_out
                     and evidence.get("identity_ok") is True and evidence.get("time_ok") is True
                     and progress.publisher_stopped is True and evidence.get("resources_released") is True
                     and (progress.terminal in {"succeeded", "failed", "cancelled"} or progress.started is False))
            if state == 'paused':
                self._record_pause_query(step, contract, observed)
                known = not self.record['command_unknown']
            else:
                self.record.update(stopped_confirmed=known, resources_cleared=known,
                                   command_unknown=not known)
            self.record.update(last_query_command_id=command_id, advice_query=progress.as_dict())
            self._save("advice_original_queried")
            return {"accepted": True, "query_only": True, "command_id": command_id,
                    "state": self.state, "result_known": known,
                    "message": "原命令结果已记录；未重试或推进任务"}

    def dispatch_count(self, step_id: str) -> int:
        return int((self.record.get("dispatch_counts") or {}).get(step_id) or 0)

    def open_task(self, task_id: str, *, task_desc: str = "", force_new: bool = False,
                  phases=None, execution_backend=None, execution_target=None, planning=False, reflection_enabled=True, origin=None):
        del force_new  # 不能越过未核清资源
        with self._exclusive():
            self._reload_locked(check_identity=False)
            self._reject_if_blocked("拒绝新任务")
            if phases is not None:
                self._bind_package(self.package_name, phases)
            elif planning:
                # Loaded phases belong to the previous task, including when
                # the new planner fails before it can return a replacement.
                self._bind_package(self.package_name, [])
            if self.package_name in {"generic", "desk"} and not self.phases and not planning:
                raise Rejected("通用任务没有计划步骤")
            revision = int(self.record.get("revision") or 0)
            self.record = _blank_record()
            self.record["revision"] = revision
            self.record["task_id"] = str(task_id)
            self._owned_task_id = str(task_id)
            self.record["task_desc"] = task_desc
            from brain.adapters.door_verification import settings as door_settings
            door = door_settings(self.config)
            # Only new tasks opt in. Never retroactively authorize old waiting commands.
            self.record['door_verification'] = {k: v for k, v in door.items() if k != 'vlm'}
            self.record["package"] = self.package_name
            self.record["phase_order"] = [step.step_id for step in self.phases]
            self.record["phase_specs"] = [step_spec_dict(step) for step in self.phases]
            self.record["execution_backend"] = execution_backend
            self.record["execution_target"] = execution_target
            self.record["plan_ready"] = not planning
            if planning:
                self.planning_done.clear()
            self.record["reflection_enabled"] = reflection_enabled
            self.record.update({key: (origin or {}).get(key) for key in ("entry", "environment_revision")})
            bound = self.release_reader(self.config, self.package_name)
            self.record["release_id"] = bound["version_id"]
            self.record["release_rules"] = bound["rules"]
            self.apply("accept")
            try:
                self._save("task_opened")
            except StaleWrite:
                self._reload_locked()
                raise Rejected("并发写入已被拒绝")
        return self.public_status()

    def plan(self, planner):
        """Own the planning request; a planner supplies steps and never submits them."""
        try:
            plan = planner()
            steps = list(plan)
            if not steps or len({step.step_id for step in steps}) != len(steps):
                raise Rejected("计划为空或步骤身份重复")
            with self._exclusive():
                self._reload_locked()
                if self.state not in {"running", "paused"} or self.record.get("control_request") == "cancel":
                    return
                self._bind_package(self.package_name, steps)
                self.record["phase_specs"] = [step_spec_dict(step) for step in steps]
                self.record["phase_order"] = [step.step_id for step in steps]
                self.record["plan_ready"] = True
                self.record["reasoning_explanation"] = str(getattr(plan, "reasoning", ""))
                self._save("plan_ready")
        except Exception as exc:
            self.fail_closed(str(exc))
        finally:
            self.planning_done.set()

    def fail_closed(self, reason):
        with self._exclusive():
            self._reload_locked()
            if self.state in {"running", "verifying"}:
                self.record["blocked_reason"] = reason
                self.record["command_unknown"] = bool(self.record.get("open_command_id"))
                self.apply("unknown")
                self._save("execution_error")

    def note_still_waiting(self):
        with self._exclusive():
            self._reload_locked()
            if self.state != "waiting_human":
                raise IllegalTransition("只有等待人工时可以继续等待")
            return self.state

    def _record_pause_query(self, step, contract, observed):
        """Keep the original command evidence and pause flags in one Runtime write."""
        attempt = self._latest(step.step_id)
        attempt['pause_query'] = deepcopy(observed)
        progress = self.runner._progress(contract, observed)
        evidence = progress.evidence
        valid = (observed.get('command_id') == contract.command_id and not progress.timed_out
                 and evidence.get('identity_ok') is True and evidence.get('time_ok') is True)
        known = valid and (progress.terminal in {'succeeded', 'failed', 'cancelled'}
                           or progress.started is False)
        stopped = known and progress.publisher_stopped is True
        self.record.update(command_unknown=not known, stopped_confirmed=stopped,
                           resources_cleared=stopped and evidence.get('resources_released') is True,
                           not_started_confirmed=known and progress.started is False)
        self.record.pop('advice_query', None)  # An older query must not outrank this pause observation.
        if valid:
            self._stamp_attempt(step, progress, finished=bool(progress.terminal))
            # A success observed while pausing still needs the ordinary Verifier
            # after explicit continue. Cancellation is never step completion.
            attempt['verdict'] = 'FAIL' if progress.terminal in {'failed', 'cancelled'} else ''
            self.record['pending_progress'] = progress.as_dict()
        else:
            # Never let a preceding (possibly simulated) step describe this command.
            self.record['pending_progress'] = None

    def request_pause(self, *, stop_acknowledged: bool | None = None, expected_step_id=None, wait_for_stop=False):
        with self._exclusive():
            self._reload_locked()
            if expected_step_id and (self._current_step() is None or self._current_step().step_id != expected_step_id):
                raise Rejected("当前环节已变化，未停止其他环节")
            native_wait = (self.state == 'waiting_human' and self._current_step()
                           and self._current_step().kind in NATIVE_OPERATIONS
                           and self.record.get('open_command_id'))
            if self.state not in {"running", "verifying"} and not native_wait:
                raise IllegalTransition("只有运行或核验中可以暂停")
            if self.record.get("control_request") == "cancel":
                raise IllegalTransition("取消已开始，不能再暂停")
            task_id, resume_state = self.record['task_id'], self.state
            command_id = self.record.get("open_command_id")
            step = self._step_of_command(command_id) if command_id else None
            attempt = self._latest(step.step_id) if step else None
            if command_id and (not attempt or attempt.get('command_id') != command_id):
                raise Rejected('原命令与暂停环节不匹配')
            contract = self._contract(step, attempt['attempt_id'], command_id,
                                      deepcopy(attempt['request'])) if command_id else None
            self.record['dispatch_closed'] = True
            self.record['control_request'] = 'pause'
            self.record['pause_resume_state'] = resume_state
            self._save("pause_intent")
        observed, cancel_error = {}, ''
        if command_id:
            try:
                self.port.cancel(command_id, contract.request)
            except Exception as exc:
                cancel_error = str(exc)
            # A lost cancel acknowledgement does not prevent querying the original
            # command; an accepted cancel alone is not proof that it stopped.
            try:
                observed = self.port.query(command_id, contract.request)
                if not isinstance(observed, dict):
                    observed = {'error': '暂停查询回执无效'}
                if wait_for_stop and step.kind in NATIVE_OPERATIONS and not observed.get('terminal'):
                    wait = getattr(self.port, 'wait_completion', None)
                    if callable(wait):
                        request = dict(contract.request, deadline_sec=min(30., float(contract.deadline_sec)))
                        observed = wait(command_id, request)  # Original command GETs only; no cancel/finish replay.
            except Exception as exc:
                observed = {'error': str(exc)}
        with self._exclusive():
            self._reload_locked()
            current = self._latest(step.step_id) if step else None
            if (self.record.get('task_id') != task_id
                    or self.record.get('open_command_id') != command_id
                    or self.record.get("control_request") != "pause" or self.state != resume_state
                    or (contract and (not current or current.get('attempt_id') != contract.attempt_id))):
                return self.state
            if command_id:
                self._record_pause_query(step, contract, observed)
                if cancel_error:
                    current['pause_cancel_error'] = cancel_error
                stop_acknowledged = stop_acknowledged is not False and self.record['stopped_confirmed']
            elif stop_acknowledged is None:
                stop_acknowledged = True
            self.record["blocked_reason"] = "" if stop_acknowledged else "pause_without_stop_ack"
            self.apply("pause_ack" if stop_acknowledged else "pause_nack")
            self._save("pause_requested")
        return self.state

    def resume_paused(self):
        with self._exclusive():
            self._reload_locked()
            self.apply("resume_verify" if self.record.get("pause_resume_state") == "verifying" else "resume_pause")
            self.record["dispatch_closed"] = False
            self.record["control_request"] = ""
            self._save("pause_resumed")

    def human_continue(self):
        with self._exclusive():
            self._reload_locked()
            self.apply("human_continue")
            self.record["dispatch_closed"] = False
            self.record["control_request"] = ""
            self._save("human_continue")

    def _clear_cancel_locked(self):
        """Abandon brain orchestration only; never manufacture a remote stop receipt."""
        if self.state == "cancelled":
            return
        self.record["task_cancellation"] = {
            "scope": "task_only", "at": datetime.now().astimezone().isoformat(),
            "abandoned_command_id": self.record.get("open_command_id"),
            "previous_state": self.state,
            "remote_command_unknown": bool(self.record.get("command_unknown")),
            "remote_stopped_confirmed": bool(self.record.get("stopped_confirmed")),
            "remote_resources_cleared": bool(self.record.get("resources_cleared")),
        }
        self.record.update(state="cancelled", open_command_id=None, command_unknown=False,
                           dispatch_closed=True, control_request="cancel", blocked_reason="",
                           pending_progress=None, requery_hold=False)
        self.record.pop("execution_stop", None)

    def reconcile_cancel_intent(self):
        """Finish an already requested task cancellation locally after an upgrade."""
        with self._exclusive():
            self._reload_locked()
            if self.record.get("control_request") == "cancel" and self.state in NON_TERMINAL_STATES:
                self._clear_cancel_locked()
                self._save("task_cancelled")

    def request_cancel(self):
        with self._exclusive():
            self._reload_locked()
            if self.state == "cancelled":
                return {"accepted": True, "completed": True, "scope": "task_only"}
            if self.state not in NON_TERMINAL_STATES:
                raise IllegalTransition("终态不能再取消")
            command_id = self.record.get("open_command_id")
            self._clear_cancel_locked()
            self._save("task_cancelled")
            return {"accepted": True, "completed": True, "command_id": command_id,
                    "scope": "task_only"}

    def settle_cancel(self):
        # Compatibility for older callers: cancellation never queries the executor.
        if self.state == "cancelling":
            self.request_cancel()
        return self.state

    def reconcile_cancelled_resources(self):
        """Read the abandoned command before environment application, never resume it."""
        with self._exclusive():
            self._reload_locked()
            if self.state != 'cancelled':
                raise Rejected('只有已取消任务可以核查归档命令')
            if self.record.get('resources_cleared') is True:
                return {'confirmed': True, 'no_op': True}
            command_id = (self.record.get('task_cancellation') or {}).get('abandoned_command_id')
            attempts = [a for bucket in (self.record.get('steps') or {}).values()
                        for a in bucket.get('attempts') or [] if a.get('command_id') == command_id]
            if not command_id or len(attempts) != 1 or not attempts[0].get('request'):
                raise Rejected('已取消任务缺少可核对的原命令，不能确认资源释放')
            attempt = deepcopy(attempts[0])
            revision = self.record['revision']
        result = self.port.query(command_id, attempt['request'])
        evidence = result.get('evidence') or {} if isinstance(result, dict) else {}
        if (not isinstance(result, dict) or result.get('command_id') != command_id
                or result.get('timed_out') or result.get('terminal') not in {'succeeded', 'failed', 'cancelled'}
                or result.get('stopped') is not True or result.get('resources_released') is not True
                or not isinstance(evidence, dict) or evidence.get('identity_ok') is not True
                or evidence.get('time_ok') is not True):
            raise Rejected('已取消任务的原命令停止或资源释放仍未确认，拒绝切换环境')
        with self._exclusive():
            self._reload_locked()
            if self.state != 'cancelled' or self.record['revision'] != revision:
                raise Rejected('核查期间任务已变化，未采用迟到回执')
            self.record.setdefault('cancelled_resource_checks', []).append({
                'command_id': command_id, 'attempt_id': attempt['attempt_id'],
                'checked_at': datetime.now().astimezone().isoformat(), 'result': deepcopy(result)})
            self.record.update(stopped_confirmed=True, resources_cleared=True)
            self._save('cancelled_resources_confirmed')
        return {'confirmed': True, 'query_only': True, 'command_id': command_id}

    def resolve_cancel(self, *, command_terminal: bool, stopped: bool, resources_released: bool):
        # Remote facts do not decide whether an abandoned task is cancelled.
        del command_terminal, stopped, resources_released
        return self.settle_cancel()

    def requery(self):
        with self._exclusive():
            self._reload_locked()
            if self.state != "recovery_required":
                raise IllegalTransition("只有结果未知时才核对原命令")
            command_id = self.record.get("open_command_id")
            if not command_id:
                raise Rejected("没有原命令可查询")
            step = self._step_of_command(command_id)
            attempt = self._latest(step.step_id) if step else None
            if step is None or attempt is None:
                raise Rejected("没有原命令可查询")
            contract = self._contract(
                step,
                attempt["attempt_id"],
                attempt["command_id"],
                deepcopy(attempt["request"]),
            )
        progress = self.runner.execute(contract, self.port, requery=True)
        if progress.command_id != command_id:
            raise KernelError("重查不能更换 command_id")
        result = {
            "command_id": progress.command_id,
            "terminal": progress.terminal,
            "timed_out": progress.timed_out,
            "started": progress.started,
            "stopped": progress.publisher_stopped,
            "evidence": progress.evidence,
        }
        with self._exclusive():
            self._reload_locked()
            if self.state != "recovery_required" or self.record.get("open_command_id") != command_id:
                return result
            kind = classify_query(
                result,
                requires_object_evidence=bool(step and step.requires_object_evidence),
            )
            self.record["last_query_command_id"] = command_id
            if kind == "not_started":
                self.record["not_started_confirmed"] = True
                self.record["stopped_confirmed"] = True
                self.record["command_unknown"] = False
                self.record["blocked_reason"] = "原命令确认未开始；点击继续将核对资源与前置条件，尝试重新下发当前环节。"
            elif kind == "stopped":
                self.record["stopped_confirmed"] = True
                self.record["command_unknown"] = False
                self.record["blocked_reason"] = "原命令已停止；点击继续将核对执行结果、资源与前置条件，尝试重新下发未完成环节。"
            elif kind == "ended":
                self.record["pending_progress"] = self._progress_from_query(step, command_id, result).as_dict()
                self.record["command_unknown"] = False
                self.apply("requery_ended")
            else:
                self.record["command_unknown"] = True
                self.record["not_started_confirmed"] = False
                self.record["stopped_confirmed"] = False
            self._save("requery")
        return result

    def resume(self):
        """续跑入口只查询原命令，不发出新的 -r 命令。"""
        with self._exclusive():
            self._reload_locked()
            step = self._current_step()
            # The preceding action has already passed. A failed handoff before
            # the next submit has no open command to requery; retry that gate.
            if (self.state == "recovery_required" and not self.record.get("open_command_id")
                    and self.record.get("blocked_reason") == "handoff_unconfirmed"
                    and step and step.handoff_before
                    and not (self._latest(step.step_id) or {}).get("submitted")):
                self.record["dispatch_closed"] = False
                self.record["control_request"] = ""
                self.record["requery_hold"] = False
                self.record["blocked_reason"] = ""
                self.apply("retry_handoff")
                self._save("handoff_recheck_requested")
                return {"handoff_recheck": True, "step_id": step.step_id}
            revision = self.record.get("revision", 0)
            command_id = self.record.get("open_command_id")
        result = self.requery()
        with self._exclusive():
            self._reload_locked()
            # Explicit resume may withdraw an earlier failed pause/cancel only after
            # the original command is verified ended. A concurrent control wins.
            if (self.state == "verifying" and self.record.get("revision") == revision + 1
                    and self.record.get("open_command_id") == command_id
                    and result.get("stopped") is True
                    and (result.get("evidence") or {}).get("resources_released") is True):
                self.record["dispatch_closed"] = False
                self.record["control_request"] = ""
                self.record["blocked_reason"] = ""
                self._save("resume_original_confirmed")
        return result

    def vla_effect(self, task_id, command_id, action, *, evidence=None, authorization='', auto_advance=False, expected_revision=None):
        """Control only the original VLA command; queries never drive or retry a step."""
        if action not in {'query', 'pause', 'finish'}:
            raise Rejected('未知 VLA 效果核验操作')
        if auto_advance and (action != 'finish' or not callable(getattr(self.port, 'wait_completion', None))):
            raise Rejected('该执行模式不支持确认后自动推进')
        if action == 'finish' and (not isinstance(authorization, str) or not authorization.startswith('Bearer ')
                                   or not authorization[7:].strip()):
            raise Rejected('完成确认需要授权核验方的 Bearer 凭据')
        with self._exclusive():
            self._reload_locked()
            step = self._current_step()
            if expected_revision is not None and self.record.get('revision') != expected_revision:
                raise Rejected('视觉核验期间任务已变化，禁止提交迟到结果')
            if (self.record.get('task_id') != task_id or not command_id
                    or self.record.get('open_command_id') != command_id
                    or not step or step.kind not in NATIVE_OPERATIONS
                    or self.state not in {'waiting_human', 'recovery_required'}
                    or self.record.get('control_request') or self.record.get('dispatch_closed')):
                raise Rejected('当前任务、命令或控制状态不允许效果核验')
            attempt = self._latest(step.step_id)
            if not attempt or attempt['command_id'] != command_id:
                raise Rejected('VLA 命令与尝试不匹配')
            request = deepcopy(attempt['request'])
            contract = self._contract(step, attempt['attempt_id'], command_id, request)
            controls = attempt.setdefault('effect_controls', {})
            sent_action = action
            if action != 'query':
                prior = controls.get(action)
                if prior and prior.get('evidence') != evidence:
                    raise Rejected('原命令已记录不同效果证据，禁止覆盖')
                if prior:
                    sent_action = 'query'  # Lost replies/restarts never replay a mutation.
                else:
                    controls[action] = {'evidence': deepcopy(evidence), 'status': 'requested',
                                        'auto_advance': auto_advance}
                    self._save('vla_effect_intent')
            revision = self.record.get('revision')
        try:
            from shared.diagnostic_context import diagnostic_context
            with diagnostic_context(task_id=contract.task_id, step_id=contract.step_id,
                                    attempt_id=contract.attempt_id, command_id=command_id):
                viewed = self.port.effect_control(sent_action, command_id, request,
                                                 evidence=evidence, authorization=authorization)
            progress = self.runner._progress(contract, viewed)
        except Exception as exc:
            # Keep the durable intent if a POST may have been received. Explicit validation
            # rejections may be corrected by a new human request; never retry automatically.
            from brain.adapters.http_client import HttpContractError
            rejection = exc.payload if isinstance(exc, HttpContractError) and isinstance(exc.payload, dict) else {}
            definite = isinstance(exc, ValueError) or (
                isinstance(exc, HttpContractError) and exc.status_code in {400, 401, 403, 409, 501}
                and (rejection.get('error') or {}).get('code') != 'COMPLETION_CONFLICT')
            with self._exclusive():
                self._reload_locked()
                if self.record.get('revision') == revision and self.record.get('open_command_id') == command_id:
                    if sent_action != 'query' and definite:
                        previous = self._latest(step.step_id)['effect_controls'].pop(action)
                        self._latest(step.step_id).setdefault('effect_rejections', []).append(
                            dict(previous, action=action, status='rejected'))
                    self._save('vla_effect_rejected' if definite else 'vla_effect_uncertain')
            raise Rejected('VLA 核验请求未确认，请查询原命令；' + str(exc)) from exc
        with self._exclusive():
            self._reload_locked()
            if (self.record.get('revision') != revision or self.record.get('open_command_id') != command_id
                    or self.record.get('control_request') or self.state in TERMINAL_STATES):
                raise Rejected('任务状态已变化，未应用迟到的 VLA 回包')
            if progress.evidence.get('identity_ok') is not True:
                raise Rejected('VLA 核验回包身份不符；保留原命令等待核清')
            self._stamp_attempt(step, progress, finished=bool(progress.terminal))
            raw = viewed.get('raw') or {}
            self.record['vla_confirmation'] = {
                'command_id': command_id, 'operation': step.kind, 'state': raw.get('state'),
                'pause_requested': raw.get('pause_requested') is True,
                'verification_ready_at': raw.get('verification_ready_at'),
                'finish_requested': raw.get('finish_requested') is True,
                'holding_evidence': deepcopy((raw.get('result') or {}).get('holding_evidence')),
                'execution_mode': raw.get('execution_mode'), 'object_id': request['body']['object_id'],
                'execution_ready_at': raw.get('execution_ready_at'),
                'completion': deepcopy(raw.get('completion')),
                'door_observation': deepcopy(attempt.get('door_observation')),
                'finish_intent': deepcopy((self._latest(step.step_id).get('effect_controls') or {}).get('finish')),
            }
            self._save('vla_effect_observed')
            return {'accepted': True, 'query_only': sent_action == 'query', 'task_id': task_id,
                    'command_id': command_id, 'state': self.state,
                    'vla_confirmation': deepcopy(self.record['vla_confirmation'])}

    def door_observation_context(self):
        """An immutable observation lease; no model or image reads under task lock."""
        with self._exclusive():
            self._reload_locked()
            options = self.record.get('door_verification') or {}
            step = self._current_step()
            grasp = step is not None and step.kind == 'grasp_cola'
            close = step is not None and step.kind in {'close_fridge','close_fridge_test'}
            confirmation = self.record.get('vla_confirmation') or {}
            if (options.get('enabled') is not True
                    or (options.get('grasp_evidence_contract') != 'fq/vla-grasp-cola-wrist-return-evidence/v2' if grasp
                        else options.get('close_evidence_contract') != 'fq/vla-close-fridge-head-arm-evidence/v1' if close
                        else options.get('evidence_contract') != 'fq/vla-open-fridge-head-arm-evidence/v2')
                    or self.state != 'waiting_human'
                    or self.record.get('execution_backend') not in {'reception_real', 'reception_sim_navigation_real', 'body_real'}
                    or not step or step.kind not in {'open_fridge','grasp_cola','close_fridge','close_fridge_test'}
                    or self.record.get('control_request') or self.record.get('dispatch_closed')
                    or confirmation.get('state') != 'running' or confirmation.get('observation_error')
                    or not confirmation.get('execution_ready_at')):
                return None
            attempt = self._latest(step.step_id)
            if (not attempt or attempt.get('command_id') != self.record.get('open_command_id')
                    or confirmation.get('command_id') != attempt['command_id']
                    or (attempt.get('effect_controls') or {}).get('finish')):
                return None
            return dict(task_id=self.record['task_id'], command_id=attempt['command_id'],
                        attempt_id=attempt['attempt_id'], revision=self.record['revision'],
                        vla_url=(self.record.get('execution_target') or {}).get('vla'),
                        execution_ready_at=confirmation['execution_ready_at'], options=deepcopy(options),
                        operation=step.kind, object_id=step.object_id)

    def apply_door_observation(self, context, result, *, authorization=''):
        """Record evidence on the original attempt, then use the existing finish intent."""
        from brain.adapters.door_verification import SCHEMA, VISUAL_CONTRACT, GRASP_SCHEMA, GRASP_CONTRACT, CLOSE_SCHEMA, CLOSE_CONTRACT
        from brain.adapters.vla_camera import timestamp
        from contracts.vla import validate_evidence
        with self._exclusive():
            self._reload_locked()
            current = self.door_observation_context()
            if not current or current != context:
                return False
            grasp = context.get('operation') == 'grasp_cola'
            close = context.get('operation') in {'close_fridge','close_fridge_test'}
            expected_contract = GRASP_CONTRACT if grasp else CLOSE_CONTRACT if close else VISUAL_CONTRACT
            if not isinstance(result, dict) or result.get('schema') != (GRASP_SCHEMA if grasp else CLOSE_SCHEMA if close else SCHEMA):
                raise Rejected('操控视觉核验结果合同不符')
            step = self._current_step()
            attempt = self._latest(step.step_id)
            field = 'grasp_observation' if grasp else 'door_observation'
            previous = attempt.get(field) or {}
            observation = deepcopy(result)
            calls = result.get('vlm_calls', 0)
            if type(calls) is not int or not 0 <= calls <= 1:
                raise Rejected('视觉核验调用预算无效')
            observation['total_vlm_calls'] = int(previous.get('total_vlm_calls', 0)) + calls
            attempt[field] = observation
            self.record['vla_confirmation'][field] = deepcopy(observation)
            self._save('grasp_visual_observed' if grasp else 'door_visual_observed')
            revision = self.record['revision']
            evidence = result.get('evidence') or {}
            if result.get('status') != 'pass' or context['options']['mode'] != 'auto':
                return False
            if (evidence.get('source') != 'visual_verifier' or (evidence.get('binding') or {}).get('task_id') != context['task_id']
                    or (evidence.get('binding') or {}).get('command_id') != context['command_id'] or evidence.get('visual_contract') != expected_contract):
                raise Rejected('视觉证据不属于原操控命令')
            validate_evidence(step.kind, attempt['request']['body']['object_id'], evidence, context['execution_ready_at'])
            age = (datetime.now().astimezone() - timestamp(evidence['observed_at'])).total_seconds()
            if not 0 <= age <= context['options']['max_evidence_age_sec']:
                return False
        # vla_effect persists one intent before I/O and never retries a lost POST.
        self.vla_effect(context['task_id'], context['command_id'], 'finish', evidence=evidence,
                        authorization=authorization, auto_advance=True, expected_revision=revision)
        return True

    def observe_waiting_vla(self):
        """Read an existing VLA command without confirming, retrying or driving."""
        with self._exclusive():
            self._reload_locked()
            step = self._current_step()
            confirmation = self.record.get('vla_confirmation') or {}
            command_id = self.record.get('open_command_id')
            if (self.state != 'waiting_human' or not command_id or not step
                    or step.kind not in NATIVE_OPERATIONS
                    or self.record.get('control_request') or self.record.get('dispatch_closed')
                    or confirmation.get('command_id') != command_id
                    or (confirmation.get('state') == 'succeeded' and self.record.get('command_unknown') is False)
                    or self.pending_vla_completion()):
                return False
            attempt = self._latest(step.step_id)
            if not attempt or attempt.get('command_id') != command_id:
                return False
            contract = self._contract(step, attempt['attempt_id'], command_id, deepcopy(attempt['request']))
            revision = self.record['revision']
        try:
            from shared.diagnostic_context import diagnostic_context
            with diagnostic_context(task_id=contract.task_id, step_id=contract.step_id,
                                    attempt_id=contract.attempt_id, command_id=command_id):
                viewed = self.port.query(command_id, contract.request)
            if not isinstance(viewed, dict):
                viewed = {'error': 'VLA 原命令回包无效'}
        except Exception as exc:
            if confirmation.get('observation_error') != str(exc):
                from shared.diagnostic_context import report_exception
                report_exception(exc, component='runtime.vla_observation', task_id=contract.task_id,
                                 step_id=contract.step_id, attempt_id=contract.attempt_id, command_id=command_id)
            viewed = {'error': str(exc)}
        progress = self.runner._progress(contract, viewed)
        valid = (viewed.get('command_id') == command_id and not progress.timed_out
                 and progress.evidence.get('identity_ok') is True
                 and (not progress.terminal or progress.evidence.get('time_ok') is True))
        with self._exclusive():
            self._reload_locked()
            if (self.record.get('task_id') != contract.task_id or self.record['revision'] != revision
                    or self.state != 'waiting_human' or self.record.get('open_command_id') != command_id
                    or self.record.get('control_request') or self.record.get('dispatch_closed')
                    or self.pending_vla_completion()):
                self._log_discarded_observation(contract, revision, 'task_revision_or_control_changed')
                return False
            attempt = self._latest(step.step_id)
            if not attempt or attempt.get('attempt_id') != contract.attempt_id:
                self._log_discarded_observation(contract, revision, 'attempt_changed')
                return False
            previous = self.record['vla_confirmation']
            if not valid:
                error = progress.error or 'VLA 原命令回包身份或时间未核清'
                if previous.get('observation_error') == error:
                    return False
                attempt['vla_query_error'] = deepcopy(viewed)
                self.record['vla_confirmation'] = dict(previous, observation_error=error)
                self.record.update(command_unknown=True, stopped_confirmed=False, resources_cleared=False,
                                   blocked_reason='vla_status_unconfirmed')
                self._save('vla_status_unconfirmed')
                return True
            raw = viewed.get('raw') or {}
            latest = deepcopy(previous)
            latest.update(progress.evidence.get('vla_confirmation') or dict(
                state=progress.terminal, completion=deepcopy(raw.get('completion'))))
            latest.pop('observation_error', None)
            if not progress.terminal and latest == previous:
                return False  # Do not append identical running snapshots on every poll.
            self._stamp_attempt(step, progress, finished=bool(progress.terminal))
            self.record['vla_confirmation'] = latest
            self.record['pending_progress'] = progress.as_dict()
            known = progress.terminal in {'succeeded', 'failed', 'cancelled'}
            stopped = known and progress.publisher_stopped is True
            self.record.update(command_unknown=not known, stopped_confirmed=stopped,
                               resources_cleared=stopped and progress.evidence.get('resources_released') is True)
            if progress.terminal in {'failed', 'cancelled'}:
                attempt['verdict'] = self.verifier.judge(contract, progress)
                self.record.update(blocked_reason=((raw.get('error') or {}).get('message')
                                   or (raw.get('result') or {}).get('message') or progress.terminal),
                                   breakpoint_phase=step.step_id)
                self.record.pop('recovery_advice', None)
                self.apply('vla_terminal_failed')
            else:
                self.record['blocked_reason'] = ('vla_success_pending_verification' if known
                                                 else 'vla_effect_confirmation')
            self._save('vla_terminal_observed' if known else 'vla_status_observed')
            return True

    def _log_discarded_observation(self, contract, revision, reason):
        try:
            from shared.brain_journal import emit
            emit('TASK', event='progress_ignored', inherit_task=False, task_id=contract.task_id,
                 step_id=contract.step_id, attempt_id=contract.attempt_id, command_id=contract.command_id,
                 component='runtime', queried_revision=revision, reason=reason,
                 current_task_id=self.record.get('task_id'), current_revision=self.record.get('revision'))
        except Exception:
            pass  # Logging cannot undo the stale-result guard.

    def pending_vla_completion(self):
        """Only a persisted authorized finish intent can authorize automatic progression."""
        step = self._current_step()
        if (self.state != 'waiting_human' or not step or step.kind not in NATIVE_OPERATIONS
                or self.record.get('control_request') or self.record.get('dispatch_closed')):
            return False
        intent = ((self._latest(step.step_id) or {}).get('effect_controls') or {}).get('finish') or {}
        return intent.get('auto_advance') is True

    def await_vla_completion(self):
        """Use existing cooperative I/O and Verifier, never a second task loop."""
        with self._exclusive():
            self._reload_locked()
            if not self.pending_vla_completion():
                return
            step = self._current_step()
            attempt = self._latest(step.step_id)
            command_id = attempt['command_id']
            request = deepcopy(attempt['request'])
            intent = deepcopy(attempt['effect_controls']['finish'])
            contract = self._contract(step, attempt['attempt_id'], command_id, request)
        from shared.diagnostic_context import diagnostic_context
        with diagnostic_context(task_id=contract.task_id, step_id=contract.step_id,
                                attempt_id=contract.attempt_id, command_id=command_id):
            viewed = self.port.wait_completion(command_id, request)
        progress = self.runner._progress(contract, viewed)
        with self._exclusive():
            self._reload_locked()
            if (not self.pending_vla_completion() or self.record.get('open_command_id') != command_id
                    or (self._latest(step.step_id).get('effect_controls') or {}).get('finish') != intent):
                return  # Pause/cancel/new task wins over a late completion.
            finish = self._latest(step.step_id)['effect_controls']['finish']
            finish['auto_advance'] = False
            finish['status'] = 'observed' if progress.terminal else 'unconfirmed'
            completion = (viewed.get('raw') or {}).get('completion') or {}
            if progress.terminal and ((viewed.get('raw') or {}).get('state') == 'succeeded'):
                if (completion.get('status') != 'completed' or completion.get('evidence') != intent['evidence']):
                    progress.evidence.update(supports=False, effect_verified=False,
                                             detail='VLA 完成确认与原核验证据不匹配')
                    progress.effect_ok = False
            if not progress.terminal or progress.evidence.get('identity_ok') is not True:
                self._stamp_attempt(step, progress, finished=bool(progress.terminal))
                self.record.update(command_unknown=True, pending_progress=progress.as_dict(),
                                   blocked_reason=progress.error or 'VLA 收尾结果未确认，请查询原命令')
                self.apply('vla_completion_unconfirmed')
                self._save('vla_completion_unconfirmed')
                return
            self.record['command_unknown'] = False
            self.apply('human_continue')
            self.record['blocked_reason'] = ''
            self._consume_submit(step, progress)  # Original identity/effect/stop verification follows.

    def continue_current(self):
        """Human continue: verify completion or authorize one new attempt of this step."""
        with self._exclusive():
            self._reload_locked()
            state = self.state
            if state not in {'paused', 'recovery_required', 'waiting_human'}:
                raise Rejected('当前状态不能继续')
            command_id = self.record.get('open_command_id')
            step = self._current_step()
            attempt = self._latest(step.step_id) if step else None
            if (state == 'recovery_required' and not command_id and attempt
                    and attempt.get('outcome') == 'rejected'
                    and (attempt.get('progress') or {}).get('evidence', {}).get('submission_rejected')):
                previous = attempt['command_id']
                fresh = self._append_attempt(step, physical=self.policy.physical(step.kind))
                fresh['authorized_by'] = 'human_continue'
                self.record.update(open_command_id=None, pending_progress=None, command_unknown=False,
                                   dispatch_closed=False, control_request='', requery_hold=False,
                                   blocked_reason='', breakpoint_phase='')
                self.apply('continue_retry')
                self._save('continue_attempt_prepared')
                return {'retry_prepared': True, 'step_id': step.step_id,
                        'previous_command_id': previous, 'command_id': fresh['command_id']}
            if command_id and (not attempt or attempt.get('command_id') != command_id):
                raise Rejected('原命令与当前环节不匹配，不能重新下发')
            snapshot = deepcopy(self.record)
            request = deepcopy(attempt['request']) if attempt else None
        if state == 'waiting_human':
            if step and step.kind in NATIVE_OPERATIONS and command_id:
                self.vla_effect(snapshot['task_id'], command_id, 'query')
                with self._exclusive():
                    self._reload_locked()
                    if (self.state != state or self.record.get('open_command_id') != command_id
                            or self.record.get('control_request')):
                        raise Rejected('任务控制已变化，未推进迟到的 VLA 结果')
                    progress = ProgressEvent.from_dict(self._latest(step.step_id)['progress'])
                    if not progress.terminal or progress.evidence.get('identity_ok') is not True:
                        raise Rejected('VLA 尚未返回原命令终态；请先暂停、核验并确认效果，或查询原命令')
                    self.record['command_unknown'] = False
                    self.apply('human_continue')
                    self.record['blocked_reason'] = ''
                    self._consume_submit(step, progress)
                return {'original_command': command_id, 'verification_pending': True}
            self.human_continue()
            return {'resumed': True}
        if not command_id:
            if state == 'paused':
                self.resume_paused()
                return {'resumed': True}
            return self.resume()
        try:
            result = self.port.query(command_id, request)
        except Exception as exc:
            raise Rejected('无法查询原动作停止和执行结果，未重新下发') from exc
        if not isinstance(result, dict):
            raise Rejected('原动作回执无效，未重新下发')
        retry_boundary = None
        check_retry = getattr(self.port, 'retry_ready', None)
        if (step and step.kind in NATIVE_OPERATIONS and callable(check_retry)
                and result.get('terminal') in {'failed', 'cancelled'}):
            try:
                retry_boundary = check_retry(command_id, request, result)
            except Exception as exc:
                raise Rejected('无法核对当前策略发布通路，未重新下发') from exc
            if not isinstance(retry_boundary, dict) or retry_boundary.get('confirmed') is not True:
                reason = retry_boundary.get('reason') if isinstance(retry_boundary, dict) else 'invalid_response'
                raise Rejected('当前策略发布通路尚未核清，未重新下发：' + str(reason))
        with self._exclusive():
            self._reload_locked()
            if (self.record.get('task_id') != snapshot['task_id']
                    or self.record['revision'] != snapshot['revision'] or self.state != snapshot['state']
                    or self.record.get('open_command_id') != command_id):
                raise Rejected('任务状态已变化，未重新下发，请刷新后重试')
            if (result.get('command_id') != command_id or result.get('timed_out')
                    or (retry_boundary is None and (result.get('stopped') is not True
                                                   or result.get('resources_released') is not True))):
                raise Rejected('原动作停止或资源释放尚未确认，未重新下发')
            evidence = result.get('evidence') or {}
            if evidence.get('identity_ok') is False or evidence.get('time_ok') is False:
                raise Rejected('原动作回执身份或时间无效，未重新下发')
            terminal = result.get('terminal')
            if terminal not in {'succeeded', 'failed', 'cancelled'} and result.get('started') is not False:
                raise Rejected('原动作效果仍不明确，未重新下发')
            progress = self._progress_from_query(step, command_id, result)
            if retry_boundary is None:
                progress.evidence = dict(progress.evidence or {}, resources_released=True)
            else:
                progress.evidence = dict(progress.evidence or {}, resources_released=result.get('resources_released'))
                self._latest(step.step_id)['retry_boundary_observation'] = deepcopy(retry_boundary)
            if isinstance(result.get('raw'), dict):
                progress.evidence['downstream'] = deepcopy(result['raw'])
            self._stamp_attempt(step, progress, finished=bool(terminal))
            self.record.update(command_unknown=False, stopped_confirmed=result.get('stopped') is True, resources_cleared=True,
                               dispatch_closed=False, control_request='', requery_hold=False,
                               blocked_reason='', breakpoint_phase='')
            if terminal == 'succeeded':
                # Even a weak success claim is verified, never blindly executed again.
                self.record['pending_progress'] = progress.as_dict()
                self.apply('continue_verify')
                self._save('continue_verifying_original')
                return {'verifying_original': True, 'command_id': command_id}
            attempt = self._latest(step.step_id)
            attempt['verdict'] = 'FAIL' if terminal in {'failed', 'cancelled'} else 'UNKNOWN'
            fresh = self._append_attempt(step, physical=self.policy.physical(step.kind))
            new_command_id = fresh['command_id']
            fresh['authorized_by'] = 'human_continue'
            # An ID reserved for a new attempt is not an active downstream command.
            self.record.update(open_command_id=None, pending_progress=None)
            self.apply('continue_retry')
            self._save('continue_attempt_prepared')
            return {'retry_prepared': True, 'step_id': step.step_id,
                    'previous_command_id': command_id, 'command_id': new_command_id}

    def skip_current(self, *, step_id):
        """Assume this step's conditions by human disposition, preserving real receipts."""
        with self._exclusive():
            self._reload_locked()
            status = status_from_record(self.record)
            if not status['can_skip'] or not step_id or status['skip_step_id'] != step_id:
                raise Rejected("当前步骤已变化或不可跳过，请刷新后重试")
            task_id = self.record['task_id']
            next_step = self._next_body(self._current_step())
            body_only_skip = (getattr(self.port, 'supports_body_only_manual_skip', False) is True
                              and self._current_step().kind in NATIVE_OPERATIONS
                              and next_step is not None and next_step.kind == 'navigate')
            needs_pause = (self.state in {"running", "verifying"}
                           or (self.state == 'waiting_human' and self._current_step().kind in NATIVE_OPERATIONS))
        if needs_pause:
            self.request_pause(expected_step_id=step_id, wait_for_stop=True)
            if self.state != 'paused' and not (body_only_skip and self.state == 'recovery_required'
                                               and self.record.get('control_request') == 'pause'):
                raise Rejected("流程已暂停，但当前动作停止未确认，尚未下发下一步")
        with self._exclusive():
            self._reload_locked()
            status = status_from_record(self.record)
            if (self.record['task_id'] != task_id or not status['can_skip']
                    or not step_id or status['skip_step_id'] != step_id):
                raise Rejected("当前失败步骤已变化或不可跳过，请刷新后重试")
            step = self._current_step()
            revision = self.record['revision']
            original_state = self.state
            command_id = self.record.get('open_command_id')
            skipped_command_id = (self._latest(step_id) or {}).get('command_id')
            refused = self._refused_attempt_observation(self._latest(step_id) or {})
            viewed = None
            if not command_id:
                # A handoff failure can occur after the previous action closed.
                # Recheck its stop evidence before skipping an unsubmitted step.
                for previous in reversed(self.phases[:int(self.record['cursor']) + 1]):
                    attempt = self._latest(previous.step_id) or {}
                    if attempt.get('submitted') and attempt.get('command_id'):
                        # Rejected submissions never created a downstream command.
                        # Still check any earlier accepted action: a refusal says
                        # nothing about another command's stop/resource state.
                        if self._refused_attempt_observation(attempt):
                            continue
                        command_id = attempt['command_id']
                        break
                if not command_id and refused and not self.record.get('command_unknown'):
                    command_id = refused['command_id']
                    viewed = refused
            request = self._request_of(command_id) if command_id else None
            if not command_id and not self.record.get('resources_cleared'):
                raise Rejected("动作资源尚未核清，不能跳过")
        body_handoff = None
        # Query only. A timeout/failed command may still own the action port.
        if command_id and viewed is None:
            try:
                viewed = self.port.query(command_id, request)
            except Exception as exc:
                raise Rejected("无法确认原动作已停止，不能跳过") from exc
            if body_only_skip and request.get('skill') in NATIVE_OPERATIONS:
                body_handoff = self.port.handoff_with_context(dict(task_id=task_id,
                    step_id=next_step.step_id, kind='to_nav', source_command_id=command_id,
                    source_request=request, manual_skips=[step_id]))
                if not isinstance(body_handoff, dict) or body_handoff.get('confirmed') is not True:
                    reason = (body_handoff or {}).get('reason') if isinstance(body_handoff, dict) else 'invalid_response'
                    raise Rejected('身体发布通路尚未核清，不能切换导航：' + str(reason))
            elif (not isinstance(viewed, dict) or viewed.get('command_id') != command_id
                    or viewed.get('stopped') is not True or viewed.get('resources_released') is not True
                    or not (viewed.get('terminal') in {'succeeded', 'failed', 'cancelled'}
                            or viewed.get('started') is False)):
                raise Rejected("原动作停止或资源释放尚未确认；请先暂停并核对，不能跳过")
        if (original_state == 'paused' and skipped_command_id == command_id
                and viewed and viewed.get('terminal') == 'succeeded'):
            raise Rejected('原动作已报告完成，请点击继续核验，无需跳过')
        with self._exclusive():
            self._reload_locked()
            if (self.record.get('task_id') != task_id or self.record['revision'] != revision
                    or self.state != original_state):
                raise Rejected("任务状态已变化，未跳过任何步骤，请刷新后重试")
            effects = {**self.policy.effects(step, final=False), **self.policy.effects(step, final=True)}
            if step.required_holding:
                effects.setdefault('holding', step.required_holding)
            skipped = {'task_id': self.record['task_id'], 'step_id': step_id, 'command_id': skipped_command_id,
                       'stop_command_id': command_id,
                       'reason': self.record.get('blocked_reason'),
                       'source': 'human_control', 'at': datetime.now().astimezone().isoformat(),
                       'conditions_satisfied': True, 'assumed_effects': effects,
                       'stop_observation': deepcopy(viewed)}
            if body_handoff is not None:
                skipped['body_handoff_observation'] = deepcopy(body_handoff)
            self.record.setdefault('manual_skips', []).append(skipped)
            self.record.update(effects)  # Human assumption only; do not create verifier PASS/belief evidence.
            self._mark(step, 'manual_skipped')
            verify_id = self._verify_id(step)
            if verify_id:
                self._mark(self.phases[self._index(verify_id)], 'manual_skipped')
            nxt = self._next_body(step)
            self.record.update(open_command_id=None, command_unknown=False, resources_cleared=True,
                               pending_progress=None, dispatch_closed=False, control_request='',
                               requery_hold=False, blocked_reason='', breakpoint_phase='')
            if nxt:
                for intermediate in self.phases[self._index(step_id) + 1:self._index(nxt.step_id)]:
                    if intermediate.kind != 'verify':
                        self._mark(intermediate, 'skipped')
                self.record['cursor'] = self._index(nxt.step_id)
                self.record['phase'] = nxt.step_id
                self.apply('manual_skip')
            else:
                self.apply('skip_final')
                self._finish_cursor()
                self.record.update(flow_finished=True, blocked_reason='流程结束，含人工跳过，任务未全部成功')
            self._save('step_manually_skipped')
            return {'skipped_step_id': step_id, 'next_step_id': nxt.step_id if nxt else None}

    def _refused_attempt_observation(self, attempt):
        """Use only the bound, explicit pre-execution refusal, never a missing receipt."""
        progress = attempt.get('progress') or {}
        receipt = (progress.get('evidence') or {}).get('submission_rejected') or {}
        identity = {'task_id': self.record['task_id'], 'command_id': attempt.get('command_id')}
        if (attempt.get('outcome') != 'rejected' or attempt.get('accepted') is not False
                or progress.get('started') is not False or not identity['command_id']
                or any(receipt.get(k) != v or progress.get(k) != v for k, v in identity.items())
                or progress.get('attempt_id') != attempt.get('attempt_id')
                or progress.get('step_id') != attempt.get('step_id')):
            return None
        validated = submission_refusal(receipt.get('status_code'), receipt.get('raw'), **identity)
        return refusal_observation(validated) if validated else None

    def continue_same_attempt(self):
        with self._exclusive():
            self._reload_locked()
            if not self.record.get("not_started_confirmed"):
                raise Rejected("原命令尚未确认未开始")
            self.record["requery_hold"] = True
            self.record["dispatch_closed"] = False
            self.record["control_request"] = ""
            self.apply("requery_not_started")
            self._save("same_attempt")

    def open_new_attempt(self):
        with self._exclusive():
            self._reload_locked()
            if self.state != "recovery_required":
                raise IllegalTransition("只有结果未知后才能开始新尝试")
            if not (self.record.get("not_started_confirmed") or self.record.get("stopped_confirmed")):
                raise Rejected("上一条命令尚未核清，不能发出新尝试")
            step = self._current_step()
            if step is None or not step.body:
                raise Rejected("当前相位不能开始新尝试")
            self._append_attempt(step)
            self.record["command_unknown"] = False
            self.record["resources_cleared"] = False
            self.record["requery_hold"] = False
            self.record["dispatch_closed"] = False
            self.record["control_request"] = ""
            self.apply("retry_allowed")
            try:
                self._save("new_attempt")
            except StaleWrite:
                self._reload_locked()
                raise Rejected("并发恢复已被其他写入占用")

    def drive(self, *, limit: int | None = None):
        self._require_port_version()
        if self.record.get("plan_ready") is False:
            self.fail_closed("planning_interrupted")
            return self.state
        limit = limit if limit is not None else max(40, len(self.phases) * 3 + 1)
        for _ in range(limit):
            with self._exclusive():
                self._reload_locked()
                if self._halted():
                    return self._stop_locked("drive_stopped")
                if self.state == "verifying":
                    step = self._current_step()
                    nxt = self._next_body(step) if step else None
                    need_safe = bool(step and self._needs_safe_idle(step))
                    need_handoff = nxt.handoff_before if nxt else ""
                    next_id = nxt.step_id if nxt else ""
                    mode = "verify"
                    contract = None
                else:
                    step = self._skip_non_body()
                    if step is None:
                        return self._stop_locked("drive_idle")
                    self._save("drive_cursor")
                    if self._halted():
                        return self._stop_locked("drive_stopped")
                    need_safe = False
                    need_handoff = ""
                    next_id = ""
                    if self._needs_requery(step):
                        attempt = self._latest(step.step_id)
                        contract = self._contract(
                            step,
                            attempt["attempt_id"],
                            attempt["command_id"],
                            deepcopy(attempt["request"]),
                        )
                        mode = "requery"
                    else:
                        contract = None
                        mode = "body"
            if mode == "verify":
                if not self._verify_outside(step, need_safe, need_handoff, next_id):
                    return self.state
                continue
            if mode == "requery":
                if not self._requery_outside(step, contract):
                    return self.state
                continue
            gate_error = ""
            if step.requires_gate:
                # An unreachable gate is a closed gate: nothing was dispatched, so the
                # operator's continue re-checks it instead of querying a command.
                try:
                    gate_open = self.port.gate_open is True
                except Exception as exc:
                    gate_open, gate_error = False, str(exc) or type(exc).__name__
            if step.requires_gate and not gate_open:
                with self._exclusive():
                    self._reload_locked()
                    if self._halted() or self.state != "running":
                        return self.state
                    self.record["blocked_reason"] = gate_error or getattr(self.port, "gate_reason", "navigation_gate")
                    self.record["breakpoint_phase"] = step.step_id
                    self.apply("gate")
                    self._save("gate")
                return self.state
            if step.handoff_before:
                answer = self._handoff(step.step_id, step.handoff_before)
                if not (answer.get("available") and answer.get("confirmed")):
                    with self._exclusive():
                        self._reload_locked()
                        if self._halted() or self.state != "running":
                            return self.state
                        self.record["blocked_reason"] = "handoff_unconfirmed"
                        self.record["breakpoint_phase"] = step.step_id
                        self.apply("handoff_unconfirmed")
                        self._save("handoff_blocked")
                    return self.state
            with self._exclusive():
                self._reload_locked()
                if self._halted() or self.state != "running":
                    return self.state
                current = self._current_step()
                if current is None or current.step_id != step.step_id:
                    continue
                if self._needs_requery(current):
                    attempt = self._latest(current.step_id)
                    contract = self._contract(
                        current,
                        attempt["attempt_id"],
                        attempt["command_id"],
                        deepcopy(attempt["request"]),
                    )
                    mode = "requery"
                    step = current
                else:
                    try:
                        contract = self._prepare_submit(current)
                    except StaleWrite:
                        self._reload_locked()
                        return self.state
                    except Rejected as exc:
                        self.record["blocked_reason"] = str(exc)
                        self.apply("unknown")
                        self._save("submit_precondition_rejected")
                        return self.state
                    mode = "body"
                    step = current
            if mode == "requery":
                if not self._requery_outside(step, contract):
                    return self.state
                continue
            progress = self.runner.execute(contract, self.port)
            with self._exclusive():
                self._reload_locked()
                if self._halted() or self.state != "running":
                    return self.state
                if not _same_command(self.record.get("open_command_id"), contract.command_id):
                    return self.state
                self._consume_submit(step, progress)
        raise KernelError("内核推进超过步数上限")

    def _stop_locked(self, event: str):
        try:
            self._save(event)
        except StaleWrite:
            self._reload_locked()
        return self.state

    def _verify_outside(self, step, need_safe, need_handoff, next_id) -> bool:
        safe = None
        handoff_ok = True
        if step is not None and need_safe:
            answer = self._handoff(step.step_id, "safe_idle")
            safe = bool(answer.get("available") and answer.get("confirmed"))
        if need_handoff:
            answer = self._handoff(next_id, need_handoff)
            handoff_ok = bool(answer.get("available") and answer.get("confirmed"))
        with self._exclusive():
            self._reload_locked()
            if self._halted() or self.state != "verifying":
                return False
            self._judge(safe_idle=safe, handoff_ok=handoff_ok, next_step_id=next_id)
        return True

    def _needs_safe_idle(self, step):
        return step.requires_safe_idle or getattr(self.port, 'requires_authoritative_handoff', False) is True

    def _handoff(self, step_id, kind):
        contextual = getattr(self.port, "handoff_with_context", None)
        with self._exclusive():
            self._reload_locked()
            snapshot = deepcopy(self.record)
            command_id = None
            boundary = self._index(step_id) + (1 if kind == 'safe_idle' else 0)
            source_index = 0
            for index in range(boundary - 1, -1, -1):
                previous = self.phases[index]
                attempts = (snapshot.get("steps", {}).get(previous.step_id) or {}).get("attempts") or []
                submitted = [a for a in attempts if a.get('submitted') and a.get('command_id')]
                if submitted:
                    command_id = submitted[-1]['command_id']
                    source_index = index
                    break
            context = {"task_id": snapshot["task_id"], "step_id": step_id, "kind": kind,
                       "source_command_id": command_id,
                       "source_request": self._request_of(command_id) if command_id else None}
            # The skip checked resources for its target action. Authoritative adapters
            # recheck the current boundary; others reuse that disposition.
            skipped = {item.get("step_id") for item in snapshot.get("manual_skips") or []}
            bridged = [step.step_id for step in self.phases[source_index:boundary] if step.step_id in skipped]
            context['manual_skips'] = bridged
        if bridged and getattr(self.port, 'requires_authoritative_handoff', False) is not True:
            answer = {"available": True, "confirmed": True, "reason": "manual_skip",
                      "kind": kind, "task_id": context["task_id"],
                      "source_command_id": command_id, "skipped_steps": bridged}
        elif not callable(contextual):
            return self.port.handoff(step_id, kind)
        else:
            answer = contextual(context)
        if not isinstance(answer, dict):
            answer = {"available": False, "confirmed": False, "reason": "invalid_handoff_response"}
        with self._exclusive():
            self._reload_locked()
            if self.record["revision"] != snapshot["revision"]:
                return {"available": False, "confirmed": False, "reason": "stale_handoff_response"}
            self.record["handoff_observation"] = deepcopy(answer)
            self._save("handoff_observed")
        return answer

    def _requery_outside(self, step, contract) -> bool:
        progress = self.runner.execute(contract, self.port, requery=True)
        result = {
            "command_id": progress.command_id,
            "terminal": progress.terminal,
            "timed_out": progress.timed_out,
            "started": progress.started,
            "stopped": progress.publisher_stopped,
            "resources_released": (progress.evidence or {}).get("resources_released"),
            "evidence": progress.evidence,
        }
        with self._exclusive():
            self._reload_locked()
            if progress.command_id != contract.command_id:
                if self.state == "running":
                    self.record["blocked_reason"] = "requery_changed_command"
                    self.apply("unknown")
                    self._save("requery_rejected")
                return False
            if self._halted() or self.state != "running":
                return False
            if self.record.get("open_command_id") != contract.command_id:
                return False
            self._consume_requery(step, result)
        return True

    def consume_foreign_progress(self, progress: ProgressEvent):
        """Ignore a progress event that names a different step."""
        with self._exclusive():
            self._reload_locked()
            current = self._current_step()
            if current is None or progress.step_id != current.step_id:
                self.record["blocked_reason"] = "progress_step_mismatch"
                self._save("progress_ignored")
                return False
            return True

    def apply(self, event: str, **facts):
        state = self.state
        if event == "accept":
            if state in NON_TERMINAL_STATES or self.record.get("command_unknown"):
                raise Rejected("任务未结束或资源未核清，拒绝新任务")
            self._set("running")
            return
        if event == "cancel_accepted":
            if state not in NON_TERMINAL_STATES:
                raise IllegalTransition(f"{state} 不能进入 cancelling")
            self._set("cancelling")
            return
        if event == "pass_continue" and not facts.get("handoff_ok"):
            raise IllegalTransition("交接条件未满足")
        if event == "pass_final" and not facts.get("safe_idle"):
            raise IllegalTransition("安全收尾未满足")
        if event == "human_continue" and self.record.get("command_unknown"):
            raise Rejected("存在未核清命令，不能离开等待人工")
        nxt = _TRANSITIONS.get((state, event))
        if nxt is None:
            raise IllegalTransition(f"非法迁移: {state} + {event}")
        self._set(nxt)

    def _reject_if_blocked(self, action: str):
        if self.state in NON_TERMINAL_STATES or self.record.get("command_unknown"):
            raise Rejected(f"{action}：任务未结束或资源未核清")

    def _set(self, state: str):
        self.record["state"] = state

    def record_scene(self, *, subject: str, value, source: str, valid_until: str):
        """Append a scene observation. It does not replace a confirmed location."""
        with self._exclusive():
            self._reload_locked()
            if not self.record.get("task_id"):
                raise Rejected("没有任务，不能记录现场")
            append_observation(
                self.record,
                subject=subject,
                value=value,
                source=source,
                observed_at=datetime.now().astimezone(),
                kind="observed",
                valid_until=valid_until,
            )
            self._save("scene_observed")

    def belief(self, subject: str, *, now=None) -> dict:
        return read_subject(self.record.get("observations") or [], subject, now=now)

    def recent_context(self, *, now=None) -> list:
        limit, ttl = event_window_settings(self.config)
        reader = getattr(self.store, "recent_events", None)
        if not callable(reader):
            return []
        return reader(limit=limit, ttl_sec=ttl, now=now)

    def _confirm(self, subject: str, value, source: str):
        append_observation(
            self.record,
            subject=subject,
            value=value,
            source=source,
            observed_at=datetime.now().astimezone(),
            kind="established",
        )

    def _save(self, event: str):
        saved = self.store.save_state(self.record)
        self.record = saved
        self.store.append_event(
            event,
            task_id=self.record.get("task_id"),
            state=self.record.get("state"),
            phase=self.record.get("phase"),
            command_id=self.record.get("open_command_id"),
            entry=self.record.get("entry"),
            environment_revision=self.record.get("environment_revision"),
            execution_backend=self.record.get("execution_backend"),
            release_id=self.record.get("release_id"),
            revision=self.record.get("revision"),
        )
        if self.event_sink:
            try:
                self.event_sink(event, deepcopy(self.record))
            except Exception:
                import logging
                logging.getLogger("runtime").exception("事件投影失败；任务账本已持久化")

    def _require_port_version(self):
        if getattr(self.port, "contract_version", None) != CONTRACT_VERSION:
            raise KernelError("端口必须声明合同版本 fq/reception-lan/v1")

    def _current_step(self):
        cursor = int(self.record.get("cursor") or 0)
        if cursor < 0 or cursor >= len(self.phases):
            return None
        return self.phases[cursor]

    def _skip_non_body(self):
        while True:
            step = self._current_step()
            if step is None:
                return None
            if step.kind == "local":
                self._mark(step, "done")
                self.record["cursor"] = int(self.record["cursor"]) + 1
                continue
            if step.optional and not self.policy.enabled(step, self.config):
                self._mark(step, "skipped")
                self.record["cursor"] = int(self.record["cursor"]) + 1
                continue
            if step.kind == "verify":
                self._mark(step, "done")
                self.record["cursor"] = int(self.record["cursor"]) + 1
                continue
            self.record["phase"] = step.step_id
            return step

    def _needs_requery(self, step) -> bool:
        attempt = self._latest(step.step_id)
        return bool(attempt and attempt.get("submitted") and not attempt.get("finished"))

    def _mark(self, step, status: str):
        bucket = self.record["steps"].setdefault(step.step_id, {"attempts": []})
        bucket["status"] = status

    def _latest(self, step_id: str):
        attempts = ((self.record.get("steps") or {}).get(step_id) or {}).get("attempts") or []
        return attempts[-1] if attempts else None

    def _step_of_command(self, command_id: str):
        for step in self.phases:
            attempt = self._latest(step.step_id)
            if attempt and attempt.get("command_id") == command_id:
                return step
        return self._current_step()

    def _request_of(self, command_id: str) -> dict:
        for step in self.phases:
            for attempt in ((self.record.get("steps") or {}).get(step.step_id) or {}).get("attempts") or []:
                if attempt.get("command_id") == command_id:
                    return deepcopy(attempt.get("request") or {})
        return {"task_id": self.record.get("task_id"), "skill": "", "body": {}, "deadline_sec": 30}

    def _last_command(self, step_id: str) -> str:
        attempt = self._latest(step_id)
        return str((attempt or {}).get("command_id") or "")

    def _append_attempt(self, step, *, physical: bool = True):
        bucket = self.record["steps"].setdefault(step.step_id, {"attempts": []})
        attempts = bucket.setdefault("attempts", [])
        number = len(attempts) + 1
        if physical:
            command_id = make_command_id(step.prefix, self.record["task_id"], number)
            request = self._envelope(step, command_id)
        else:
            command_id = ""
            _, ttl_sec = event_window_settings(self.config)
            request = {
                "skill": step.kind,
                "task_id": self.record["task_id"],
                "step_id": step.step_id,
                "deadline_sec": step.deadline_sec,
                "observation_ttl_sec": ttl_sec,
                "body": {
                    "task_id": self.record["task_id"],
                    "task": self.record.get("task_desc") or "",
                    "subtask": step.key,
                },
            }
        attempt_id = f"{step.step_id}-a{number}"
        previous = attempts[-1] if attempts else None
        contract = self._contract(step, attempt_id, command_id, request)
        record = AttemptRecord(
            attempt_id=attempt_id,
            step_id=step.step_id,
            task_id=self.record["task_id"],
            command_id=command_id,
            scene={
                "object_location": self.record.get("object_location"),
                "holding": self.record.get("holding"),
                "phase": step.step_id,
            },
            contract=contract.as_dict(),
            request=request,
            evidence_ref=evidence_filename(attempt_id),
            delta={} if previous is None else {
                "previous_attempt_id": previous.get("attempt_id"),
                "previous_command_id": previous.get("command_id"),
            },
            verdict="",
            outcome="",
            intent="submit" if physical else "observe",
        ).as_dict()
        record["submitted"] = False
        record["finished"] = False
        attempts.append(record)
        if physical:
            self.record["open_command_id"] = command_id
            self.record["not_started_confirmed"] = False
            self.record["stopped_confirmed"] = False
        return record

    def _bound_rules(self, skill: str) -> tuple:
        return tuple(
            rule for rule in (self.record.get("release_rules") or [])
            if isinstance(rule, dict) and rule.get("skill") == skill
        )

    def _envelope(self, step, command_id: str) -> dict:
        # After a skipped leg the operator has moved the robot. The proof still names the
        # real DREAM command; VLA checks it against DREAM before taking the action port.
        request = self.policy.request(step, self.record["task_id"], command_id, self._last_command)
        if step.required_holding:
            for previous in reversed(self.phases[:self._index(step.step_id)]):
                assumed = self._manual_conditions(previous)
                effects = {**self.policy.effects(previous, final=False), **self.policy.effects(previous, final=True)}
                if assumed:
                    effects.update(assumed.get('assumed_effects') or {})
                if 'holding' not in effects:
                    continue
                if assumed and assumed.get('assumed_effects', {}).get('holding') == step.required_holding:
                    request['operator_assumption'] = dict(source='human_skip', task_id=self.record['task_id'],
                        step_id=previous.step_id, command_id=assumed.get('command_id'),
                        holding=step.required_holding, at=assumed['at'])
                break  # The latest holding effect or human assumption wins.
        return request

    def _manual_conditions(self, step):
        bucket = self.record.get('steps', {}).get(step.step_id) or {}
        if bucket.get('status') != 'manual_skipped':
            return None
        command_id = (self._latest(step.step_id) or {}).get('command_id')
        return next((item for item in reversed(self.record.get('manual_skips') or [])
                     if item.get('task_id') == self.record['task_id'] and item.get('step_id') == step.step_id
                     and item.get('command_id') == command_id and item.get('conditions_satisfied') is True), None)

    def _contract(self, step, attempt_id: str, command_id: str, request: dict) -> SkillContract:
        return SkillContract(
            package=self.package_name,
            skill=step.kind,
            version=CONTRACT_VERSION,
            task_id=self.record["task_id"],
            step_id=step.step_id,
            attempt_id=attempt_id,
            goal=step.key or step.target_area or step.step_id,
            object_id=self.policy.object_id(step),
            preconditions=step.handoff_before,
            evidence=step.evidence,
            failure_budget=0,
            deadline_sec=step.deadline_sec,
            command_prefix=step.prefix,
            command_id=command_id,
            postconditions=(step.evidence,) if step.evidence else (),
            handoff=step.handoff_before,
            tools=(step.kind,),
            requires_object_evidence=step.requires_object_evidence,
            requires_safe_idle=self._needs_safe_idle(step),
            request=request,
            bound_rules=self._bound_rules(step.kind),
        )

    def _prepare_submit(self, step):
        if self.record.get("dispatch_closed"):
            raise Rejected("新派发已关闭")
        if step.required_holding and self.record.get('holding') != step.required_holding:
            raise Rejected('后续运送需要已核验或本任务人工确认的持物记录')
        if step.kind == 'grasp_cola' and not any(
                previous.kind == 'open_fridge' and previous.target_area == step.target_area
                and ((self._latest(previous.step_id) or {}).get('verdict') == 'PASS'
                     or self._manual_conditions(previous))
                for previous in self.phases[:self._index(step.step_id)]):
            raise Rejected('抓可乐必须有本任务同一目标的开门通过证据或人工确认')
        if step.kind == 'close_fridge' and not any(
                previous.kind == 'grasp_cola' and previous.target_area == step.target_area
                and ((self._latest(previous.step_id) or {}).get('verdict') == 'PASS'
                     or self._manual_conditions(previous))
                for previous in self.phases[:self._index(step.step_id)]):
            raise Rejected('关冰箱必须有本任务同一作业点的抓取通过证据或人工确认')
        physical = self.policy.physical(step.kind)
        attempt = self._latest(step.step_id)
        if attempt is None or attempt.get("submitted"):
            attempt = self._append_attempt(step, physical=physical)
        attempt["submitted"] = True
        changes = self.record.get('plan_revisions') or []
        if changes and step.step_id in {s['step_id'] for s in changes[-1]['after']}:
            attempt['plan_revision'] = self.record.get('plan_revision', 0)
            attempt.setdefault('authorized_by', 'human_recovery_plan')
        if physical:
            counts = self.record.setdefault("dispatch_counts", {})
            counts[step.step_id] = int(counts.get(step.step_id) or 0) + 1
            self.record["command_unknown"] = True
            self.record["resources_cleared"] = False
            self.record["open_command_id"] = attempt["command_id"]
        else:
            self.record["command_unknown"] = False
            self.record["resources_cleared"] = True
            self.record["open_command_id"] = None
        self.record["phase"] = step.step_id
        self._save("submit_intent" if physical else "observe_intent")
        attempt = self._latest(step.step_id)
        return self._contract(
            step,
            attempt["attempt_id"],
            attempt["command_id"],
            deepcopy(attempt["request"]),
        )

    def _consume_submit(self, step, progress: ProgressEvent):
        if self.state != "running" or self.record.get("dispatch_closed"):
            return
        if progress.step_id != step.step_id or not _same_command(
            progress.command_id, self.record.get("open_command_id"),
        ):
            self.record["blocked_reason"] = "progress_step_mismatch"
            self._save("progress_ignored")
            return
        self._stamp_attempt(step, progress, finished=bool(progress.terminal))
        if (progress.evidence or {}).get("submission_rejected"):
            self._record_submission_rejection(step, progress)
            return
        confirmation = (progress.evidence or {}).get('vla_confirmation')
        if (step.kind in NATIVE_OPERATIONS and confirmation and not progress.timed_out
                and progress.evidence.get('identity_ok') is True):
            self.record.update(vla_confirmation=deepcopy(confirmation), command_unknown=True,
                               resources_cleared=False, blocked_reason='vla_effect_confirmation',
                               breakpoint_phase=step.step_id)
            self.apply('gate')
            self._save('vla_effect_waiting')
            return
        if progress.timed_out or not progress.terminal:
            attempt = self._latest(step.step_id)
            if attempt is not None:
                attempt["verdict"] = "UNKNOWN"
            self.record["breakpoint_phase"] = step.step_id
            if progress.timed_out:
                self.record["blocked_reason"] = "timeout"
            elif progress.error:
                self.record["blocked_reason"] = progress.error
            else:
                self.record["blocked_reason"] = "missing_terminal"
            self.record["command_unknown"] = bool(self.record.get("open_command_id"))
            self.record["pending_progress"] = progress.as_dict()
            self.apply("unknown")
            self._save("unconfirmed")
            return
        self.record["pending_progress"] = progress.as_dict()
        verify_id = self._verify_id(step)
        self.record["phase"] = verify_id or step.step_id
        self.apply("action_finished")
        self._save("action_finished")

    def _record_submission_rejection(self, step, progress):
        receipt = progress.evidence["submission_rejected"]
        attempt = self._latest(step.step_id)
        self._stamp_attempt(step, progress, finished=True)
        attempt.update(verdict="FAIL", outcome="rejected", accepted=False)
        self.record.update(open_command_id=None, command_unknown=False, resources_cleared=True,
                           not_started_confirmed=True, stopped_confirmed=True,
                           pending_progress=progress.as_dict(), breakpoint_phase=step.step_id,
                           blocked_reason="submission_rejected:" + receipt["code"])
        self.record.pop("recovery_advice", None)
        self.apply("submission_rejected")
        self._save("submission_rejected")

    def reconcile_submission_rejection(self):
        """Repair old UNKNOWN records only from their original validation refusal.

        No 404, idle status or new network request is used as proof. Keep the
        original progress for audit. Never resume; honor an already pending cancel.
        """
        import re
        with self._exclusive():
            self._reload_locked()
            command_id = self.record.get("open_command_id")
            if (self.state != "recovery_required" or not command_id
                    or self.record.get("control_request") not in (None, "", "cancel")):
                return False
            step = self._current_step()
            attempt = self._latest(step.step_id) if step else None
            if not attempt or attempt.get("command_id") != command_id or attempt.get("verdict") != "UNKNOWN":
                return False
            old = attempt.get("progress") or {}
            body = (attempt.get("request") or {}).get("body") or {}
            if (old.get("terminal") or old.get("started") is not None
                    or any(old.get(k) != v for k, v in (
                        ("task_id", self.record["task_id"]), ("step_id", step.step_id),
                        ("attempt_id", attempt["attempt_id"]), ("command_id", command_id)))
                    or body.get("command_id") != command_id or body.get("task_id") != self.record["task_id"]):
                return False
            match = re.match(r"^HTTP (400|403|409|422|503) https?://\S+: ", old.get("error") or "")
            raw = (old.get("evidence") or {}).get("downstream")
            receipt = submission_refusal(int(match[1]), raw, command_id=command_id,
                                         task_id=self.record["task_id"]) if match else None
            if not receipt:
                return False
            contract = self._contract(step, attempt["attempt_id"], command_id, deepcopy(attempt["request"]))
            progress = self.runner._progress(contract, refusal_observation(receipt))
            attempt["original_unknown_progress"] = deepcopy(old)
            self._record_submission_rejection(step, progress)
            if self.record.get("control_request") == "cancel":
                self.apply("cancel_accepted")
                self._clear_cancel_locked()
                self._save("cancel_resolved")
            return True

    def _consume_requery(self, step, result):
        if self.state != "running" or self.record.get("dispatch_closed"):
            return
        kind = classify_query(result, requires_object_evidence=step.requires_object_evidence)
        command_id = self.record.get("open_command_id")
        if kind == "unknown":
            self.record["command_unknown"] = True
            self.record["breakpoint_phase"] = step.step_id
            self.apply("unknown")
            self._save("requery_unknown")
            return
        if kind == "not_started":
            self.record["not_started_confirmed"] = True
            self.record["stopped_confirmed"] = True
            self.record["command_unknown"] = False
            self.record["requery_hold"] = True
            self.record["blocked_reason"] = "原命令确认未开始；点击继续将核对资源与前置条件，尝试重新下发当前环节。"
            self.apply("requery_stopped")
            self._save("requery_not_started_seen")
            return
        if kind == "ended":
            self.record["pending_progress"] = self._progress_from_query(step, command_id, result).as_dict()
            self.apply("action_finished")
            self._save("requery_ended_running")
            return
        self.record["stopped_confirmed"] = True
        self.record["command_unknown"] = False
        self.record["requery_hold"] = True
        self.record["blocked_reason"] = "原命令已停止；点击继续将核对执行结果、资源与前置条件，尝试重新下发未完成环节。"
        self.apply("requery_stopped")
        self._save("requery_stopped_seen")

    def _store_passed_observation(self, progress) -> bool:
        """Append a generic observation payload after PASS. Steps without one stay unchanged."""
        payload = (progress.evidence or {}).get("observation")
        if not isinstance(payload, dict):
            return True
        try:
            append_observation(
                self.record,
                subject=str(payload.get("subject") or ""),
                value=payload.get("value"),
                source=str(payload.get("source") or ""),
                observed_at=payload.get("observed_at"),
                kind="observed",
                valid_until=payload.get("valid_until"),
            )
        except ValueError:
            return False
        return True

    def _judge(self, *, safe_idle, handoff_ok: bool, next_step_id: str):
        if self.state != "verifying" or self.record.get("dispatch_closed"):
            return
        step = self._current_step()
        if step is None:
            return
        progress = ProgressEvent.from_dict(self.record.get("pending_progress") or {})
        if self._needs_safe_idle(step):
            progress.safe_idle = bool(safe_idle)
            if safe_idle and getattr(self.port, 'requires_authoritative_handoff', False) is True:
                progress.evidence['resources_released'] = True
        else:
            progress.safe_idle = True
        attempt = self._latest(step.step_id)
        contract = self._contract(
            step,
            attempt["attempt_id"],
            attempt["command_id"],
            deepcopy(attempt["request"]),
        )
        verdict = self.verifier.judge(contract, progress)
        attempt["verdict"] = verdict
        attempt["outcome"] = progress.terminal or ("timeout" if progress.timed_out else "unknown")
        attempt["evidence_ref"] = evidence_filename(attempt["attempt_id"])
        attempt["finished"] = bool(progress.terminal)
        image = (progress.evidence or {}).get("image")
        if isinstance(image, (bytes, bytearray)):
            self.store.save_image(attempt["evidence_ref"], bytes(image))
        if verdict != "PASS":
            self.record["breakpoint_phase"] = step.step_id
            self.record["blocked_reason"] = verdict.lower()
            evidence = progress.evidence or {}
            known_terminal = (progress.terminal in {"succeeded", "failed", "cancelled"}
                              and not progress.timed_out
                              and evidence.get("identity_ok") is True
                              and evidence.get("time_ok") is True)
            self.record["command_unknown"] = not known_terminal
            self.record["stopped_confirmed"] = known_terminal and progress.publisher_stopped is True
            self.record["resources_cleared"] = (self.record["stopped_confirmed"]
                                                and evidence.get("resources_released") is True)
            self.apply("fail" if verdict == "FAIL" else "unknown")
            self._save("verdict")
            return
        if not self._store_passed_observation(progress):
            self.record["breakpoint_phase"] = step.step_id
            self.record["blocked_reason"] = "observation_incomplete"
            self.record["command_unknown"] = bool(self.record.get("open_command_id"))
            self.apply("unknown")
            self._save("observation_incomplete")
            return
        for subject, value in self.policy.effects(step, final=False).items():
            self._confirm(subject, value, attempt["attempt_id"])
            self.record[subject] = value
        verify_id = self._verify_id(step)
        if verify_id:
            self._mark(self.phases[self._index(verify_id)], "done")
            self.record["steps"][verify_id]["verified_attempt_id"] = attempt["attempt_id"]
        if next_step_id:
            for intermediate in self.phases[self._index(step.step_id) + 1:self._index(next_step_id)]:
                if intermediate.kind == 'local':
                    self._mark(intermediate, 'done')
                elif intermediate.optional and not self.policy.enabled(intermediate, self.config):
                    self._mark(intermediate, 'skipped')
        self.record["command_unknown"] = False
        self.record["open_command_id"] = None
        self.record["resources_cleared"] = True
        if next_step_id and not handoff_ok:
            self.record["cursor"] = self._index(next_step_id)
            self.record["breakpoint_phase"] = next_step_id
            self.record["blocked_reason"] = "handoff_unconfirmed"
            self.record["phase"] = next_step_id
            self.apply("handoff_unconfirmed")
            self._save("handoff_blocked")
            return
        if next_step_id:
            self.apply("pass_continue", handoff_ok=True, has_more=True)
            self.record["cursor"] = self._index(next_step_id)
            self.record["phase"] = next_step_id
            self._save("step_passed")
            return
        for subject, value in self.policy.effects(step, final=True).items():
            self._confirm(subject, value, attempt["attempt_id"])
            self.record[subject] = value
        if self._needs_safe_idle(step):
            self.record["safe_idle"] = True
        if self.record.get("manual_skips"):
            self.apply("finish_with_skips")
            self.record.update(flow_finished=True, blocked_reason="流程结束，含人工跳过，任务未全部成功")
        else:
            self.apply("pass_final", safe_idle=True)
        self._finish_cursor()
        self._save("task_finished_with_skips" if self.record.get("manual_skips") else "task_succeeded")

    def _progress_from_query(self, step, command_id: str, result: dict) -> ProgressEvent:
        attempt = self._latest(step.step_id) if step else None
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        return ProgressEvent(
            task_id=self.record.get("task_id") or "",
            step_id=step.step_id if step else "",
            attempt_id=(attempt or {}).get("attempt_id") or "",
            command_id=command_id or "",
            phase=step.step_id if step else "",
            progress="query",
            evidence_ref=evidence_filename((attempt or {}).get("attempt_id") or "attempt"),
            action_ended=bool(result.get("terminal")),
            effect_ok=evidence.get("supports") is True,
            publisher_stopped=result.get("stopped") is True,
            handoff_confirmed=False,
            timed_out=bool(result.get("timed_out")),
            terminal=str(result.get("terminal") or ""),
            evidence=evidence,
            safe_idle=False,
        )

    def _stamp_attempt(self, step, progress: ProgressEvent, *, finished: bool):
        attempt = self._latest(step.step_id)
        if attempt is None:
            return
        attempt["outcome"] = progress.terminal or ("timeout" if progress.timed_out else "pending")
        attempt["finished"] = finished
        attempt["evidence_ref"] = progress.evidence_ref
        attempt["publisher_stopped"] = progress.publisher_stopped
        attempt["action_ended"] = progress.action_ended
        attempt["progress"] = progress.as_dict()

    def _verify_id(self, step):
        index = self._index(step.step_id)
        if index + 1 < len(self.phases) and self.phases[index + 1].kind == "verify":
            return self.phases[index + 1].step_id
        return ""

    def _next_body(self, step):
        if step is None:
            return None
        index = self._index(step.step_id)
        for candidate in self.phases[index + 1:]:
            if candidate.kind in {"verify", "local"}:
                continue
            if candidate.optional and not self.policy.enabled(candidate, self.config):
                continue
            return candidate
        return None

    def _index(self, step_id: str) -> int:
        for index, step in enumerate(self.phases):
            if step.step_id == step_id:
                return index
        return int(self.record.get("cursor") or 0)

    def _finish_cursor(self):
        self.record["cursor"] = len(self.phases)
        self.record["phase"] = self.phases[-1].step_id if self.phases else ""
