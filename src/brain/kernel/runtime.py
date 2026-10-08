"""The only writer of task state for the phase-1 kernel."""

from __future__ import annotations

import threading
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
from brain.kernel.runner import Runner
from brain.kernel.verifier import Verifier


_TRANSITIONS = {
    ("running", "action_finished"): "verifying",
    ("recovery_required", "manual_skip"): "running",
    ("paused", "manual_skip"): "running",
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


def status_from_record(record) -> dict:
    state = (record or {}).get("state")
    terminal = state in TERMINAL_STATES
    advice = deepcopy((record or {}).get("recovery_advice"))
    if advice:
        advice["applicable"] = advice.get("record_revision") == (record or {}).get("revision")
    tasks = []
    for order, spec in enumerate((record or {}).get("phase_specs") or [], 1):
        bucket = (record or {}).get("steps", {}).get(spec["step_id"]) or {}
        attempts = bucket.get("attempts") or []
        latest = attempts[-1] if attempts else {}
        verdict = latest.get("verdict")
        evidence = (latest.get("progress") or {}).get("evidence") or {}
        marker = bucket.get("status")
        manual_skip = marker == "manual_skipped"
        done = verdict == "PASS" or marker in {"done", "skipped", "manual_skipped"}
        tasks.append({"order": order, "robot_name": spec.get("robot_name") or "FQrobot",
                      "subtask": spec.get("key") or spec["step_id"], "done": done,
                      "step_id": spec["step_id"], "skipped": manual_skip,
                      "status": "skipped" if manual_skip else "success" if done else "failure" if verdict == "FAIL" else "unknown" if verdict == "UNKNOWN" else "pending",
                      "result": "人工跳过；未确认该步骤完成" if manual_skip else evidence.get("text") or evidence.get("detail") or latest.get("outcome") or "",
                      "reported_success": evidence.get("reported_success"),
                      "evidence_level": evidence.get("reported_evidence_level") or evidence.get("grade"),
                      "effect_verified": not manual_skip and (verdict == "PASS" or (spec.get("kind") == "verify"
                          and marker == "done" and bool(bucket.get("verified_attempt_id"))))})
    cursor = int((record or {}).get("cursor") or 0)
    specs = (record or {}).get("phase_specs") or []
    current = specs[cursor] if 0 <= cursor < len(specs) else {}
    attempts = ((record or {}).get("steps", {}).get(current.get("step_id")) or {}).get("attempts") or []
    can_skip = ((state == "recovery_required" or (state == "paused" and bool(attempts)
                 and attempts[-1].get("submitted") is True)) and current.get("kind") not in {None, "local", "verify"}
                and (record or {}).get("control_request") != "cancel"
                and (not attempts or attempts[-1].get("verdict") != "PASS"))
    return {
        "can_skip": can_skip,
        "skip_step_id": current.get("step_id") if can_skip else None,
        "control_step_id": current.get("step_id"),
        "control_command_id": (attempts[-1].get("command_id") if attempts else None),
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
        "can_resume": state in {"paused", "waiting_human", "recovery_required"},
        "execution_active": state == "running",
        "source": "kernel",
        "subtask_list": tasks,
        "completed": sum(item["done"] for item in tasks),
        "total": len(tasks),
        "task_type": (record or {}).get("package"),
        "execution_backend": (record or {}).get("execution_backend"),
        "answer": (((record or {}).get("pending_progress") or {}).get("evidence") or {}).get("text", "") if state == "succeeded" else "",
        "blocked_reason": (record or {}).get("blocked_reason") or "",
        "phase": (record or {}).get("phase") or "",
        "selection": deepcopy((record or {}).get("selection")),
        "recovery_advice": advice,
        "handoff_observation": deepcopy((record or {}).get("handoff_observation")),
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
            self.record = loaded

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
            self.record["recovery_advice"] = {
                **deepcopy(advice), "task_id": self.record["task_id"],
                "command_id": self.record.get("open_command_id"),
                "based_on_revision": self.record["revision"],
                "record_revision": self.record["revision"] + 1,
                "allowed_actions": list(context["allowed_actions"]),
                "automatic": False,
            }
            self._save("recovery_advised")
            return True

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

    def request_pause(self, *, stop_acknowledged: bool | None = None):
        with self._exclusive():
            self._reload_locked()
            if self.state not in {"running", "verifying"}:
                raise IllegalTransition("只有运行或核验中可以暂停")
            if self.record.get("control_request") == "cancel":
                raise IllegalTransition("取消已开始，不能再暂停")
            self.record["dispatch_closed"] = True
            self.record["control_request"] = "pause"
            resume_state = self.state
            self.record["pause_resume_state"] = resume_state
            command_id = self.record.get("open_command_id")
            request = self._request_of(command_id) if command_id else None
            self._save("pause_intent")
        if command_id:
            try:
                self.port.cancel(command_id, request)
            except Exception:
                stop_acknowledged = False
            if stop_acknowledged is None:
                try:
                    viewed = self.port.query(command_id, request) or {}
                except Exception:
                    viewed = {}
                stop_acknowledged = isinstance(viewed, dict) and viewed.get("stopped") is True
        elif stop_acknowledged is None:
            stop_acknowledged = True
        if stop_acknowledged is None:
            stop_acknowledged = False
        with self._exclusive():
            self._reload_locked()
            if self.record.get("control_request") != "pause" or self.state != resume_state:
                return self.state
            self.record["blocked_reason"] = "" if stop_acknowledged else "pause_without_stop_ack"
            self.apply("pause_ack" if stop_acknowledged else "pause_nack")
            if not stop_acknowledged:
                self.record["command_unknown"] = True
                self.record["resources_cleared"] = False
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

    def request_cancel(self):
        with self._exclusive():
            self._reload_locked()
            if self.state not in NON_TERMINAL_STATES:
                raise IllegalTransition("终态不能再取消")
            self.record["dispatch_closed"] = True
            self.record["control_request"] = "cancel"
            command_id = self.record.get("open_command_id")
            request = (
                self._request_of(command_id)
                if command_id else {"task_id": self.record.get("task_id")}
            )
            self._save("cancel_intent")
        result = {"accepted": True, "completed": False, "command_id": command_id}
        if command_id:
            try:
                result = self.port.cancel(command_id, request) or result
            except Exception as exc:
                result = {
                    "accepted": False,
                    "completed": False,
                    "command_id": command_id,
                    "error": str(exc),
                }
        with self._exclusive():
            self._reload_locked()
            if self.record.get("control_request") != "cancel" or self.state in TERMINAL_STATES:
                return result
            if not result.get("accepted"):
                self.record["blocked_reason"] = "cancel_not_accepted"
                self.record["command_unknown"] = True
                self.record["resources_cleared"] = False
                if self.state != "cancelling":
                    self.apply("cancel_accepted")
                self.apply("cancel_unclear")
                self._save("cancel_unclear")
                return result
            if self.state != "cancelling":
                self.apply("cancel_accepted")
            self.record["blocked_reason"] = "cancel_accepted"
            self._save("cancel_accepted")
        return result

    def settle_cancel(self):
        """After acceptance, read stop and resource evidence. Do not treat acceptance as cancelled."""
        with self._exclusive():
            self._reload_locked()
            if self.state != "cancelling":
                return self.state
            command_id = self.record.get("open_command_id")
            request = self._request_of(command_id) if command_id else None
        if not command_id:
            self.resolve_cancel(
                command_terminal=True, stopped=True, resources_released=True,
            )
            return self.state
        try:
            viewed = self.port.query(command_id, request) or {}
        except Exception:
            viewed = {}
        if not isinstance(viewed, dict):
            viewed = {}
        self.resolve_cancel(
            command_terminal=viewed.get("terminal") in {"succeeded", "failed", "cancelled"},
            stopped=viewed.get("stopped") is True,
            resources_released=viewed.get("resources_released") is True,
        )
        return self.state

    def resolve_cancel(self, *, command_terminal: bool, stopped: bool, resources_released: bool):
        with self._exclusive():
            self._reload_locked()
            if command_terminal and stopped and resources_released:
                self.record["open_command_id"] = None
                self.record["command_unknown"] = False
                self.record["resources_cleared"] = True
                self.record["dispatch_closed"] = False
                self.record["control_request"] = ""
                self.record["blocked_reason"] = ""
                self.apply("cancel_cleared")
            else:
                self.record["command_unknown"] = True
                self.record["resources_cleared"] = False
                self.record["blocked_reason"] = "cancel_facts_unclear"
                self.apply("cancel_unclear")
            self._save("cancel_resolved")

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
            if command_id and (not attempt or attempt.get('command_id') != command_id):
                raise Rejected('原命令与当前环节不匹配，不能重新下发')
            snapshot = deepcopy(self.record)
            request = deepcopy(attempt['request']) if attempt else None
        if state == 'waiting_human':
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
        with self._exclusive():
            self._reload_locked()
            if (self.record['revision'] != snapshot['revision'] or self.state != snapshot['state']
                    or self.record.get('open_command_id') != command_id):
                raise Rejected('任务状态已变化，未重新下发，请刷新后重试')
            if (result.get('command_id') != command_id or result.get('timed_out')
                    or result.get('stopped') is not True or result.get('resources_released') is not True):
                raise Rejected('原动作停止或资源释放尚未确认，未重新下发')
            evidence = result.get('evidence') or {}
            if evidence.get('identity_ok') is False or evidence.get('time_ok') is False:
                raise Rejected('原动作回执身份或时间无效，未重新下发')
            terminal = result.get('terminal')
            if terminal not in {'succeeded', 'failed', 'cancelled'} and result.get('started') is not False:
                raise Rejected('原动作效果仍不明确，未重新下发')
            progress = self._progress_from_query(step, command_id, result)
            progress.evidence = dict(progress.evidence or {}, resources_released=True)
            if isinstance(result.get('raw'), dict):
                progress.evidence['downstream'] = deepcopy(result['raw'])
            self._stamp_attempt(step, progress, finished=bool(terminal))
            self.record.update(command_unknown=False, stopped_confirmed=True, resources_cleared=True,
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
        """Explicit human disposition; never synthesize effects or success receipts."""
        with self._exclusive():
            self._reload_locked()
            status = status_from_record(self.record)
            if not status['can_skip'] or not step_id or status['skip_step_id'] != step_id:
                raise Rejected("当前失败步骤已变化或不可跳过，请刷新后重试")
            step = self._current_step()
            revision = self.record['revision']
            original_state = self.state
            command_id = self.record.get('open_command_id')
            skipped_command_id = (self._latest(step_id) or {}).get('command_id')
            if not command_id:
                # A handoff failure can occur after the previous action closed.
                # Recheck its stop evidence before skipping an unsubmitted step.
                for previous in reversed(self.phases[:int(self.record['cursor']) + 1]):
                    attempt = self._latest(previous.step_id) or {}
                    if attempt.get('submitted') and attempt.get('command_id'):
                        command_id = attempt['command_id']
                        break
            request = self._request_of(command_id) if command_id else None
            if not command_id and not self.record.get('resources_cleared'):
                raise Rejected("动作资源尚未核清，不能跳过")
        # Query only. A timeout/failed command may still own the action port.
        viewed = None
        if command_id:
            try:
                viewed = self.port.query(command_id, request)
            except Exception as exc:
                raise Rejected("无法确认原动作已停止，不能跳过") from exc
            if (not isinstance(viewed, dict) or viewed.get('command_id') != command_id
                    or viewed.get('stopped') is not True or viewed.get('resources_released') is not True
                    or not (viewed.get('terminal') in {'succeeded', 'failed', 'cancelled'}
                            or viewed.get('started') is False)):
                raise Rejected("原动作停止或资源释放尚未确认；请先暂停并核对，不能跳过")
        if original_state == 'paused' and viewed and viewed.get('terminal') == 'succeeded':
            raise Rejected('原动作已报告完成，请点击继续核验，无需跳过')
        with self._exclusive():
            self._reload_locked()
            if self.record['revision'] != revision or self.state != original_state:
                raise Rejected("任务状态已变化，未跳过任何步骤，请刷新后重试")
            skipped = {'step_id': step_id, 'command_id': skipped_command_id,
                       'stop_command_id': command_id,
                       'reason': self.record.get('blocked_reason'),
                       'source': 'human_control', 'at': datetime.now().astimezone().isoformat(),
                       'stop_observation': deepcopy(viewed)}
            self.record.setdefault('manual_skips', []).append(skipped)
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
                    need_safe = bool(step and step.requires_safe_idle)
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
            # The operator's skip already confirmed that step's command stopped and
            # released its resources. A handoff across it is filled in, not re-asked.
            skipped = {item.get("step_id") for item in snapshot.get("manual_skips") or []}
            bridged = [step.step_id for step in self.phases[source_index:boundary] if step.step_id in skipped]
        if bridged:
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
        return self.policy.request(step, self.record["task_id"], command_id, self._last_command)

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
            requires_safe_idle=step.requires_safe_idle,
            request=request,
            bound_rules=self._bound_rules(step.kind),
        )

    def _prepare_submit(self, step):
        if self.record.get("dispatch_closed"):
            raise Rejected("新派发已关闭")
        physical = self.policy.physical(step.kind)
        attempt = self._latest(step.step_id)
        if attempt is None or attempt.get("submitted"):
            attempt = self._append_attempt(step, physical=physical)
        attempt["submitted"] = True
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
        if step.requires_safe_idle:
            progress.safe_idle = bool(safe_idle)
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
            self.record["command_unknown"] = True
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
        if step.requires_safe_idle:
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
