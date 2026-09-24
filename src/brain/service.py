"""Application bridge to the shared Runtime. External entry points keep their contracts."""
from __future__ import annotations

import threading
import logging
import uuid
from copy import deepcopy

from contracts.tasks import Rejected
from brain.packages.registry import match_name
from brain.adapters.ports import build_port, select_backend, target_identity
from brain.storage.tasks import load_existing
from brain.service_support import runtime_dir, control_task


_ADMISSION = threading.RLock()


class BrainService:
    def __init__(self, config, planning, port_factory, *, reflection=None, runtime_factory=None):
        self.config = config
        self.planning = planning
        self.port_factory = port_factory
        self.reflection = reflection
        self.runtime = None
        self.plan_call = lambda runtime, options: self.planning.plan(runtime, options)
        self._drive_lock = threading.Lock()
        if runtime_factory is None:
            from brain.app import create_runtime
            runtime_factory = create_runtime
        self.runtime_factory = runtime_factory

    def _port(self, backend):
        return self.port_factory(backend)

    def publish(self, task, task_id, *, resume=False, options=None):
        from shared.execution_profile import admission
        with admission(), _ADMISSION:
            if resume:
                runtime = self.attach()
                if task_id and task_id != runtime.record.get("task_id"):
                    raise Rejected("续跑 task_id 与当前任务不符")
                if runtime.state == "recovery_required":
                    runtime.resume()
                elif runtime.state == "paused":
                    runtime.resume_paused()
                elif runtime.state == "waiting_human":
                    runtime.human_continue()
                elif runtime.state not in {"running", "verifying"}:
                    raise Rejected("当前任务不能续跑")
                self._launch(runtime)
                return self._response(runtime)
            task_id = task_id or uuid.uuid4().hex
            existing = load_existing(runtime_dir(self.config)) or {}
            if existing.get("task_id") == task_id:
                raise Rejected("task_id 已存在；续跑须使用原任务的恢复入口")
            package = match_name(task) or "generic"
            options = options or {}
            backend = select_backend(self.config, package, mock=options.get("mock") is True)
            port = self._port(backend)
            runtime = self.runtime_factory(self.config, port, package=package)
            desc = task if isinstance(task, str) else (task[0] if task else "")
            from shared.execution_profile import applied_profile
            runtime.open_task(task_id, task_desc=desc, execution_backend=backend, planning=True,
                              execution_target=target_identity(self.config, backend),
                              reflection_enabled=options.get("reflect", True) is not False,
                              origin={"entry": options.get("entry", "api"),
                                      "environment_revision": (applied_profile() or {}).get("revision")})
            self._adopt(runtime)
        runtime.plan(lambda: self.plan_call(runtime, options))
        self._launch(runtime)
        return self._response(runtime)

    def _adopt(self, runtime):
        self.runtime = runtime

    def attach(self):
        existing = load_existing(runtime_dir(self.config)) or {}
        if not existing.get("task_id"):
            raise Rejected("没有内核任务；旧账本不能作为内核断点续跑")
        runtime = self.runtime
        if runtime and runtime.record.get("task_id") == existing["task_id"]:
            runtime.public_status()
            return runtime
        backend = existing.get("execution_backend")
        if not backend:
            # Compatibility with the already-reviewed single reception / generic slice.
            backend = {"generic": "desk", "look": "camera", "reception": "reception_real"}.get(existing.get("package"))
        target = existing.get("execution_target")
        if target and target != target_identity(self.config, backend):
            raise Rejected("执行地址或后端已变化，先恢复原配置再核对原命令")
        runtime = self.runtime_factory(self.config, self._port(backend or ""), package=existing.get("package") or "reception")
        self._adopt(runtime)
        return runtime

    def control(self, action):
        from shared.execution_profile import admission
        with admission(check_block=(action == 'continue')), _ADMISSION:
            runtime = self.attach()
            payload, runtime = control_task(self.config, action, runtime=runtime)
            if payload.get("accepted") and action == "continue":
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
                logging.getLogger("brain").exception("执行异常")
            if runtime.state in {"succeeded", "recovery_required", "cancelled", "failed"}:
                if runtime.record.get("reflection_enabled", True):
                    self._reflect(runtime)

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
