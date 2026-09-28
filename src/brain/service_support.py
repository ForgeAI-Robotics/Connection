"""Open a kernel reception task. The default switch does not call this."""

from __future__ import annotations

import os
from shared.paths import workspace_root

from contracts.tasks import CONTRACT_VERSION
from brain.kernel.runtime import status_from_record
from brain.app import create_runtime
from brain.storage.tasks import KernelStore, load_existing


def runtime_dir(config) -> str:
    real = (config or {}).get("reception_real") or {}
    configured = real.get("kernel_runtime_dir")
    if configured:
        from shared.paths import data_path
        return str(data_path(configured))
    return str(workspace_root() / "data/tasks")


def _port(config, port, record=None, package=None):
    if port is not None:
        return port
    from brain.adapters.ports import build_port
    record = record or {}
    backend = record.get("execution_backend") or {
        "generic": "desk", "look": "camera", "desk": "desk",
    }.get(package or record.get("package"), "reception_real")
    return build_port(config, backend)


def open_runtime(config, task_id, *, task_desc="", port=None, force_new=False, package=None, phases=None):
    store = KernelStore(runtime_dir(config))
    runtime = create_runtime(config, _port(config, port, package=package), package=package)
    runtime.open_task(task_id, task_desc=task_desc, force_new=force_new, phases=phases)
    return runtime


def resume_runtime(config, *, port=None):
    from contracts.tasks import Rejected

    store = KernelStore(runtime_dir(config))
    runtime = create_runtime(config, _port(config, port, store.load_state()), package=(store.load_state() or {}).get("package") or "reception")
    if runtime.state is None:
        raise Rejected("没有可续跑的内核任务")
    if runtime.state == "recovery_required":
        runtime.resume()
    return runtime


def attach_runtime(config, *, port=None):
    from contracts.tasks import Rejected

    store = KernelStore(runtime_dir(config))
    runtime = create_runtime(config, _port(config, port, store.load_state()), package=(store.load_state() or {}).get("package") or "reception")
    if runtime.state is None:
        raise Rejected("没有可操作的内核任务")
    return runtime


def control_task(config, action, *, port=None, runtime=None, step_id=None):
    """Pause, continue, or cancel the open kernel task. A closed switch does not touch a ledger."""
    from contracts.tasks import IllegalTransition, Rejected
    from brain.config_flags import kernel_enabled

    if runtime is None and not kernel_enabled(config):
        from contracts.tasks import NON_TERMINAL_STATES

        existing = load_existing(runtime_dir(config)) or {}
        if existing.get("state") not in NON_TERMINAL_STATES:
            return {"accepted": False, "error": "内核开关关闭", "state": None}, None
    try:
        if runtime is None:
            runtime = attach_runtime(config, port=port)
        elif port is not None and runtime.port is None:
            runtime.port = port
        if action == "pause":
            runtime.request_pause()
            paused = runtime.state == "paused"
            return {
                "accepted": True,
                "state": runtime.state,
                "paused": paused,
                "error": None if paused else "暂停没有停止回执",
            }, runtime
        if action == "continue":
            result = runtime.continue_current()
            return {"accepted": True, "state": runtime.state, "error": None, **result}, runtime
        if action == "skip":
            result = runtime.skip_current(step_id=step_id)
            return {"accepted": True, "state": runtime.state, **result}, runtime
        if action == "cancel":
            result = runtime.request_cancel()
            if runtime.state == "cancelling":
                runtime.settle_cancel()
            return {
                "accepted": bool(result.get("accepted")) or runtime.state == "cancelled",
                "completed": runtime.state == "cancelled",
                "state": runtime.state,
                "error": None if result.get("accepted") else (result.get("error") or "取消未被受理"),
            }, runtime
        raise Rejected(f"未知控制动作: {action}")
    except (Rejected, IllegalTransition) as exc:
        state = None if runtime is None else runtime.state
        return {"accepted": False, "error": str(exc), "state": state}, runtime


def peek_status(config):
    record = load_existing(runtime_dir(config))
    if not record:
        return None
    return status_from_record(record)


def status_view(config, runtime):
    if runtime is not None:
        return runtime.public_status()
    loaded = peek_status(config)
    if loaded is not None:
        return loaded
    return {
        "active": False,
        "terminal": True,
        "state": None,
        "blocks_new_motion": False,
        "can_resume": False,
        "source": "kernel",
        "subtask_list": [],
        "contract_version": CONTRACT_VERSION,
    }
