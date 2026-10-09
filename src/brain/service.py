"""Application bridge to the shared Runtime. External entry points keep their contracts."""
from __future__ import annotations

import threading
import uuid
from copy import deepcopy

from contracts.tasks import Rejected
from brain.adapters.ports import select_backend, target_identity
from brain.storage.tasks import load_existing
from brain.service_support import runtime_dir, control_task


_ADMISSION = threading.RLock()


class BrainService:
    def __init__(self, config, planning, port_factory, *, reflection=None, runtime_factory=None, reasoner=None):
        self.config = config
        self.planning = planning
        self.port_factory = port_factory
        self.reflection = reflection
        self.reasoner = reasoner
        self.runtime = None
        self.plan_call = lambda runtime, options: self.planning.plan(runtime, options)
        self.select_call = lambda task, options: self.reasoner.select(task, options)
        self.advice_call = lambda context: self.reasoner.advise(context)
        self._drive_lock = threading.Lock()
        if runtime_factory is None:
            from brain.app import create_runtime
            runtime_factory = create_runtime
        self.runtime_factory = runtime_factory

    def _port(self, backend):
        return self.port_factory(backend)

    def publish(self, task, task_id, *, resume=False, options=None):
        from contracts.task_control import control_action
        if control_action(task):
            raise Rejected("任务控制指令应使用控制入口，不创建新任务")
        from shared.execution_profile import admission
        with admission(), _ADMISSION:
            if resume:
                runtime = self.attach()
                if task_id and task_id != runtime.record.get("task_id"):
                    raise Rejected("续跑 task_id 与当前任务不符")
                if runtime.state in {"recovery_required", "paused", "waiting_human"}:
                    runtime.continue_current()
                elif runtime.state not in {"running", "verifying"}:
                    raise Rejected("当前任务不能续跑")
                self._launch(runtime)
                return self._response(runtime)
            task_id = task_id or uuid.uuid4().hex
            existing = load_existing(runtime_dir(self.config)) or {}
            if existing.get("task_id") == task_id:
                raise Rejected("task_id 已存在；续跑须使用原任务的恢复入口")
            options = options or {}
            # Own a cancellable task before any model or remote planning call.
            from brain.adapters.ports import UnselectedPort
            runtime = self.runtime_factory(self.config, UnselectedPort(), package="generic")
            desc = task if isinstance(task, str) else (task[0] if task else "")
            from shared.execution_profile import applied_profile
            runtime.open_task(task_id, task_desc=desc, execution_backend="unselected", planning=True,
                              execution_target={"backend": "unselected"},
                              reflection_enabled=options.get("reflect", True) is not False,
                              origin={"entry": options.get("entry", "api"),
                                      "environment_revision": (applied_profile() or {}).get("revision")})
            self._adopt(runtime)
        runtime.plan(lambda: self._plan_task(runtime, options))
        self._launch(runtime)
        return self._response(runtime)

    def _plan_task(self, runtime, options):
        from contracts.planning import Plan
        from brain.reasoning import selection
        from brain.packages.registry import match_name
        chosen = (self.select_call(runtime.record["task_desc"], options) if self.reasoner else
                  selection(match_name(runtime.record["task_desc"]) or "generic", "rules", "业务包选择"))
        if runtime.public_status()["state"] not in {"running", "paused"}:
            return Plan(())
        package = chosen["package"]
        backend = select_backend(self.config, package, mock=options.get("mock") is True)
        if backend == "reception_mock":
            chosen = dict(chosen, sop={"id": "reception.demo", "version": "legacy",
                                      "description": "既有会议补货演示，不经过导航／操控 HTTP。"})
        port = self._port(backend)
        if not runtime.bind_execution(chosen, backend, target_identity(self.config, backend), port):
            return Plan(())
        return self.plan_call(runtime, options)

    def _adopt(self, runtime):
        self.runtime = runtime

    def _execution_backend(self, existing):
        backend = existing.get("execution_backend")
        if backend:
            return backend
        # Compatibility with the already-reviewed single reception / generic slice.
        return {"generic": "desk", "look": "camera", "reception": "reception_real"}.get(existing.get("package"))

    def _target_changed(self, existing) -> bool:
        target = existing.get("execution_target")
        backend = self._execution_backend(existing)
        return bool(target) and target != target_identity(self.config, backend)

    def attach(self, *, ignore_target_change=False):
        existing = load_existing(runtime_dir(self.config)) or {}
        if not existing.get("task_id"):
            raise Rejected("没有内核任务；旧账本不能作为内核断点续跑")
        runtime = self.runtime
        if runtime and runtime.record.get("task_id") == existing["task_id"]:
            runtime.public_status()
            return runtime
        backend = self._execution_backend(existing)
        changed = self._target_changed(existing)
        if changed and not ignore_target_change:
            raise Rejected("执行地址或后端已变化，先恢复原配置再核对原命令")
        from brain.adapters.ports import UnselectedPort
        # A stale address must not receive a cancel for a command that was never sent.
        if changed or backend == "unselected":
            port = UnselectedPort()
        else:
            port = self._port(backend or "")
        runtime = self.runtime_factory(self.config, port, package=existing.get("package") or "reception")
        self._adopt(runtime)
        return runtime

    def control(self, action, *, task_id=None, step_id=None, expected_command_id=None):
        if action not in {'pause', 'continue', 'cancel', 'skip'}:
            raise Rejected(f'未知控制动作: {action}')
        from shared.execution_profile import admission
        with admission(check_block=(action in {'continue', 'skip'})), _ADMISSION:
            existing = load_existing(runtime_dir(self.config)) or {}
            current_id = existing.get('task_id')
            if action == 'skip' and (not task_id or not isinstance(step_id, str) or not step_id):
                raise Rejected('跳过必须指定 task_id 和 step_id')
            if task_id is not None and task_id != current_id:
                raise Rejected("控制目标任务已变化，未操作新任务")
            from contracts.tasks import TERMINAL_STATES
            if not current_id or existing.get('state') in TERMINAL_STATES:
                return {'accepted': True, 'completed': True, 'no_op': True,
                        'task_id': current_id, 'state': existing.get('state'),
                        'message': '当前没有进行中的任务，无需操作'}
            if action == 'cancel':
                from brain.adapters.ports import UnselectedPort
                runtime = self.runtime_factory(self.config, UnselectedPort(),
                                               package=existing.get('package') or 'reception')
                self._adopt(runtime)
            else:
                runtime = self.attach()
            if action == 'continue' and (step_id is not None or expected_command_id is not None):
                current = runtime.public_status()
                if ((step_id is not None and step_id != (current.get('control_step_id') or ''))
                        or (expected_command_id is not None
                            and expected_command_id != (current.get('control_command_id') or ''))):
                    raise Rejected('当前环节或命令已变化，未重复执行，请刷新后重试')
            if (action == 'pause' and runtime.state == 'paused') or (
                    action == 'continue' and runtime.state in {'running', 'verifying'}):
                return {'accepted': True, 'no_op': True, 'task_id': current_id,
                        'state': runtime.state, 'message': '任务已经暂停' if action == 'pause' else '任务已经在执行或核验中'}
            payload, runtime = control_task(self.config, action, runtime=runtime, step_id=step_id)
            payload['task_id'] = current_id
            if payload.get("accepted") and action in {"continue", "skip"}:
                self._launch(runtime)
            return payload

    def _launch(self, runtime):
        # Standalone library calls are synchronous; the application installs its owner queue.
        self._drive(runtime)

    def _drive(self, runtime):
        while not runtime.planning_done.wait(.1):
            try:
                state = runtime.public_status()
            except Rejected:
                return
            if state.get("terminal") or state.get("state") == "paused":
                return
        with self._drive_lock:
            try:
                if runtime.public_status().get("terminal"):
                    return
            except Rejected:
                return
            try:
                runtime.drive()
            except Exception as exc:
                try:
                    runtime.fail_closed(str(exc))
                except Rejected:
                    return
            self._advise(runtime)
            if runtime.state in {"succeeded", "recovery_required", "cancelled", "failed"}:
                if runtime.record.get("reflection_enabled", True):
                    self._reflect(runtime)

    def _advise(self, runtime):
        from brain.reasoning import RECOVERY_STATES, recovery_context, fallback_advice
        if self.reasoner is None:
            return
        snapshot = runtime.snapshot()
        if snapshot.get("state") not in RECOVERY_STATES:
            return
        # Do not repeat a model call merely because drive is called again.
        if (snapshot.get("recovery_advice") or {}).get("record_revision") == snapshot["revision"]:
            return
        context = recovery_context(snapshot)
        try:
            advice = self.advice_call(context)
        except Exception as exc:
            advice = dict(fallback_advice(context), source="rules", error=str(exc)[:1000])
        runtime.record_advice(context, advice)

    def _reflect(self, runtime):
        if self.reflection is not None:
            self.reflection(deepcopy(runtime.record))

    def _response(self, runtime):
        return {"reasoning_explanation": runtime.record.get("reasoning_explanation", ""),
                "subtask_list": runtime.public_status()["subtask_list"], "source": "kernel",
                "task_id": runtime.record["task_id"]}

    def status(self):
        from brain.service_support import status_view
        return status_view(self.config, None)
