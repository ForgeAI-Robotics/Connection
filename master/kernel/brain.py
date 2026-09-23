"""Application bridge to the shared Runtime. External entry points keep their contracts."""
from __future__ import annotations

import threading
import uuid
from copy import deepcopy

from kernel.contracts import Rejected
from kernel.packages.registry import match_name
from kernel.ports import build_port, select_backend, target_identity
from kernel.runtime import TaskRuntime
from kernel.store import KernelStore, load_existing
from kernel.switch import runtime_dir, control_task


_ADMISSION = threading.RLock()


class Brain:
    def __init__(self, agent):
        self.agent = agent
        self._drive_lock = threading.Lock()

    @property
    def config(self):
        return self.agent.config

    def _port(self, backend):
        # Test injection is explicit and never configured by an external request.
        factory = getattr(self.agent, "_kernel_port_factory", None)
        if factory:
            return factory(backend)
        return build_port(self.config, backend, agent=self.agent)

    def _old_work(self):
        if getattr(self.agent, "_reception_running", False) or getattr(self.agent, "_dispatch_running", False):
            return "旧执行线程尚未停止"
        from sop.reception_store import ReceptionStore
        snapshot = ReceptionStore(self.agent._reception_runtime_dir()).load_state() or {}
        state = snapshot.get("state")
        if state and state not in {"SUCCEEDED", "COMPLETED_HAND_STATE_ONLY", "FAILED", "CANCELLED"}:
            return f"旧接待账本存在未核清任务（{state}）；先核清原命令及旧进程，不能自动迁移或强制覆盖"
        return None

    def publish(self, task, task_id, *, resume=False, options=None):
        from common.execution_profile import admission
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
            reason = self._old_work()
            if reason:
                raise Rejected(reason)
            task_id = task_id or uuid.uuid4().hex
            existing = load_existing(runtime_dir(self.config)) or {}
            if existing.get("task_id") == task_id:
                raise Rejected("task_id 已存在；续跑须使用原任务的恢复入口")
            package = match_name(task) or "generic"
            options = options or {}
            backend = select_backend(self.config, package, mock=options.get("mock") is True)
            port = self._port(backend)
            runtime = TaskRuntime(KernelStore(runtime_dir(self.config)), port, config=self.config, package=package)
            desc = task if isinstance(task, str) else (task[0] if task else "")
            runtime.open_task(task_id, task_desc=desc, execution_backend=backend, planning=True,
                              execution_target=target_identity(self.config, backend),
                              reflection_enabled=options.get("reflect", True) is not False)
            self._adopt(runtime)
        runtime.plan(lambda: self._plan(runtime, options))
        self._launch(runtime)
        return self._response(runtime)

    def _plan(self, runtime, options):
        agent, package = self.agent, runtime.package_name
        if package == "reception":
            agent.current_reasoning = "接待业务流程交给统一 Runtime 管理。"
            if runtime.record["execution_backend"] == "reception_mock":
                from kernel.packages.reception_mock import plan
                return plan(runtime.port, options)
            from kernel.packages.reception import PHASES
            return PHASES
        if package == "look":
            from kernel.packages.look import PHASES
            agent.current_reasoning = "现场观察交给 Runtime，只读取现场，不下发身体动作。"
            return PHASES
        if package == "desk":
            planned = agent._plan_desk_tidy(runtime.record["task_desc"])
            agent.current_reasoning = planned["reasoning_explanation"]
            if runtime.record["execution_backend"] == "desk":
                from kernel.packages.desk import plan_steps
                return plan_steps(planned, runtime.port)
        else:
            agent.conversation_history = []
            text = runtime.record["task_desc"]
            experiences = agent._load_experiences(task=text)
            attempts = int((self.config.get("model") or {}).get("model_retry_planning", 0)) + 1
            planned = None
            for _ in range(attempts):
                response = agent.planner.forward(text, agent.conversation_history, experiences)
                planned = agent._extract_json(response)
                if agent.reasoning_and_subtasks_is_right(planned):
                    break
            if not agent.reasoning_and_subtasks_is_right(planned):
                raise Rejected("Planner 没有返回有效计划")
            agent.current_reasoning = planned.get("reasoning_explanation") or ""
            if agent._is_dream_backend():
                from agents.dream_route_policy import enforce_dream_route_plan
                planned = enforce_dream_route_plan(text, planned)
        from kernel.packages.generic import steps_from_subtasks
        kind = "sim" if runtime.record["execution_backend"] == "desk" else "slaver"
        subtasks = planned.get("subtask_list") or []
        check_skills = []
        if kind == "sim":
            from kernel.adapters import parse_sim_action
            from sop.skill_executor import SKILLS, pending_objects
            expanded = []
            for item in subtasks:
                action = parse_sim_action(item.get("subtask"))
                if action and action[0] == "skill":
                    name = action[1]
                    check_skills.append(name)
                    for obj in pending_objects(name, runtime.port._read_world(), runtime.port._read_zones()):
                        expanded.extend([
                            dict(item, subtask=f"抓取 {obj}"),
                            dict(item, subtask=f"放置 {obj} 到 {SKILLS[name]['zone']}"),
                        ])
                else:
                    expanded.append(item)
            subtasks = expanded
        steps = steps_from_subtasks(subtasks, kind=kind)
        from kernel.packages.reception import StepSpec
        steps.extend(StepSpec(f"CHECK_SKILL_{i}", "desk_check", key=name, evidence="desk_tidy")
                     for i, name in enumerate(check_skills, 1))
        if kind == "slaver":
            from dataclasses import replace
            steps = [replace(step, deadline_sec=agent._subtask_wait_timeout_sec()) for step in steps]
        if runtime.record["execution_backend"] in {"slaver:real", "slaver:dream"}:
            from dataclasses import replace
            steps = [replace(step, requires_gate=True) for step in steps]
        if not steps:
            raise Rejected("没有可执行步骤或可核验的完成条件")
        return steps

    def _adopt(self, runtime):
        agent = self.agent
        agent._kernel_runtime = runtime
        agent.current_task_id = runtime.record["task_id"]
        agent.current_task_desc = runtime.record["task_desc"]
        agent.current_task_type = runtime.package_name
        agent.current_task_queue = None
        agent.last_reflection = None

    def attach(self):
        existing = load_existing(runtime_dir(self.config)) or {}
        if not existing.get("task_id"):
            raise Rejected("没有内核任务；旧账本不能作为内核断点续跑")
        runtime = getattr(self.agent, "_kernel_runtime", None)
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
        runtime = TaskRuntime(KernelStore(runtime_dir(self.config)), self._port(backend or ""), config=self.config)
        self._adopt(runtime)
        return runtime

    def control(self, action):
        from common.execution_profile import admission
        with admission(check_block=(action == 'continue')), _ADMISSION:
            runtime = self.attach()
            payload, runtime = control_task(self.config, action, runtime=runtime)
            if payload.get("accepted") and action == "continue":
                self._launch(runtime)
            return payload

    def _launch(self, runtime):
        threading.Thread(target=self._drive, args=(runtime,), daemon=True, name="brain_runtime").start()

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
                self.agent.logger.exception("[kernel] 执行异常")
            if runtime.state in {"succeeded", "recovery_required", "cancelled", "failed"}:
                if runtime.record.get("reflection_enabled", True):
                    self._reflect(runtime)

    def _reflect(self, runtime):
        from sop.episode import episode_from_steps
        record = deepcopy(runtime.record)
        steps = []
        for step_id in record.get("phase_order") or []:
            for attempt in (record.get("steps", {}).get(step_id) or {}).get("attempts") or []:
                verdict = attempt.get("verdict")
                progress = attempt.get("progress") or {}
                evidence = progress.get("evidence") or {}
                steps.append({"phase": step_id, "attempt_id": attempt["attempt_id"],
                              "command_id": attempt["command_id"], "status": verdict or "unknown",
                              "verify_ok": True if verdict == "PASS" else False if verdict == "FAIL" else None,
                              "claimed_ok": evidence.get("claimed_ok"), "detail": evidence.get("detail") or "",
                              "skill": (attempt.get("contract") or {}).get("skill")})
        final = "success" if record["state"] == "succeeded" else "recovery" if record["state"] == "recovery_required" else "failure"
        episode = episode_from_steps(record["task_id"], record["task_desc"], steps,
                                     task_type=record["package"], backend=record.get("execution_backend"),
                                     final=final, error=record.get("blocked_reason"))
        self.agent._run_task_reflection(task_type=record["package"], backend=record.get("execution_backend"),
                                        final=final, episode=episode)

    def _response(self, runtime):
        return {"reasoning_explanation": getattr(self.agent, "current_reasoning", ""),
                "subtask_list": runtime.public_status()["subtask_list"], "source": "kernel"}
