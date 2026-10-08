"""Process control and health; no Flask or task imports."""
from ops.services import (by_id, catalog, display_log_path, feishu_ready_from_text,
    latest_log_file, port_open, start_shell, tail_file)
from ops import dream_remote, tmuxctl, vla_remote, simple_remote

def _tmux_name(service) -> str:
    return service.window or service.id


def _brain_state(service) -> str:
    if service.controllable and tmuxctl.session_alive(_tmux_name(service)):
        return "tmux"
    if tmuxctl.unmanaged_pids(service) or port_open(service):
        return "unmanaged"
    return "stopped"


def _service_status(service, *, include_pids=True) -> dict:
    state = "stopped"
    pids = []
    if service.controllable:
        state = _brain_state(service)
        if include_pids and state == "tmux":
            pids = sorted(tmuxctl.tmux_pids_for(service))
        elif include_pids and state == "unmanaged":
            pids = tmuxctl.unmanaged_pids(service)
    health = {"ok": False, "detail": "未运行"}
    if not service.controllable:
        health = service.health(service) if service.health else health
    elif state != "stopped":
        if service.id == "slaver":
            health = {"ok": True, "detail": "进程在运行"}
        elif service.id == "feishu":
            health = _feishu_health(service, state)
        elif service.health:
            health = service.health(service)
    return {
        **service_description(service),
        **_review_page(service),
        "state": state if service.controllable else ("up" if health.get("ok") else "down"),
        "health": health,
        "pids": pids,
    }


def service_description(service) -> dict:
    """Static card data. Rendering a page must not probe hosts or processes."""
    return {
        "id": service.id,
        "name": service.name,
        "layer": service.layer,
        "controllable": service.controllable,
        "confirm_restart": service.confirm_restart,
        "confirm_start": service.confirm_start,
        "confirm_start_message": service.confirm_start_message,
        "confirm_stop": service.confirm_stop,
        "confirm_stop_message": service.confirm_stop_message,
        "action_confirms": dict(service.action_confirms),
        "remote_control": service.remote_control,
        "remote_actions": list(service.remote_actions),
        "disabled_action": service.disabled_action,
        "disabled_reason": service.disabled_reason,
        "note": service.note,
        "port": service.port,
        "state": "checking",
        "health": {"ok": None, "detail": "正在检查…"},
        "pids": [],
        "tmux": _tmux_name(service) if service.controllable else None,
        "attach": f"tmux attach -t {_tmux_name(service)}" if service.controllable else None,
    }



def _review_page(service) -> dict:
    if service.remote_control != "ssh_dream":
        return {}
    dream_remote.ensure_log_follower()
    return {
        "review_url": dream_remote.review_url_for_panel(),
        "review_ok": dream_remote.review_ready(),
    }


def _feishu_health(service, state: str) -> dict:
    text = ""
    log_path = latest_log_file(service)
    if log_path:
        text = tail_file(log_path, 40)
    if not feishu_ready_from_text(text):
        text += "\n" + tmuxctl.capture_pane(_tmux_name(service), 20)
    if feishu_ready_from_text(text):
        return {"ok": True, "detail": "飞书长连接已就绪"}
    if state != "stopped":
        return {"ok": False, "detail": "进程在运行，尚未看到长连接就绪"}
    return {"ok": False, "detail": "未运行"}


def _logs(service, lines: int, kind: str | None = None) -> tuple[str, str | None, str | None]:
    if service.id == "simple_o7":
        return simple_remote.logs(lines)
    selected = str(kind or "auto").strip().lower()
    log_path = latest_log_file(service, selected)
    # Historical failure pins are archival, not current task alerts.
    pin = None
    if not log_path:
        name = service.log_service or service.id
        if service.id == "master" and selected == "brain":
            return (
                "暂无大脑日志。可展开高级诊断查看进程输出。",
                None,
                pin,
            )
        if service.id == "master" and selected == "http":
            return "暂无 HTTP 访问日志。", None, None
        return f"暂无日志。尚未写出 logs/<日期>/{name}/ 文件。", None, None
    return tail_file(log_path, lines), display_log_path(log_path), pin


def _start(service) -> None:
    if service.remote_control == "ssh_simple":
        simple_remote.run("start")
        return
    if service.remote_control == "ssh_vla":
        vla_remote.run("start")
        return
    if service.remote_control == "ssh_dream":
        dream_remote.run("start")
        return
    if not service.controllable:
        raise RuntimeError(f"{service.name} 只能监控，不能由面板启动")
    tmuxctl.stop_unmanaged(service)
    tmuxctl.start_session(service, start_shell(service))
    tmuxctl.wait_session(service)


def _stop(service) -> None:
    if service.remote_control == "ssh_simple":
        simple_remote.run("stop")
        return
    if service.remote_control == "ssh_vla":
        vla_remote.run("stop")
        return
    if service.remote_control == "ssh_dream":
        dream_remote.run("stop")
        return
    if not service.controllable:
        raise RuntimeError(f"{service.name} 只能监控，不能由面板停止")
    tmuxctl.stop_session(service)
    tmuxctl.stop_unmanaged(service)


def _remote_extra(service, action: str) -> None:
    if service.remote_control == "ssh_dream":
        dream_remote.run(action)
        return
    raise RuntimeError(f"{service.name} 不支持 {action}")
