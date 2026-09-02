"""serve_dream HTTP 后端 — 把 DREAM(导航建图组)交付物适配成 FQPlanner 标准后端接口。

上层(master/slaver/nav2 生成器)通过 robot_api 打这里,与打 serve/serve_3dgs 无差别:
  GET  /objects /fixtures /scene /base_status /map_data /health
  POST /nav        → 按对方文件契约写任务、轮询结果(adapters/nav_handoff)
  POST /grasp /place /screenshot → 501,真机上由 VLA 组后端提供(robot_api 分路由)

所有 GET 每次请求都重读交换目录里的 latest 文件 → 天然"随时拿最新",无缓存失效问题。
"""

import json
import math
import os
import sys
import uuid

from flask import Flask, Response, jsonify, request

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_DIR = os.path.dirname(_THIS_DIR)
_PROJECT_ROOT = os.path.dirname(_PKG_DIR)
for p in (_PKG_DIR, _PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml

from adapters import agent_http, exchange, nav_handoff, rosmap, scene_graph
import reviewed_targets

app = Flask(__name__)

_CONFIG_PATH = os.path.join(_PKG_DIR, "config.yaml")


def _cfg():
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _exchange_dir():
    cfg = _cfg()
    d = (cfg.get("exchange") or {}).get("dir", "serve_dream/sample")
    return d if os.path.isabs(d) else os.path.join(_PROJECT_ROOT, d)


def _protocol():
    return str((_cfg().get("exchange") or {}).get("protocol") or "legacy_sync")


def _agent_client():
    cfg = _cfg()
    nav_cfg = cfg.get("nav") or {}
    return agent_http.AgentHttpClient(
        exchange.base_url(cfg) or exchange.nav_url(cfg),
        request_timeout_sec=float(nav_cfg.get("request_timeout_sec", 10)),
    )


def _real_control_gate():
    """Return (authorized, detail).  GET/read-only endpoints never call this."""
    safety = _cfg().get("safety") or {}
    if not bool(safety.get("real_control_enabled", False)):
        return False, "serve_dream/config.yaml safety.real_control_enabled=false"
    env_name = str(safety.get("ack_env") or "FQPLANNER_DREAM_REAL_CONTROL_ACK")
    expected = str(safety.get("ack_value") or "AGENT_HTTP_NAVIGATION_AUTHORIZED")
    if os.environ.get(env_name) != expected:
        return False, f"缺少本地真机确认: export {env_name}={expected}"
    return True, "authorized"


def _require_real_control():
    ok, detail = _real_control_gate()
    if ok:
        return None
    return jsonify({
        "success": False,
        "result": f"真机动作被本地安全门拒绝: {detail}",
        "real_control_authorized": False,
    }), 403


def _exchange_file(key, default):
    cfg = (_cfg().get("exchange") or {})
    return os.path.join(_exchange_dir(), cfg.get(key, default))


def _load_graph_or_none():
    return exchange.load_json_file(_cfg(), _PROJECT_ROOT, "scene_graph",
                                   "total_scene_graph_latest.json")


@app.route("/health", methods=["GET"])
def api_health():
    cfg = _cfg()
    graph, gsrc = _load_graph_or_none()
    map_path = exchange.map_yaml_path(cfg, _PROJECT_ROOT)
    return jsonify({
        "success": True,
        "mode": exchange.mode(cfg),
        "protocol": _protocol(),
        "source": exchange.base_url(cfg) if exchange.mode(cfg) == "http" else _exchange_dir(),
        "nav_url": exchange.nav_url(cfg) or None,
        "real_control_authorized": _real_control_gate()[0],
        "scene_graph": {"found": graph is not None, "source": gsrc,
                        **(scene_graph.graph_meta(graph) if graph else {})},
        "map": {"found": map_path is not None, "path": map_path},
    })


@app.route("/objects", methods=["GET"])
def api_objects():
    graph, path = _load_graph_or_none()
    if graph is None:
        return jsonify({"success": False, "result": f"场景图不存在: {path}"}), 503
    return jsonify(scene_graph.graph_objects(graph))


@app.route("/fixtures", methods=["GET"])
def api_fixtures():
    graph, path = _load_graph_or_none()
    if graph is None:
        return jsonify({"success": False, "result": f"场景图不存在: {path}"}), 503
    return jsonify(scene_graph.graph_fixtures(graph))


@app.route("/scene", methods=["GET"])
def api_scene():
    graph, path = _load_graph_or_none()
    if graph is None:
        return jsonify({"success": False, "result": f"场景图不存在: {path}"}), 503
    base = _read_pose()
    return jsonify({
        "objects": scene_graph.graph_objects(graph),
        "fixtures": scene_graph.graph_fixtures(graph),
        "robot": {"base_pos": base["pos"], "yaw": base["yaw_deg"]} if base else None,
        "meta": scene_graph.graph_meta(graph),
    })


def _read_pose():
    """位姿来源:nav_map_latest.json 的 robot_xyt: [x, y, yaw_rad](http/dir 通道均可)。"""
    if exchange.mode(_cfg()) == "http" and _protocol() == "agent_http_v1":
        try:
            return agent_http.extract_robot_pose(_agent_client().get_status())
        except Exception:
            return None
    data, _src = exchange.load_json_file(_cfg(), _PROJECT_ROOT, "pose_file", "nav_map_latest.json")
    if data is None:
        return None
    try:
        xyt = data.get("robot_xyt")
        if not xyt or len(xyt) < 3:
            return None
        yaw_rad = float(xyt[2])
        return {
            "pos": [float(xyt[0]), float(xyt[1]), 0.0],
            "yaw_rad": round(yaw_rad, 4),
            "yaw_deg": round(math.degrees(yaw_rad) % 360.0, 2),
        }
    except Exception:
        return None


@app.route("/base_status", methods=["GET"])
def api_base_status():
    pose = _read_pose()
    if pose is None:
        return jsonify({"success": False,
                        "result": "DREAM 状态未提供 robot_xyt/pose_xyt/current_pose，无法构造底盘位姿"}), 503
    return jsonify(pose)


@app.route("/map_data", methods=["GET"])
def api_map_data():
    path = exchange.map_yaml_path(_cfg(), _PROJECT_ROOT)
    if path is None:
        return jsonify({"success": False, "result": "地图不可用(检查通道配置与对方数据服务)"}), 503
    return jsonify(rosmap.load_map_data(path))


@app.route("/nav", methods=["POST"])
def api_nav():
    data = request.json or {}
    x, y = data.get("x"), data.get("y")
    if x is None or y is None:
        return jsonify({"success": False,
                        "result": "缺少 x 或 y(serve_dream 只收 map 系坐标;地名→工作点由 slaver 解析)"}), 400
    yaw_deg = data.get("target_yaw", data.get("w"))
    nav_cfg = _cfg().get("nav") or {}
    # Both HTTP and directory handoff can ultimately move the real robot.  Gate
    # before selecting a transport so the backup channel cannot bypass safety.
    blocked = _require_real_control()
    if blocked:
        return blocked
    if exchange.mode(_cfg()) == "http":
        if _protocol() == "agent_http_v1":
            if yaw_deg is None:
                return jsonify({"success": False,
                                "result": "Agent HTTP 导航必须提供最终朝向 target_yaw"}), 400
            try:
                payload = agent_http.build_navigation_payload(
                    x, y, yaw_deg,
                    yaw_unit=data.get("yaw_unit", nav_cfg.get("input_yaw_unit", "degrees")),
                    command_id=data.get("command_id"), task_id=data.get("task_id"),
                    target_id=data.get("target_id"), leg_index=data.get("leg_index", 1),
                    frame_id=data.get("frame_id", "map"),
                    motion_mode=data.get("motion_mode", "forward_path"),
                    require_final_orientation=data.get("require_final_orientation", True),
                    route_phase=data.get("route_phase"),
                    contract_version=data.get("contract_version", "fq/reception-lan/v1"),
                )
                resp = _agent_client().navigate(
                    payload,
                    timeout_sec=float(nav_cfg.get("result_timeout_sec", 180)),
                    poll_interval_sec=float(nav_cfg.get("result_poll_interval_sec", 0.5)),
                )
                return jsonify(resp), 200 if resp.get("success") else 502
            except (ValueError, agent_http.AgentHttpError) as exc:
                return jsonify({"success": False, "result": str(exc)}), 502
        # 旧版同步 nav2_goal_bridge 兼容通道。
        resp, code = exchange.nav_forward(
            _cfg(), x, y, yaw_deg,
            timeout=float(nav_cfg.get("result_timeout_sec", 180)),
        )
        return jsonify(resp), code
    task = nav_handoff.build_task(
        x, y, yaw_deg,
        position_tolerance_m=float(nav_cfg.get("position_tolerance_m", 0.35)),
        yaw_tolerance_deg=float(nav_cfg.get("yaw_tolerance_deg", 25.0)),
    )
    task_dir = nav_handoff.write_task(_exchange_dir(), task)
    print(f"[serve_dream] 导航任务已写出: {task_dir}", file=sys.stderr)
    result = nav_handoff.poll_result(
        task_dir,
        timeout_sec=float(nav_cfg.get("result_timeout_sec", 180)),
        interval_sec=float(nav_cfg.get("result_poll_interval_sec", 1.0)),
    )
    return jsonify(nav_handoff.result_to_response(result))


@app.route("/dream/world", methods=["GET"])
def api_dream_world():
    if exchange.mode(_cfg()) != "http" or _protocol() != "agent_http_v1":
        return jsonify({"success": False, "result": "仅 agent_http_v1 模式支持"}), 409
    try:
        return jsonify(_agent_client().get_world())
    except agent_http.AgentHttpError as exc:
        return jsonify({"success": False, "result": str(exc)}), 502


@app.route("/dream/status", methods=["GET"])
def api_dream_status():
    if exchange.mode(_cfg()) != "http" or _protocol() != "agent_http_v1":
        return jsonify({"success": False, "result": "仅 agent_http_v1 模式支持"}), 409
    try:
        return jsonify(_agent_client().get_status())
    except agent_http.AgentHttpError as exc:
        return jsonify({"success": False, "result": str(exc)}), 502


@app.route("/commands/<command_id>", methods=["GET"])
def api_command_status(command_id):
    try:
        return jsonify(_agent_client().get_command(command_id))
    except agent_http.AgentHttpError as exc:
        return jsonify({"success": False, "result": str(exc)}), 502


@app.route("/camera/status", methods=["GET"])
def api_camera_status():
    try:
        return jsonify(_agent_client().get_camera_status())
    except agent_http.AgentHttpError as exc:
        return jsonify({"success": False, "result": str(exc)}), 502


def _camera_proxy(path, default_type):
    try:
        raw, _status, content_type = _agent_client().request_bytes("GET", path)
        return Response(raw, content_type=content_type or default_type)
    except agent_http.AgentHttpError as exc:
        return jsonify({"success": False, "result": str(exc)}), 502


@app.route("/camera/rgb", methods=["GET"])
def api_camera_rgb():
    return _camera_proxy("/v1/camera/rgb", "image/png")


@app.route("/camera/depth", methods=["GET"])
def api_camera_depth():
    return _camera_proxy("/v1/camera/depth", "image/png")


@app.route("/camera/intrinsics", methods=["GET"])
def api_camera_intrinsics():
    return _camera_proxy("/v1/camera/intrinsics", "application/octet-stream")


@app.route("/inspection", methods=["POST"])
def api_inspection():
    blocked = _require_real_control()
    if blocked:
        return blocked
    data = request.json or {}
    navigation_command_id = data.get("navigation_command_id")
    target_object_id = data.get("target_object_id")
    if not navigation_command_id or not target_object_id:
        return jsonify({"success": False,
                        "result": "缺少 navigation_command_id 或 target_object_id"}), 400
    command_id = data.get("command_id") or f"fq-inspect-{uuid.uuid4().hex[:16]}"
    payload = {"command_id": command_id,
               "navigation_command_id": str(navigation_command_id),
               "target_object_id": str(target_object_id)}
    try:
        nav_cfg = _cfg().get("nav") or {}
        resp = _agent_client().inspect(
            payload,
            timeout_sec=float(data.get("timeout_sec", 300)),
            poll_interval_sec=float(nav_cfg.get("result_poll_interval_sec", 0.5)),
        )
        return jsonify(resp), 200 if resp.get("success") else 502
    except agent_http.AgentHttpError as exc:
        return jsonify({"success": False, "result": str(exc)}), 502


@app.route("/nav/cancel", methods=["POST"])
def api_nav_cancel():
    blocked = _require_real_control()
    if blocked:
        return blocked
    command_id = (request.json or {}).get("command_id")
    if not command_id:
        return jsonify({"success": False, "result": "缺少 command_id"}), 400
    try:
        return jsonify(_agent_client().cancel_navigation(command_id))
    except agent_http.AgentHttpError as exc:
        return jsonify({"success": False, "result": str(exc)}), 502


@app.route("/door_contract", methods=["GET"])
def api_door_contract():
    graph, source = _load_graph_or_none()
    contract = scene_graph.door_navigation_contract(graph or {})
    if contract:
        return jsonify({"success": True, "source": source, "contract": contract})
    fallback = reviewed_targets.door_contract() or {}
    return jsonify({
        "success": bool(fallback),
        "source": "serve_dream/dream_navigation_sop.yaml door_transit",
        "warning": "当前关系图缺少 door_1，使用导航 SOP 的人工审核回退；联调前应让导航组确认最新在线关系图",
        "contract": fallback,
    }), 200 if fallback else 503


@app.route("/reviewed_target/<path:target_name>", methods=["GET"])
def api_reviewed_target(target_name):
    """Read-only natural-language target resolution for preflight/acceptance."""
    spec = reviewed_targets.resolve_reviewed_target(target_name)
    if not spec:
        return jsonify({"success": False,
                        "result": f"没有 '{target_name}' 的人工审核目标"}), 404
    payload = reviewed_targets.as_robot_api_target(spec)
    return jsonify({
        "success": True,
        "input": target_name,
        "frame_id": payload["frame_id"],
        "target_id": payload["target_id"],
        "goal_xyt": [payload["x"], payload["y"], payload["yaw"]],
        "yaw_unit": payload["yaw_unit"],
        "motion_mode": payload["motion_mode"],
        "require_final_orientation": payload["require_final_orientation"],
        "purpose": spec.get("purpose"),
    })


def _not_this_backend(what, owner):
    return jsonify({"success": False,
                    "result": f"serve_dream 不提供 {what}(真机由 {owner} 后端提供,见 robot_api/config.yaml)"}), 501


@app.route("/grasp", methods=["POST"])
def api_grasp():
    return _not_this_backend("/grasp", "VLA 执行组")


@app.route("/place", methods=["POST"])
def api_place():
    return _not_this_backend("/place", "VLA 执行组")


@app.route("/screenshot", methods=["POST"])
def api_screenshot():
    return _not_this_backend("/screenshot", "VLA 执行组")


@app.route("/scene_state", methods=["GET"])
def api_scene_state():
    # belief/scene_memory 是 robocasa 仿真端能力;真机首期用 use_realtime_coords:true(方案Y)。
    return jsonify({"success": False, "result": "serve_dream 暂无 scene_state(真机首期走 /objects 真值)"}), 503


def start_server(port=None):
    port = port or int((_cfg().get("server") or {}).get("port", 5006))
    print(f"[serve_dream] API: http://localhost:{port}  (exchange: {_exchange_dir()})")
    app.run(host="0.0.0.0", port=port, threaded=True)
