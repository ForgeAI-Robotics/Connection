"""
server.py - Flask API 服务
通过命令队列与仿真器主循环通信，避免多线程同时调用 env.step()
"""

import os
import sys
import math
import time
import threading
from pathlib import Path

import numpy as np
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
from PIL import Image as PILImage

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robot_api.scene_metadata import load_camera_config

from tools.arm import (
    get_arm_info, get_obj_pos,
    move_arm,
    open_gripper, close_gripper, is_grasped,
)
from tools.act_grasp import act_grasp_init, act_grasp_step
from tools.pi05_grasp import pi05_grasp_init, pi05_grasp_step
from tools.move import (
    base_link_yaw_deg_from_chassis_yaw_deg,
    base_link_yaw_from_chassis_yaw,
    get_base_info,
    nav,
    move,
    stop_base,
    set_base_velocity,
)

app = Flask(__name__)
CORS(app)

_env_holder = {"env": None}
_cmd_queue = []
_results = {}
_active_command = None
_queue_lock = threading.Lock()
_env_lock = threading.RLock()
_base_cmd = {
    "Vx": 0.0,
    "Vy": 0.0,
    "Vw": 0.0,
    "expires_at": 0.0,
    "last_log_at": 0.0,
    "was_active": False,
}
_base_status_cache = {}

NAV_SUBSTEPS = 1
ARM_STEP = 0.025
CMD_VEL_MAX_LINEAR = 1.0
CMD_VEL_MAX_ANGULAR = 1.5

_recording = {
    "active": False,
    "frames": [],
    "fps": 1,
    "view_height": 240,
    "view_width": 320,
    "last_capture": 0,
    "interval": 1.0,
}

RECORD_CAMERAS = []

_video_dir = os.path.join(os.path.dirname(__file__), "..", "videos")
_camera_config_cache = None
_act_config = {
    "url": None,        # ACT inference service URL, e.g. http://localhost:5003
    "max_steps": 300,   # default grasp cap; overridden by config.yaml policy_services.act.max_steps
}
_pi05_config = {"url": None, "task": None, "loaded": False}


def _ensure_pi05_config():
    """Lazily load PI0.5 service url + default task from robot_api/config.yaml."""
    if _pi05_config["loaded"]:
        return
    _pi05_config["loaded"] = True
    try:
        from robot_api.config import load_robot_api_config
        svc = load_robot_api_config().policy_service("pi05")
        if svc is not None:
            _pi05_config["url"] = svc.url or None
            _pi05_config["task"] = (svc.raw or {}).get("task") or "pick up the object and place it"
            if _pi05_config["url"]:
                print(f"[service] 🤖 PI0.5 推理服务: {_pi05_config['url']}")
    except Exception as exc:
        print(f"[service] 读取 pi05 配置失败: {exc}")


def _camera_model_name(camera_name: str) -> str:
    cameras = _camera_config().get("cameras", {})
    entry = cameras.get(camera_name, {})
    if isinstance(entry, dict) and "model_name" in entry:
        return entry["model_name"]
    return camera_name


def _find_camera_id(model, camera_name: str) -> int:
    model_name = _camera_model_name(camera_name)
    aliases = {
        "overhead_cam": ("follower", "base_cam", "chassis_cam"),
        "head_cam": ("base_cam", "chassis_cam"),
        "right_arm_cam": ("right_ee_cam",),
        "left_arm_cam": ("left_ee_cam",),
    }
    candidates = (model_name,) + aliases.get(camera_name, ())
    for idx, camera in enumerate(model.cameras.cameras if hasattr(model.cameras, 'cameras') else model.cameras):
        name = getattr(camera, "name", "")
        for candidate in candidates:
            if name == candidate or name.endswith(candidate):
                return idx
    return 0


def _camera_config():
    global _camera_config_cache
    if _camera_config_cache is None:
        _camera_config_cache = load_camera_config()
    return _camera_config_cache


def _screenshot_defaults():
    return (_camera_config().get("screenshot", {}) or {})


