"""Independent operations UI; task submissions belong to the brain API."""
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, render_template, request
from ops.services import *
from ops.control import (_start, _stop, _brain_state, _service_status, _remote_extra, _logs, service_description)
from ops import dream_remote, tmuxctl, vla_remote


def create_app(*, switch_pool=None):
    app = Flask(__name__)
    _STATUS_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix='ops-health')
    @app.get("/")
    def index():
        descriptions = [service_description(service) for service in catalog()]
        bootstrap = {layer: [item for item in descriptions if item['layer'] == layer]
                     for layer in ('brain', 'environment', 'support', 'robot')}
        return render_template("index.html", bootstrap=bootstrap)


    # Environment application runs outside the request thread; polling reports progress.
    _SWITCH_POOL = switch_pool or ThreadPoolExecutor(max_workers=1, thread_name_prefix="ops-switch")
    switch_state = {"job": None}
    app.extensions["switch"] = switch_state
    import threading
    _SWITCH_GUARD = threading.Lock()


    @app.get("/api/execution")
    def api_execution():
        from ops.execution import Switcher
        try:
            result = Switcher().status()
            job = switch_state["job"]
            result['applying'] = result['switching'] or bool(job and not job.done())
            result['error'] = str(job.exception()) if job and job.done() and job.exception() else None
            return jsonify(result)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400


    @app.post("/api/execution/preview")
    def api_execution_preview():
        from ops.execution import Switcher
        try:
            return jsonify(Switcher().preview(request.get_json()))
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400


    @app.post("/api/execution/apply")
    def api_execution_apply():
        from ops.execution import Switcher
        try:
            config = request.get_json()
            switcher = Switcher()
            switcher.preview(config)
            with _SWITCH_GUARD:
                if switch_state["job"] and not switch_state["job"].done():
                    return jsonify({'error': '正在应用环境，请稍后'}), 409
                switch_state["job"] = _SWITCH_POOL.submit(switcher.apply, config)
            return jsonify({'accepted': True}), 202
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400


    @app.get("/api/status")
    def api_status():
        layer = request.args.get('layer')
        if layer and layer not in {'brain', 'environment', 'support', 'robot'}:
            return jsonify({'error': '未知服务层'}), 400
        services = [s for s in catalog() if not layer or s.layer == layer]
        items = list(_STATUS_POOL.map(lambda s: _service_status(s, include_pids=False), services))
        return jsonify(
            {
                "tmux": tmuxctl.tmux_available(),
                "attach": "tmux ls",
                "brain": [item for item in items if item["layer"] == "brain"],
                "environment": [item for item in items if item["layer"] == "environment"],
                "support": [item for item in items if item["layer"] == "support"],
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
        payload = {
                "id": service.id,
                "kind": kind,
                "text": text,
                "path": path,
                "pin": pin,
            }
        return jsonify(payload)

    @app.get('/api/brain/overview')
    def brain_overview():
        from ops.brain_view import overview
        return jsonify(overview())


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
        from shared.execution_profile import admission
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


    from ops.validation import register
    register(app)
    app.extensions["workers"] = (_STATUS_POOL, _SWITCH_POOL)
    return app
