"""FQPlanner control panel. Listen on 0.0.0.0:5678. Does not publish tasks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template, request


WEB_DIR = Path(__file__).resolve().parent
ROOT = WEB_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.services import (  # noqa: E402
    by_id,
    catalog,
    display_log_path,
    feishu_ready_from_text,
    latest_log_file,
    master_pin_text,
    port_open,
    start_shell,
    tail_file,
)
from web import dream_remote  # noqa: E402
from web import tmuxctl  # noqa: E402
from web import vla_remote  # noqa: E402


app = Flask(
    __name__,
    template_folder=str(WEB_DIR / "templates"),
    static_folder=str(WEB_DIR / "static"),
)
app.config["TEMPLATES_AUTO_RELOAD"] = True
_STATUS_POOL = ThreadPoolExecutor(max_workers=12)


def _tmux_name(service) -> str:
    return service.window or service.id


def _brain_state(service) -> str:
    if service.controllable and tmuxctl.session_alive(_tmux_name(service)):
        return "tmux"
    if tmuxctl.unmanaged_pids(service) or port_open(service):
        return "unmanaged"
    return "stopped"


def _service_status(service) -> dict:
    state = "stopped"
    pids = []
    if service.controllable:
        state = _brain_state(service)
        if state == "tmux":
            pids = sorted(tmuxctl.tmux_pids_for(service))
        elif state == "unmanaged":
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
        **_review_page(service),
        "remote_control": service.remote_control,
        "remote_actions": list(service.remote_actions),
        "disabled_action": service.disabled_action,
        "disabled_reason": service.disabled_reason,
        "note": service.note,
        "port": service.port,
        "state": state if service.controllable else ("up" if health.get("ok") else "down"),
        "health": health,
        "pids": pids,
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
    selected = str(kind or "auto").strip().lower()
    log_path = latest_log_file(service, selected)
    pin = master_pin_text() if service.id == "master" and selected in {"auto", "brain"} else None
    if not log_path:
        name = service.log_service or service.id
        if service.id == "master" and selected == "brain":
            return (
                "暂无指挥日志。重启 Master 后写入 log/<日期>/master/brain.log。"
                "可先切到「原始」看当前进程输出。",
                None,
                pin,
            )
        if service.id == "master" and selected == "http":
            return "暂无 HTTP 访问日志。", None, None
        return f"暂无日志。尚未写出 log/<日期>/{name}/ 文件。", None, None
    return tail_file(log_path, lines), display_log_path(log_path), pin


def _start(service) -> None:
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


@app.get("/")
def index():
    return render_template("index.html")


# Environment application runs outside the request thread; polling reports progress.
_SWITCH_POOL = ThreadPoolExecutor(max_workers=1)
_SWITCH_JOB = None
import threading
_SWITCH_GUARD = threading.Lock()


@app.get("/api/execution")
def api_execution():
    from web.execution import Switcher
    try:
        result = Switcher().status()
        job = _SWITCH_JOB
        result['applying'] = result['switching'] or bool(job and not job.done())
        result['error'] = str(job.exception()) if job and job.done() and job.exception() else None
        return jsonify(result)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.post("/api/execution/preview")
def api_execution_preview():
    from web.execution import Switcher
    try:
        return jsonify(Switcher().preview(request.get_json()))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.post("/api/execution/apply")
def api_execution_apply():
    global _SWITCH_JOB
    from web.execution import Switcher
    try:
        config = request.get_json()
        switcher = Switcher()
        switcher.preview(config)
        with _SWITCH_GUARD:
            if _SWITCH_JOB and not _SWITCH_JOB.done():
                return jsonify({'error': '正在应用环境，请稍后'}), 409
            _SWITCH_JOB = _SWITCH_POOL.submit(switcher.apply, config)
        return jsonify({'accepted': True}), 202
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400


@app.get("/api/status")
def api_status():
    services = catalog()
    items = list(_STATUS_POOL.map(_service_status, services))
    return jsonify(
        {
            "tmux": tmuxctl.tmux_available(),
            "attach": "tmux ls",
            "brain": [item for item in items if item["layer"] == "brain"],
            "robot": [item for item in items if item["layer"] == "robot"],
        }
    )


@app.get("/api/services/<service_id>/logs")
def api_logs(service_id: str):
    try:
        service = by_id(service_id)
    except KeyError:
        return jsonify({"error": "未知服务"}), 404
    raw_lines = request.args.get("lines") or 3000
    try:
        wanted = int(raw_lines)
    except (TypeError, ValueError):
        wanted = 3000
    lines = max(20, min(wanted, 8000))
    kind = request.args.get("kind") or "auto"
    text, path, pin = _logs(service, lines, kind)
    return jsonify(
        {
            "id": service.id,
            "kind": kind,
            "text": text,
            "path": path,
            "pin": pin,
        }
    )


@app.post("/api/services/<service_id>/<action>")
def api_action(service_id: str, action: str):
    try:
        service = by_id(service_id)
    except KeyError:
        return jsonify({"error": "未知服务"}), 404
    allowed = {"start", "stop", "restart"} | set(service.remote_actions)
    if action not in allowed:
        return jsonify({"error": "不支持的操作"}), 400
    from contextlib import nullcontext
    from common.execution_profile import admission
    try:
        with nullcontext() if service.remote_control else admission(check_block=False):
            if action == "start":
                _start(service)
            elif action == "stop":
                _stop(service)
            elif action == "restart":
                _stop(service)
                _start(service)
            else:
                _remote_extra(service, action)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "service": _service_status(service)})


def main() -> None:
    os.chdir(ROOT)
    from common.log_setup import attach_process_log

    attach_process_log("panel")
    app.run(host="0.0.0.0", port=5678, debug=False, threaded=True)


if __name__ == "__main__":
    main()