def _preview_config():
    return (_camera_config().get("preview", {}) or {})


def _camera_labels():
    return {
        name: values.get("label", name)
        for name, values in (_camera_config().get("cameras", {}) or {}).items()
        if isinstance(values, dict)
    }


def _render_combined_frame(env, view_w, view_h):
    cameras = _preview_config().get("cameras", ["overhead_cam"])
    panels = []
    for cam_name in cameras[:4]:
        try:
            cam_id = _find_camera_id(env.model, cam_name)
            img = env.render_frame(cam_id=cam_id, width=view_w, height=view_h)
        except Exception:
            img = np.zeros((view_h, view_w, 3), dtype=np.uint8)
        panels.append(PILImage.fromarray(img))
    while len(panels) < 4:
        panels.append(PILImage.new("RGB", (view_w, view_h), (0, 0, 0)))
    image = PILImage.new("RGB", (view_w * 2, view_h * 2))
    for idx, panel in enumerate(panels):
        image.paste(panel, ((idx % 2) * view_w, (idx // 2) * view_h))
    return np.array(image)


def _render_camera_preview(env, camera_names, width, height):
    panels = []
    for name in camera_names:
        try:
            cam_id = _find_camera_id(env.model, name)
            img = env.render_frame(cam_id=cam_id, width=width, height=height)
        except Exception:
            img = np.zeros((height, width, 3), dtype=np.uint8)
        panel = PILImage.fromarray(img)
        panels.append(panel)
    return panels


def try_record_frame():
    if not _recording["active"]:
        return
    now = time.time()
    if now - _recording["last_capture"] < _recording["interval"]:
        return
    env = _get_env()
    if env is None:
        return
    try:
        combined = _render_combined_frame(
            env, _recording["view_width"], _recording["view_height"]
        )
        _recording["frames"].append(combined)
        _recording["last_capture"] = now
        if len(_recording["frames"]) == 1:
            print(f"[record] 首帧捕获成功: shape={combined.shape}", flush=True)
    except Exception as e:
        print(f"[record] 渲染帧失败: {e}", flush=True)


def get_lock():
    return _env_lock


def set_act_config(url: str | None):
    """Set ACT inference config. Reads url + max_steps from robot_api/config.yaml."""
    try:
        from robot_api.config import load_robot_api_config
        cfg = load_robot_api_config()
        svc = cfg.policy_service("act")
    except Exception as exc:
        print(f"[service] 读取 policy_services 配置失败: {exc}")
        svc = None
    if url is None:
        url = svc.url if svc else None
    if svc is not None:
        try:
            _act_config["max_steps"] = int((svc.raw or {}).get("max_steps", 300))
        except (TypeError, ValueError):
            _act_config["max_steps"] = 300
    _act_config["url"] = url.rstrip("/") if url else None
    if _act_config["url"]:
        print(f"[service] 🔮 ACT 推理服务: {_act_config['url']} (max_steps={_act_config['max_steps']})")
    else:
        print("[service] ACT 推理服务: 未配置 (在 robot_api/config.yaml 中设置 policy_services.act.enabled: 1)")


def has_active_base_command():
    return _active_command is not None and _active_command.get("type") in ("nav", "cmd_vel", "move_duration")


def has_active_act_command():
    return _active_command is not None and _active_command.get("type") == "act_grasp"


def set_base_velocity_cmd(Vx=0.0, Vy=0.0, Vw=0.0, timeout=0.25):
    print(f"[set_base_velocity] Vx={Vx}, Vy={Vy}, Vw={Vw}, timeout={timeout}", flush=True)
    with _queue_lock:
        _base_cmd["Vx"] = float(Vx)
        _base_cmd["Vy"] = float(Vy)
        _base_cmd["Vw"] = float(Vw)
        _base_cmd["expires_at"] = time.time() + timeout
        now = time.time()
        if now - _base_cmd["last_log_at"] > 1.0:
            print(
                f"[cmd_vel] Vx={_base_cmd['Vx']:.3f}, Vy={_base_cmd['Vy']:.3f}, Vw={_base_cmd['Vw']:.3f}",
                flush=True,
            )
            _base_cmd["last_log_at"] = now


def get_base_action():
    with _queue_lock:
        if time.time() > _base_cmd["expires_at"]:
            if _base_cmd["was_active"]:
                _base_cmd["was_active"] = False
                print("[get_base_action] cmd_vel 过期，返回停止信号 (0,0,0)", flush=True)
                return (0.0, 0.0, 0.0)
            return None
        _base_cmd["was_active"] = True
        return (
            float(np.clip(_base_cmd["Vx"], -CMD_VEL_MAX_LINEAR, CMD_VEL_MAX_LINEAR)),
            float(np.clip(_base_cmd["Vy"], -CMD_VEL_MAX_LINEAR, CMD_VEL_MAX_LINEAR)),
            float(np.clip(_base_cmd["Vw"], -CMD_VEL_MAX_ANGULAR, CMD_VEL_MAX_ANGULAR)),
        )


def apply_base_velocity(env):
    now = time.time()
    if now >= _base_cmd.get("expires_at", 0):
        if _base_cmd.get("was_active"):
            _base_cmd["was_active"] = False
            stop_base(env)
            return True
        return False
    _base_cmd["was_active"] = True
    vx = float(np.clip(_base_cmd["Vx"], -CMD_VEL_MAX_LINEAR, CMD_VEL_MAX_LINEAR))
    vy = float(np.clip(_base_cmd["Vy"], -CMD_VEL_MAX_LINEAR, CMD_VEL_MAX_LINEAR))
    vw = float(np.clip(_base_cmd["Vw"], -CMD_VEL_MAX_ANGULAR, CMD_VEL_MAX_ANGULAR))
    set_base_velocity(env, vx, vy, vw)
    return True


def _get_env():
    return _env_holder["env"]


def _read_base_info(env):
    info = get_base_info(env)
    _base_status_cache.clear()
    _base_status_cache.update(_np_to_list(info))
    return info


def _np_to_list(obj):
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, dict):
        return {k: _np_to_list(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_np_to_list(v) for v in obj]
    return obj


def _object_link(env, public_name):
    if hasattr(env, "object_link"):
        return env.object_link(public_name)
    return public_name


def _scene_objects(env):
    object_map = getattr(env, "scene_objects", {}) or {}
    result = {}
    for public_name, link_name in object_map.items():
        if link_name not in env._link_name_to_idx:
            continue
        pos = get_obj_pos(env, link_name)
        result[public_name] = {
            "pos": pos.tolist(),
            "grasped": is_grasped(env, link_name),
            "link": link_name,
        }
    return result


def _scene_fixtures(env):
    return dict(getattr(env, "scene_fixtures", {}) or {})


def submit_command(cmd_type, params):
    event = threading.Event()
    cmd_id = id(event)
    cmd = {
        "id": cmd_id,
        "type": cmd_type,
        "params": params,
        "event": event,
    }
    with _queue_lock:
        _cmd_queue.append(cmd)
    event.wait(timeout=120)
    with _queue_lock:
        result = _results.pop(cmd_id, {"error": "超时"})
    return result


def _finish_command(cmd, result):
    global _active_command
    if cmd.get("type") in ("nav", "cmd_vel", "move_duration"):
        env = _get_env()
        if env is not None:
            try:
                _read_base_info(env)
            except Exception:
                pass
    with _queue_lock:
        _results[cmd["id"]] = result
    cmd["event"].set()
    _active_command = None


def _normalize_angle_deg(angle):
    return (float(angle) + 180.0) % 360.0 - 180.0


def _public_yaw_deg(info):
    return info.get("base_link_yaw_deg", base_link_yaw_deg_from_chassis_yaw_deg(info["yaw_deg"]))


def _public_yaw_rad(info):
    return info.get("base_link_yaw_rad", base_link_yaw_from_chassis_yaw(info["yaw_rad"]))


def _step_active_command(env):
    cmd = _active_command
    if cmd is None:
        return False
    try:
        cmd_type = cmd["type"]
        state = cmd["state"]
        if cmd_type == "nav":
            for _ in range(NAV_SUBSTEPS):
                info = get_base_info(env)
                x_now, y_now = info["pos"][0], info["pos"][1]
                yaw_now = _public_yaw_rad(info)
                err_x = state["x"] - x_now
                err_y = state["y"] - y_now
                pos_err = float(np.hypot(err_x, err_y))
                yaw_err = 0.0
                yaw_done = state["target_yaw"] is None
                if state["target_yaw"] is not None:
                    yaw_err = _normalize_angle_deg(state["target_yaw"] - _public_yaw_deg(info))
                    yaw_done = abs(yaw_err) < state["yaw_threshold"]
                if pos_err < state["pos_threshold"] and yaw_done:
                    final = get_base_info(env)
                    _finish_command(cmd, {"success": True, "pos": final["pos"], "yaw": _public_yaw_deg(final)})
                    return True
                if state["step"] >= state["max_steps"]:
                    final = get_base_info(env)
                    _finish_command(cmd, {"success": False, "pos": final["pos"], "yaw": _public_yaw_deg(final), "result": "导航超时"})
                    return True
                if pos_err >= state["pos_threshold"]:
                    angle_to_target = np.arctan2(err_y, err_x)
                    heading_err = np.arctan2(np.sin(angle_to_target - yaw_now), np.cos(angle_to_target - yaw_now))
                    Kp = state["kp"]
                    turn = np.clip(Kp * heading_err, -1.0, 1.0)
                    forward = np.clip(Kp * pos_err, -0.8, 0.8)
                    forward *= max(0.0, np.cos(heading_err))
                    move(env, Vx=forward, Vw=turn)
                else:
                    Vw = np.clip(2.0 * (yaw_err / 90.0), -1.0, 1.0)
                    move(env, Vx=0.0, Vw=Vw)
                state["step"] += 1
            return True

        if cmd_type == "move_duration":
            now = time.time()
            if now >= state["end_time"]:
                print(f"[move_duration] 时间到，发送停止信号", flush=True)
                set_base_velocity_cmd(Vx=0.0, Vw=0.0, timeout=1.0)
                stop_base(env)
                final = get_base_info(env)
                _finish_command(cmd, {"success": True, "pos": final["pos"], "yaw": _public_yaw_deg(final)})
                return True
            set_base_velocity_cmd(Vx=state["vx"], Vw=state["vw"], timeout=0.5)
            return True

        if cmd_type == "grasp":
            obj_name = state["obj_name"]
            link_name = _object_link(env, obj_name)
            if link_name not in env._link_name_to_idx:
                _finish_command(cmd, {"success": False, "result": f"物体 {obj_name} 不存在"})
                return True
            from tools.arm import grasp as do_grasp
            ok = do_grasp(env, link_name, snap_threshold=state.get("snap_threshold", 0.15))
            if ok:
                _finish_command(cmd, {"success": True, "result": f"成功抓取 {obj_name}"})
            else:
                _finish_command(cmd, {"success": False, "result": f"抓取 {obj_name} 失败"})
            return True

        if cmd_type == "place":
            obj_name = state["obj_name"]
            link_name = _object_link(env, obj_name)
            if link_name not in env._link_name_to_idx:
                _finish_command(cmd, {"success": False, "result": f"物体 {obj_name} 不存在"})
                return True
            from tools.arm import place as do_place
            ok = do_place(env, link_name, state["target_pos"], snap_threshold=state.get("snap_threshold", 0.15))
            if ok:
                _finish_command(cmd, {"success": True, "result": f"成功放置 {obj_name}"})
            else:
                _finish_command(cmd, {"success": False, "result": f"放置 {obj_name} 失败"})
            return True

        if cmd_type == "act_grasp":
            act_grasp_step(env, state)
            if state["done"]:
                obj_name = state["obj_name"]
                if state["success"]:
                    final = get_base_info(env)
                    _finish_command(cmd, {"success": True, "result": f"ACT 抓取 {obj_name} 成功", **_np_to_list(final)})
                else:
                    _finish_command(cmd, {"success": False, "result": f"ACT 抓取 {obj_name} 失败"})
            return True

        if cmd_type == "pi05_grasp":
            pi05_grasp_step(env, state)
            if state["done"]:
                obj_name = state["obj_name"]
                if state["success"]:
                    final = get_base_info(env)
                    _finish_command(cmd, {"success": True, "result": f"PI0.5 抓取 {obj_name} 成功", **_np_to_list(final)})
                else:
                    _finish_command(cmd, {"success": False, "result": f"PI0.5 抓取 {obj_name} 失败"})
            return True
    except Exception as e:
        _finish_command(cmd, {"success": False, "error": str(e)})
        return True
    return False


def process_commands(env):
    global _active_command

    if _step_active_command(env):
        return

    with _queue_lock:
        cmd = _cmd_queue.pop(0) if _cmd_queue else None
    if cmd is None:
        return

    for cmd in [cmd]:
        cmd_id = cmd["id"]
        cmd_type = cmd["type"]
        params = cmd["params"]
        try:
            with _env_lock:
                if cmd_type == "grasp":
                    mode = params.get("mode", "ik")
                    if mode == "act":
                        act_url = _act_config.get("url")
                        if not act_url:
                            result = {"success": False, "result": "ACT 推理服务未配置 (--act-url)"}
                            with _queue_lock:
                                _results[cmd_id] = result
                            cmd["event"].set()
                            return
                        state = act_grasp_init(
                            env, act_url, params["obj_name"],
                            max_steps=params.get("max_steps", _act_config.get("max_steps", 300)),
                            policy_fps=params.get("policy_fps", 30.0),
                        )
                        _active_command = {
                            **cmd,
                            "type": "act_grasp",
                            "state": state,
                        }
                        return
                    elif mode == "pi05":
                        _ensure_pi05_config()
                        pi05_url = _pi05_config.get("url")
                        if not pi05_url:
                            result = {"success": False, "result": "PI0.5 推理服务未配置 (config.yaml policy_services.pi05.url)"}
                            with _queue_lock:
                                _results[cmd_id] = result
                            cmd["event"].set()
                            return
                        task = params.get("task") or _pi05_config.get("task") or "pick up the object"
                        state = pi05_grasp_init(
                            env, pi05_url, params["obj_name"], task=task,
                            max_steps=params.get("max_steps", 1000),
                            policy_fps=params.get("policy_fps", 30.0),
                        )
                        _active_command = {**cmd, "type": "pi05_grasp", "state": state}
                        return
                    _active_command = {
                        **cmd,
                        "state": {
                            "obj_name": params["obj_name"],
                            "snap_threshold": params.get("snap_threshold", 0.15),
                        },
                    }
                    return
                elif cmd_type == "place":
                    _active_command = {
                        **cmd,
                        "state": {
                            "obj_name": params["obj_name"],
                            "target_pos": np.asarray(params["target_pos"], dtype=float),
                            "snap_threshold": params.get("snap_threshold", 0.15),
                        },
                    }
                    return
                elif cmd_type == "move_to":
                    reached = move_arm(env, **params)
                    info = get_arm_info(env)
                    result = {"reached": reached, "ee_pos": info["ee_pos"]}
                elif cmd_type == "open_gripper":
                    open_gripper(env, **params)
                    result = {"success": True}
                elif cmd_type == "close_gripper":
                    close_gripper(env, **params)
                    result = {"success": True}
                elif cmd_type == "nav":
                    _active_command = {
                        **cmd,
                        "state": {
                            "x": float(params["x"]),
                            "y": float(params["y"]),
                            "target_yaw": params.get("target_yaw"),
                            "kp": float(params.get("Kp", 1.5)),
                            "pos_threshold": float(params.get("pos_threshold", 0.20)),
                            "yaw_threshold": float(params.get("yaw_threshold", 10.0)),
                            "max_steps": int(params.get("max_steps", 6000)),
                            "step": 0,
                        },
                    }
                    return
                elif cmd_type == "move_duration":
                    _active_command = {
                        **cmd,
                        "state": {
                            "vx": float(params.get("vx", 0.0)),
                            "vw": float(params.get("vw", 0.0)),
                            "end_time": time.time() + float(params["duration"]),
                        },
                    }
                    return
                elif cmd_type == "cmd_vel":
                    info = move(env, **params)
                    result = {"success": True, "pos": info["pos"], "yaw": _public_yaw_deg(info)}
                elif cmd_type == "screenshot":
                    from io import BytesIO
                    import base64
                    defaults = _screenshot_defaults()
                    cam = params.get("camera_name") or defaults.get("default_camera", "overhead_cam")
                    w = params.get("width") or defaults.get("width", 640)
                    h = params.get("height") or defaults.get("height", 480)
                    quality = int(defaults.get("jpeg_quality", 80))
                    cam_id = _find_camera_id(env.model, cam)
                    rgb = env.render_frame(cam_id=cam_id, width=w, height=h)
                    pil_img = PILImage.fromarray(rgb)
                    buf = BytesIO()
                    pil_img.save(buf, format="JPEG", quality=quality)
                    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                    result = {
                        "success": True,
                        "image": img_b64,
                        "camera": cam,
                        "width": int(w),
                        "height": int(h),
                    }
                elif cmd_type == "camera_preview":
                    from io import BytesIO
                    import base64
                    preview = _preview_config()
                    camera_name = params.get("camera_name")
                    camera_names = [camera_name] if camera_name else preview.get("cameras", ["overhead_cam"])
                    w = int(params.get("width") or preview.get("width", 640))
                    h = int(params.get("height") or preview.get("height", 480))
                    quality = int(preview.get("jpeg_quality", 80))
                    panels = _render_camera_preview(env, camera_names, w, h)
                    if len(panels) == 1:
                        image = panels[0]
                    else:
                        while len(panels) < 4:
                            panels.append(PILImage.new("RGB", (w, h), (0, 0, 0)))
                        image = PILImage.new("RGB", (w * 2, h * 2))
                        for idx, panel in enumerate(panels[:4]):
                            image.paste(panel, ((idx % 2) * w, (idx // 2) * h))
                    buf = BytesIO()
                    image.save(buf, format="JPEG", quality=quality)
                    result = {
                        "success": True,
                        "image": base64.b64encode(buf.getvalue()).decode("utf-8"),
                        "camera": camera_name,
                    }
                else:
                    result = {"error": f"未知命令: {cmd_type}"}
        except Exception as e:
            result = {"error": str(e)}

        if cmd_type in ("nav", "cmd_vel"):
            try:
                _read_base_info(env)
            except Exception:
                pass

        with _queue_lock:
            _results[cmd_id] = result
        cmd["event"].set()


# ============================================================
# 状态查询
# ============================================================

@app.route("/state_qpos", methods=["GET"])
def api_state_qpos():
    env = _get_env()
    if env is None:
        return jsonify({"error": "not ready"}), 503
    with _env_lock:
        env.forward_kinematic()
        poses = env.get_link_poses()
        link_data = {}
        for i, name in enumerate(env.model.link_names):
            if name and i < poses.shape[0]:
                link_data[name] = poses[i].tolist()
    return jsonify({"links": link_data})


@app.route("/status", methods=["GET"])
def api_status():
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503
    with _env_lock:
        state = get_arm_info(env)
    return jsonify(_np_to_list(state))


@app.route("/base_status", methods=["GET"])
def api_base_status():
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503
    if not _env_lock.acquire(blocking=False):
        if _base_status_cache:
            return jsonify(_base_status_cache)
        return jsonify({"error": "仿真器忙"}), 503
    try:
        info = _read_base_info(env)
    finally:
        _env_lock.release()
    return jsonify(_np_to_list(info))


@app.route("/objects", methods=["GET"])
def api_objects():
    env = _get_env()
    if env is None:
        return jsonify({"error": "not ready"}), 503
    with _env_lock:
        result = _scene_objects(env)
    return jsonify(result)


@app.route("/fixtures", methods=["GET"])
def api_fixtures():
    env = _get_env()
    if env is None:
        return jsonify({"error": "not ready"}), 503
    return jsonify(_np_to_list(_scene_fixtures(env)))


@app.route("/scene", methods=["GET"])
def api_scene():
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503

    with _env_lock:
        objects = _scene_objects(env)
        fixtures = _scene_fixtures(env)

        arm_info = get_arm_info(env)
        base_info = get_base_info(env)
        robot = {
            "base_pos": base_info["pos"],
            "ee_pos": arm_info["ee_pos"],
            "yaw": _public_yaw_deg(base_info),
        }

    return jsonify(_np_to_list({
        "objects": objects,
        "fixtures": fixtures,
        "robot": robot,
    }))


@app.route("/scene_state", methods=["GET"])
def api_scene_state():
    return jsonify({"error": "scene_memory not available in MotrixSim backend"}), 503


# ============================================================
# 视频录制
# ============================================================

@app.route("/record/start", methods=["POST"])
def api_record_start():
    if _recording["active"]:
        return jsonify({"success": False, "message": "已在录制中"})

    data = request.json or {}
    _recording["fps"] = data.get("fps", 1)
    _recording["interval"] = 1.0 / _recording["fps"]
    _recording["frames"] = []
    _recording["last_capture"] = 0
    _recording["active"] = True

    total_w = _recording["view_width"]
    total_h = _recording["view_height"]
    print(f"[record] 开始录制 ({_recording['fps']}fps, {total_w}x{total_h})", flush=True)
    return jsonify({"success": True, "message": "录制已开始"})


@app.route("/record/stop", methods=["POST"])
def api_record_stop():
    if not _recording["active"]:
        return jsonify({"success": False, "message": "未在录制中"})

    _recording["active"] = False
    frames = list(_recording["frames"])
    _recording["frames"] = []

    if not frames:
        return jsonify({"success": False, "message": "未采集到画面"})

    os.makedirs(_video_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"recording_{ts}.mp4"
    filepath = os.path.join(_video_dir, filename)

    h, w = frames[0].shape[:2]
    fps = _recording["fps"]

    try:
        import imageio
        writer = imageio.get_writer(filepath, fps=fps)
        for f in frames:
            writer.append_data(f)
        writer.close()
    except ImportError:
        import cv2
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(filepath, fourcc, fps, (w, h))
        for f in frames:
            writer.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
        writer.release()

    print(f"[record] 录制完成: {filename} ({len(frames)} 帧, {len(frames)/fps:.1f}s)", flush=True)
    return jsonify({
        "success": True,
        "message": f"已保存 {len(frames)} 帧",
        "filename": filename,
        "frames": len(frames),
        "duration": round(len(frames) / fps, 1),
    })


@app.route("/record/download/<filename>", methods=["GET"])
def api_record_download(filename):
    filepath = os.path.join(_video_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(filepath, mimetype="video/mp4", as_attachment=True)


@app.route("/record/status", methods=["GET"])
def api_record_status():
    return jsonify({
        "active": _recording["active"],
        "frames": len(_recording["frames"]),
        "fps": _recording["fps"],
    })


# ============================================================
# 控制命令
# ============================================================

@app.route("/grasp", methods=["POST"])
def api_grasp():
    data = request.json or {}
    obj_name = data.get("obj_name")
    if not obj_name:
        return jsonify({"error": "缺少 obj_name 参数"}), 400
    mode = data.get("mode", "ik")
    params = {
        "obj_name": obj_name,
        "mode": mode,
    }
    if mode == "ik":
        params["snap_threshold"] = data.get("snap_threshold", 0.15)
    elif mode == "act":
        # max_steps default flows from config (policy_services.act.max_steps) at dispatch time
        if data.get("max_steps") is not None:
            params["max_steps"] = data["max_steps"]
    elif mode == "pi05":
        if data.get("task") is not None:
            params["task"] = data["task"]
        if data.get("max_steps") is not None:
            params["max_steps"] = data["max_steps"]
    return jsonify(submit_command("grasp", params))


@app.route("/place", methods=["POST"])
def api_place():
    data = request.json or {}
    obj_name = data.get("obj_name")
    target = data.get("target")
    if not obj_name:
        return jsonify({"error": "缺少 obj_name 参数"}), 400
    if target is None:
        return jsonify({"error": "缺少 target 参数"}), 400
    params = {
        "obj_name": obj_name,
        "target_pos": target,
    }
    if data.get("snap_threshold") is not None:
        params["snap_threshold"] = data["snap_threshold"]
    return jsonify(submit_command("place", params))


@app.route("/move_to", methods=["POST"])
def api_move_to():
    data = request.json or {}
    target = data.get("target")
    if target is None:
        return jsonify({"error": "缺少 target 参数"}), 400
    params = {
        "target_pos": target,
        "max_steps": data.get("max_steps", 1000),
        "pos_threshold": data.get("pos_threshold", 0.03),
    }
    return jsonify(submit_command("move_to", params))


@app.route("/move_duration", methods=["POST"])
def api_move_duration():
    data = request.json or {}
    vx = float(data.get("vx", 0.0))
    vw = float(data.get("vw", 0.0))
    duration = float(data.get("duration", 1.0))

    if duration <= 0:
        return jsonify({"error": "duration 必须大于 0"}), 400

    params = {"vx": vx, "vw": vw, "duration": duration}
    return jsonify(submit_command("move_duration", params))


@app.route("/nav", methods=["POST"])
def api_nav():
    data = request.json or {}
    x = data.get("x")
    y = data.get("y")
    if x is None or y is None:
        return jsonify({"error": "缺少 x 或 y 参数"}), 400

    params = {"x": x, "y": y}
    if data.get("target_yaw") is not None:
        params["target_yaw"] = data["target_yaw"]
    elif data.get("w") is not None:
        params["target_yaw"] = data["w"]
    return jsonify(submit_command("nav", params))


@app.route("/cmd_vel", methods=["POST"])
def api_cmd_vel():
    data = request.json or {}
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503
    set_base_velocity_cmd(
        Vx=data.get("vx", 0.0),
        Vy=data.get("vy", 0.0),
        Vw=data.get("vw", 0.0),
        timeout=data.get("duration", 0.25),
    )
    with _env_lock:
        info = get_base_info(env)
    return jsonify({"success": True, "pos": info["pos"], "yaw": _public_yaw_deg(info)})


@app.route("/open_gripper", methods=["POST"])
def api_open_gripper():
    return jsonify(submit_command("open_gripper", {}))


@app.route("/close_gripper", methods=["POST"])
def api_close_gripper():
    return jsonify(submit_command("close_gripper", {}))


# ============================================================
# 截图
# ============================================================

@app.route("/screenshot", methods=["POST"])
def api_screenshot():
    data = request.json or {}
    defaults = _screenshot_defaults()
    params = {
        "camera_name": data.get("camera_name") or defaults.get("default_camera", "overhead_cam"),
        "width": data.get("width") or defaults.get("width", 640),
        "height": data.get("height") or defaults.get("height", 480),
    }
    return jsonify(submit_command("screenshot", params))


@app.route("/camera/status", methods=["GET"])
def api_camera_status():
    return jsonify({
        "success": True,
        "config": _camera_config(),
    })


@app.route("/camera/latest", methods=["GET"])
def api_camera_latest():
    result = submit_command("camera_preview", {
        "camera_name": request.args.get("camera"),
        "width": request.args.get("width"),
        "height": request.args.get("height"),
    })
    if not result.get("success"):
        return jsonify(result), 500
    import base64
    return Response(base64.b64decode(result["image"]), mimetype="image/jpeg")


# ============================================================
# 启动函数
# ============================================================

def start_server(env, port=5001):
    _env_holder["env"] = env

    def run():
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    print(f"[service] Flask API 已启动: http://localhost:{port}")
    return thread
