"""DREAM/VLA LAN endpoints backed by a timed logical scene, not physical simulation."""
import math
import urllib.parse
import urllib.request
import json

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from contracts.tasks import CONTRACT_VERSION
from contracts.reception_lan import InvalidRequest, validate_request
from execution.reception_sim.store import Conflict, SCENARIOS, Store, stamp

PORTS = {"nav": 18001, "vla": 18091}


def create_app(role, state_path, *, delay=2.0, scenario="success", nav_url="http://127.0.0.1:18001", nav_get=None):
    if role not in PORTS or scenario not in SCENARIOS or not math.isfinite(delay) or not .01 <= delay <= 3600:
        raise ValueError("无效的角色、场景或延时（0.01–3600 秒）")
    parsed = urllib.parse.urlparse(nav_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise ValueError("协议模拟只能查询本机导航模拟进程")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 32768
    store = Store(state_path)
    app.extensions["simulator"] = store

    def get_nav(path):
        if nav_get:
            return nav_get(path)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(nav_url.rstrip("/") + path, timeout=3) as response:
            return json.load(response)

    def error(code, message, status):
        return jsonify(success=False, error={"code": code, "message": message, "retryable": False,
            "details": {"action_started": False, "action_state_uncertain": False}}), status

    @app.errorhandler(Conflict)
    def conflict(exc):
        return error(exc.code, exc.message, exc.status)

    @app.errorhandler(InvalidRequest)
    def invalid(exc):
        return error("INVALID_REQUEST", str(exc), 400)

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return error("HTTP_ERROR", exc.description, exc.code)

    @app.before_request
    def contract_header():
        header = request.headers.get("X-Contract-Version")
        if header and header != CONTRACT_VERSION:
            raise InvalidRequest("X-Contract-Version 不匹配")

    @app.get("/health")
    def health():
        return jsonify(contract_version=CONTRACT_VERSION,
                       service="dream-agent-http" if role == "nav" else "vla-task-service",
                       status="ok", interface_online=True, time=stamp(),
                       simulation={"kind": "protocol", "role": role, "instance_id": store.instance_id,
                                   "scenario": scenario, "delay_sec": delay})

    def status():
        active, receipt = store.status()
        current = active if active and active["role"] == role else None
        nav_ready = not active or active["role"] != "vla"
        common = {"contract_version": CONTRACT_VERSION,
                  "active_command_id": current["body"]["command_id"] if current else None}
        if role == "nav":
            value = dict(common, interface_online=True, world_state_available=True,
                         active_command_state=current["state"] if current else None,
                         navigation_transport_ready=nav_ready, motion_ready=not active,
                         motion_blockers=["COMMAND_ACTIVE"] if active else [],
                         localization_initialized=True, localization_approved=True, gateway_ready=True, token_ready=True,
                         agent_mode={"enabled": True, "auto_queue_enabled": False, "publish_nav_done_for_vla": False})
        else:
            value = dict(common, service_ready=True, policy_running=current is not None,
                         action_port={"owner": "vla" if current else "navigation", "navigation_port_ready": nav_ready,
                                      "last_changed_at": stamp()},
                         camera={"owner": "vla", "ready": False, "streaming": False},
                         robot={"sonic_healthy": True, "gripper_ready": True})
        if receipt and ((role == "nav" and receipt["kind"] == "to_nav") or
                        (role == "vla" and receipt["kind"] in {"to_vla", "safe_idle"})):
            value["control_receipt"] = receipt
        return jsonify(value)

    app.add_url_rule("/v1/status" if role == "nav" else "/v1/vla/control/status", "status", status)

    def submit():
        body = request.get_json()
        validate_request(role, body)
        rejection = None
        # A duplicate only reads its existing transaction, even if the peer is now offline.
        try:
            old = store.command(role, body["command_id"])
        except Conflict as exc:
            if exc.status != 404:
                raise
            old = None
        if role == "vla" and old is None:
            try:
                peer = get_nav("/health")
                sim = peer.get("simulation") or {}
                if (peer.get("contract_version") != CONTRACT_VERSION or sim.get("kind") != "protocol"
                        or sim.get("role") != "nav" or sim.get("instance_id") != store.instance_id):
                    raise ValueError("导航模拟服务身份或共享状态不匹配")
                proof = get_nav("/v1/commands/" + urllib.parse.quote(body["navigation_proof"]["dream_command_id"], safe=""))
                nav = get_nav("/v1/status")
            except Exception as exc:
                return error("NAVIGATION_UNAVAILABLE", str(exc), 503)
            result = proof.get("result") or {}
            if (proof.get("contract_version") != CONTRACT_VERSION or proof.get("task_id") != body["task_id"]
                    or proof.get("command_id") != body["navigation_proof"]["dream_command_id"]
                    or proof.get("target_id") != body["target_area"] or proof.get("state") != "succeeded"
                    or result.get("success") is not True or result.get("reached") is not True
                    or result.get("navigation_stopped") is not True or nav.get("active_command_id") is not None
                    or nav.get("active_command_state") is not None or nav.get("navigation_transport_ready") is not True):
                rejection = ("NAVIGATION_PROOF_INVALID", "原导航命令及导航停止状态不满足操控条件")
        value, code = store.submit(role, body, delay, scenario, rejection=rejection)
        if code == 202:
            path = "/v1/commands/" if role == "nav" else "/v1/vla/tasks/"
            value = {k: value[k] for k in ("contract_version", "task_id", "command_id", "state", "accepted_at")}
            value.update(accepted=True, status_url=path + body["command_id"])
            value["target_id" if role == "nav" else "operation"] = body["target_id" if role == "nav" else "operation"]
        return jsonify(value), code

    app.add_url_rule("/v1/navigation/goals" if role == "nav" else "/v1/vla/tasks", "submit", submit, methods=["POST"])

    def command(command_id):
        return jsonify(store.command(role, command_id))

    app.add_url_rule(("/v1/commands/" if role == "nav" else "/v1/vla/tasks/") + "<command_id>", "command", command)

    def cancel(command_id=None):
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get("task_id"), str) or not body["task_id"]:
            raise InvalidRequest("取消必须指定 task_id")
        target = command_id or body.get("command_id")
        if not isinstance(target, str) or not target or body.get("command_id", target) != target:
            raise InvalidRequest("取消 command_id 不匹配")
        store.command(role, target, cancel_task=body["task_id"])
        return jsonify(accepted=True, task_id=body["task_id"], command_id=target), 202

    app.add_url_rule("/v1/navigation/cancel" if role == "nav" else "/v1/vla/tasks/<command_id>/cancel",
                     "cancel", cancel, methods=["POST"])

    @app.get("/v1/camera/status")
    def camera():
        return jsonify(ready=False, streaming=False, reason="protocol_simulator_has_no_camera")

    if role == "nav":
        @app.get("/v1/world")
        def world():
            return jsonify(contract_version=CONTRACT_VERSION, frame_id="map", door_object_id="door_1",
                           relation_graph_url="/total_scene_graph_latest.json", updated_at=stamp())

        @app.get("/total_scene_graph_latest.json")
        def graph():
            return jsonify(frame_id="map", objects=[{"id": key, "type": kind} for key, kind in
                [("table_2", "table"), ("door_1", "door"), ("table_1", "table"), ("cola_can_1", "can")]], updated_at=stamp())

    @app.post("/v1/inspection")
    @app.post("/v1/camera/snapshots")
    def unsupported():
        return error("UNSUPPORTED_CAPABILITY", "协议模拟不生成视觉证据；请关闭可选检查", 501)

    return app
