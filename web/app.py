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
    feishu_ready_from_text,
    latest_log_file,
    port_open,
    probe_history,
    start_shell,
    tail_file,
)
from web import tmuxctl  # noqa: E402


app = Flask(
    __name__,
    template_folder=str(WEB_DIR / "templates"),
    static_folder=str(WEB_DIR / "static"),
)
app.config["TEMPLATES_AUTO_RELOAD"] = True
_STATUS_POOL = ThreadPoolExecutor(max_workers=8)


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
        "port": service.port,
        "state": state if service.controllable else ("up" if health.get("ok") else "down"),
        "health": health,
        "pids": pids,
        "tmux": _tmux_name(service) if service.controllable else None,
        "attach": f"tmux attach -t {_tmux_name(service)}" if service.controllable else None,
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


def _logs(service, lines: int) -> str:
    if not service.controllable:
        history = probe_history(service.id)
        return history or "尚无探测记录，等待下一次健康检查。"
    chunks = []
    name = _tmux_name(service)
    if tmuxctl.session_exists(name):
        pane = tmuxctl.capture_pane(name, lines)
        if pane.strip():
            chunks.append(f"----- tmux attach -t {name} -----\n{pane}")
    log_path = latest_log_file(service)
    if log_path:
        chunks.append(f"----- {log_path} -----\n{tail_file(log_path, lines)}")
    if not chunks:
        return "暂无日志。服务未在 tmux 中运行，或尚未写出日志文件。"
    return "\n\n".join(chunks)


def _start(service) -> None:
    if not service.controllable:
        raise RuntimeError(f"{service.name} 只能监控，不能由面板启动")
    tmuxctl.stop_unmanaged(service)
    tmuxctl.start_session(service, start_shell(service))
    tmuxctl.wait_session(service)


def _stop(service) -> None:
    if not service.controllable:
        raise RuntimeError(f"{service.name} 只能监控，不能由面板停止")
    tmuxctl.stop_session(service)
    tmuxctl.stop_unmanaged(service)


@app.get("/")
def index():
    return render_template("index.html")


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
    lines = max(20, min(int(request.args.get("lines") or 200), 1000))
    return jsonify({"id": service.id, "text": _logs(service, lines)})


@app.post("/api/services/<service_id>/<action>")
def api_action(service_id: str, action: str):
    if action not in {"start", "stop", "restart"}:
        return jsonify({"error": "不支持的操作"}), 400
    try:
        service = by_id(service_id)
    except KeyError:
        return jsonify({"error": "未知服务"}), 404
    try:
        if action == "start":
            _start(service)
        elif action == "stop":
            _stop(service)
        else:
            _stop(service)
            _start(service)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "service": _service_status(service)})


def main() -> None:
    os.chdir(ROOT)
    app.run(host="0.0.0.0", port=5678, debug=False, threaded=True)


if __name__ == "__main__":
    main()
