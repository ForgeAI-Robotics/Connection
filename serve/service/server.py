"""
server.py - Flask API 服务
通过命令队列与仿真器主循环通信，避免多线程同时调用 env.step()
"""

import os
import math
import time
import threading
import numpy as np
import mujoco
import yaml
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS

from tools.arm import (
    get_arm_info, get_obj_pos,
    move_arm, grasp, place,
    open_gripper, close_gripper, is_grasped,
)
from tools.move import get_base_info, nav, move, follow_path
from scene.scene_memory import (
    coords_to_waypoint,
    get_all_locations,
    get_belief_by_object,
    load_state as _load_belief_state,
    move_object,
    reset_belief_unknown,
    reset_to_initial,
)

import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'slaver', 'robot')))
try:
    from path_planner import is_line_clear as _astar_line_clear, plan_path as _astar_plan
    _ASTAR_AVAILABLE = True
except Exception as _e:
    _ASTAR_AVAILABLE = False
    print(f"[server] A* 路径规划不可用: {_e}", file=_sys.stderr)

app = Flask(__name__)
CORS(app)

_env_holder = {"env": None}
_cmd_queue = []      # 命令队列
_results = {}        # 命令结果缓存
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

# 录制状态
_recording = {
    "active": False,
    "frames": [],
    "fps": 1,
    "view_height": 240,
    "view_width": 320,
    "last_capture": 0,
    "interval": 1.0,
}

RECORD_CAMERAS = [
    ("overhead_cam",             "Top",   True),
    ("robot0_frontview",         "Front", True),
    ("robot0_eye_in_hand",       "Wrist", True),
    ("robot0_agentview_center",  "Agent", True),
]

_video_dir = os.path.join(os.path.dirname(__file__), "..", "videos")
_camera_config_cache = None


def _camera_config():
    global _camera_config_cache
    if _camera_config_cache is None:
        scene_dir = "scene"
        env = _get_env()
        if env is not None:
            scene_dir = getattr(env, "scene_dir", None) or getattr(env, "_scene_dir", "scene")
        path = os.path.join(os.path.abspath(scene_dir), "config", "camera.yaml")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _camera_config_cache = yaml.safe_load(f) or {}
        else:
            _camera_config_cache = {}
    return _camera_config_cache


_nongraspable_cache = None


def _nongraspable_objects():
    """从 objects.yaml 读出 graspable:false 的物体名集合(如 plate)。grasp 命令据此拒绝抓取,
    避免 planner 误规划'抓盘子'把不可抓的容器抓起来搞乱状态。"""
    global _nongraspable_cache
    if _nongraspable_cache is None:
        _nongraspable_cache = set()
        scene_dir = "scene"
        env = _get_env()
        if env is not None:
            scene_dir = getattr(env, "scene_dir", None) or getattr(env, "_scene_dir", "scene")
        path = os.path.join(os.path.abspath(scene_dir), "config", "objects.yaml")
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for o in cfg.get("objects", []):
                if o.get("graspable") is False:
                    _nongraspable_cache.add(o.get("name"))
        except Exception as e:
            print(f"[grasp] 读 objects.yaml 判不可抓失败(非致命): {e}", flush=True)
    return _nongraspable_cache


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
    from PIL import Image, ImageDraw
    panels = []
    for cam_name, label, flip in RECORD_CAMERAS:
        try:
            img = env.sim.render(view_w, view_h, camera_name=cam_name)
            if img is None:
                img = np.zeros((view_h, view_w, 3), dtype=np.uint8)
            elif flip:
                img = np.flipud(img)
        except Exception:
            img = np.zeros((view_h, view_w, 3), dtype=np.uint8)
        pil_img = Image.fromarray(img)
        draw = ImageDraw.Draw(pil_img)
        draw.rectangle([0, 0, len(label) * 8 + 12, 18], fill=(0, 0, 0))
        draw.text((5, 2), label, fill=(255, 255, 255))
        panels.append(np.array(pil_img))
    top = np.hstack([panels[0], panels[1]])
    bottom = np.hstack([panels[2], panels[3]])
    return np.vstack([top, bottom])


def _render_camera_preview(env, camera_names, width, height):
    from PIL import Image, ImageDraw
    labels = _camera_labels()
    panels = []
    for name in camera_names:
        try:
            img = env.sim.render(width, height, camera_name=name)
            img = np.flipud(img)  # robosuite sim.render 是上下翻的(OpenGL 约定),翻正
        except Exception:
            img = np.zeros((height, width, 3), dtype=np.uint8)
        panel = Image.fromarray(np.ascontiguousarray(img))
        label = labels.get(name, name)
        draw = ImageDraw.Draw(panel)
        draw.rectangle([0, 0, len(label) * 8 + 12, 20], fill=(0, 0, 0))
        draw.text((5, 3), label, fill=(255, 255, 255))
        panels.append(panel)
    return panels


# 实时四宫格已移除(见 start_server):命令改跑纯物理速度,时间线只在子任务边界按需渲染。


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


def has_active_base_command():
    """当前是否有正在执行的底盘命令 (nav/cmd_vel/move_duration)"""
    return _active_command is not None and _active_command.get("type") in ("nav", "cmd_vel", "move_duration")


def set_base_velocity(Vx=0.0, Vy=0.0, Vw=0.0, timeout=0.25):
    """Store the latest velocity command for the main simulation loop."""
    print(f"[set_base_velocity] Vx={Vx}, Vw={Vw}, timeout={timeout}", flush=True)
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
    """返回当前底座速度命令 [forward, turn]，不包含手臂 ctrl。

    cmd_vel 过期后返回 None，主循环不写 ctrl，保留 viewer 手动控制值。
    过期瞬间返回一次 [0, 0] 以确保机器人停止。

    直接设置底盘 freejoint 速度（bypass 轮子物理），确保 cmd_vel 响应速度
    匹配 Nav2 期望（而非通过弱电机慢慢加速）。
    """
    with _queue_lock:
        if time.time() > _base_cmd["expires_at"]:
            if _base_cmd["was_active"]:
                _base_cmd["was_active"] = False
                print("[get_base_action] cmd_vel 过期，返回停止信号 [0,0]", flush=True)
                return np.zeros(2)
            return None
        _base_cmd["was_active"] = True
        action = np.zeros(2)
        action[0] = np.clip(_base_cmd["Vx"], -1.0, 1.0)   # forward
        action[1] = -np.clip(_base_cmd["Vw"], -1.0, 1.0)   # turn
        return action


def _get_env():
    return _env_holder["env"]


def _read_base_info(env):
    info = get_base_info(env)
    _base_status_cache.clear()
    _base_status_cache.update(_np_to_list(info))
    return info


def _is_body_descendant(model, body_id, ancestor_id):
    current = int(body_id)
    ancestor_id = int(ancestor_id)
    while current > 0:
        if current == ancestor_id:
            return True
        current = int(model.body_parentid[current])
    return False


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


# ============================================================
# Phase 1 持续世界 (per-home) —— 对齐 serve_alfworld 的 per-home 学习基础设施
# MuJoCo 全可观测：drift 恢复靠 grasp 重新查 /objects 拿新坐标，无需遍历搜索。
# 这里补齐 reset_home / set_task / inject_move / success(几何裁判) / shadow_state，
# 让同一套 bench(run_home_llm / eval_drift) 能直接跑 MuJoCo 后端。
# ============================================================
import re as _re

_home = {"persistent": False, "task": "", "steps": 0}
_container_contents = {}  # container → [objs] 藏进去的物体;open_container 时抬出来露到台面


def _learning_mode():
    """学习测试台开关 = slaver/config.yaml 的 perception.use_realtime_coords 取反。

    True  → 部分可观测:reset_home 起空 belief、inject_move 不更新 belief(漂移可检测)。
    False → 全可观测:belief 直接 = 真值(原行为不变)。
    serve 与 slaver 是两个进程,但同仓库,这里直接读那份 yaml 保证单一真相源。
    """
    try:
        import yaml as _yaml
        cfg_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "slaver", "config.yaml")
        )
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _yaml.safe_load(f) or {}
        return not bool(cfg.get("perception", {}).get("use_realtime_coords", True))
    except Exception:
        return False

# 任务里的家具基名(ALFWorld 词表) → RoboCasa 实际 fixture 名包含的片段。
# 注意:RoboCasa 命名是 cab_main_main_group / shelves_main_group / stovetop_main_group,
# 所以 cabinet→"cab"、shelf→"shelves"、stove→"stove"(stovetop 含 stove)。
_FIXTURE_ALIAS = {
    "countertop": "counter", "counter": "counter",
    "sinkbasin": "sink", "sink": "sink",
    "stoveburner": "stove", "stove": "stove", "stovetop": "stove",
    "diningtable": "counter", "table": "counter",
    "island": "island",
    "shelf": "shelves", "shelves": "shelves",
    "cabinet": "cab", "drawer": "cab", "cab": "cab",
    "fridge": "fridge", "microwave": "microwave",
}


def _count_step():
    """持续模式下记一步(对齐 ALFWorld 的步数指标:每个高层动作算一步)。"""
    if _home["persistent"]:
        _home["steps"] += 1


def _match_object(base):
    env = _get_env()
    base = (base or "").lower()
    for o in env.objects:
        if base and base in o.lower():
            return o
    return None


def _fixtures_for_base(base):
    """目标家具基名 → 所有匹配的 MuJoCo fixture 实例名(可能多个 counter 实例)。

    片段如 "cab" 会同时命中 cab_main_main_group 和 island_cab_right_*;按"名字以片段
    开头"优先排序,让 inject 取 fixes[0] 时拿到正主(cab_main)而非 island 上的橱柜面板。
    """
    env = _get_env()
    target = _FIXTURE_ALIAS.get((base or "").lower(), (base or "").lower())
    if not target:
        return []
    matches = [f for f in env.fixtures if target in f.lower() or f.lower() in target]
    matches.sort(key=lambda f: (not f.lower().startswith(target), len(f)))
    return matches


def _parse_put_task(task):
    """'put mug in countertop' / 'put the mug on counter' → ('mug', 'countertop')。"""
    m = _re.search(
        r'put\s+(?:the\s+|a\s+|an\s+)?(\w+).*?\b(?:in|into|on|onto|to)\b\s+(?:the\s+)?(\w+)',
        (task or "").lower(),
    )
    if m:
        return m.group(1), m.group(2)
    return None, None


def _fixture_top_center(fix_name):
    """目标 fixture 顶面中心坐标(放置/注入落点)。"""
    fx = _get_env().fixtures[fix_name]
    pos = np.asarray(fx.pos, dtype=float)
    size = np.asarray(fx.size, dtype=float)
    return [float(pos[0]), float(pos[1]), float(pos[2] + size[2] / 2.0 + 0.05)]


def _on_fixture(opos, fix_name, margin=0.20):
    """几何裁判:物体 XY 落在 fixture footprint(含 margin)内、且未掉到地面。"""
    fx = _get_env().fixtures[fix_name]
    cx, cy, cz = [float(v) for v in fx.pos]
    sx, sy, sz = [float(v) for v in fx.size]
    return (
        abs(opos[0] - cx) <= sx / 2.0 + margin and
        abs(opos[1] - cy) <= sy / 2.0 + margin and
        opos[2] >= cz - 0.20  # 没掉到地面
    )


def _waypoint_serves_fixture(opos, fix_base):
    """Q2 符号裁判:物体最近工作点(coords_to_waypoint,与 belief/发现同一映射)是否服务目标家具。

    与几何裁判 OR 使用:几何=物理真值(footprint),这个=工作点符号判据,
    让 MuJoCo 裁判与 ALFWorld 的"物体在 shelf"符号判定同构(对齐 Q2)。
    """
    try:
        wp_name = coords_to_waypoint([float(opos[0]), float(opos[1])])
        target = _FIXTURE_ALIAS.get((fix_base or "").lower(), (fix_base or "").lower())
        info = (_load_belief_state().get("locations") or {}).get(wp_name) or {}
        fx = (info.get("fixture") or "").lower()
        return bool(fx) and (target in fx or fx in target)
    except Exception:
        return False


# 简餐/长任务食材集(gather these onto the plate)
_GATHER_INGREDIENTS = ("bread", "cheese", "ketchup")
# 食材算"凑到盘子上"的水平半径:盘子小 + 圆瓶/法棍放上去会物理滚动,取 0.6m 捕捉盘子及紧邻小簇。
# (食材起始离盘 0.7~1.8m,凑齐后进到 0.6m 内,足以区分"已凑齐 vs 没凑";要更干净可改用带壁的碗做容器。)
_GATHER_RADIUS = 0.6


def _is_gather_task(task):
    """任务是否是'把多样食材凑到盘子上'的长任务(靠关键词判断)。"""
    t = (task or "").lower()
    hit_plate = ("plate" in t or "盘" in t)
    hit_gather = any(k in t for k in ("gather", "凑", "食材", "简餐", "ingredient", "hot dog", "热狗")) \
        or sum(1 for g in _GATHER_INGREDIENTS if g in t) >= 2
    return hit_plate and hit_gather


def _judge_gather_on_plate(task=None):
    """组合裁判:bread/cheese/ketchup 都落在 plate 水平 _GATHER_RADIUS 内 = 凑齐成功。

    长任务(gather ingredients onto the plate)的完成判据。食材在盘子上会有物理滚动,
    用水平距离阈值捕捉'都凑到盘子这一小片'即可,不苛求精确堆叠。
    """
    env = _get_env()
    if env is None or "plate" not in env.obj_body_id:
        return False
    ings = [g for g in _GATHER_INGREDIENTS if g in env.obj_body_id]
    if not ings:
        return False
    try:
        with _env_lock:
            ppos = env.get_object_pos("plate")
            for n in ings:
                q = env.get_object_pos(n)
                if float(np.hypot(q[0] - ppos[0], q[1] - ppos[1])) > _GATHER_RADIUS:
                    return False
                if float(q[2]) < float(ppos[2]) - 0.15:   # 掉到盘子/台面以下不算
                    return False
        return True
    except Exception:
        return False


def _judge_task(task):
    """完成裁判,替代 ALFWorld 的 oracle won / shadow_judge。

    先看是不是'凑食材到盘子'的长任务 → 组合裁判;否则走原'put X on Y'单物体裁判:
    won = 几何裁判(物体落在目标家具 footprint,物理真值) OR 工作点符号裁判(Q2)。
    """
    if _is_gather_task(task):
        return _judge_gather_on_plate(task)
    obj_base, fix_base = _parse_put_task(task)
    if not obj_base or not fix_base:
        return False
    obj = _match_object(obj_base)
    fixes = _fixtures_for_base(fix_base)
    if not obj or not fixes:
        return False
    try:
        with _env_lock:
            opos = _get_env().get_object_pos(obj)
        if any(_on_fixture(opos, f) for f in fixes):
            return True
        return _waypoint_serves_fixture(opos, fix_base)
    except Exception:
        return False


def _shadow_at():
    """belief 层:每个物体当前最近的家具名(held 优先),供 bench 调试/裁判对照。"""
    env = _get_env()
    held = getattr(env, "grasped_object", None)
    at = {}
    with _env_lock:
        for o in env.objects:
            if o == held:
                at[o] = "held"
                continue
            try:
                opos = env.get_object_pos(o)
            except Exception:
                continue
            best = min(
                env.fixtures,
                key=lambda f: (opos[0] - env.fixtures[f].pos[0]) ** 2
                + (opos[1] - env.fixtures[f].pos[1]) ** 2,
                default=None,
            )
            at[o] = best or "unknown"
    return at


def submit_command(cmd_type, params):
    """
    提交命令到队列，等待执行结果
    Returns: dict 结果
    """
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
    event.wait(timeout=300)  # 最多等 300 秒(MuJoCo nav 单步慢,发现机制串多个 nav,留足余量)
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


def _step_active_command(env):
    # PandaOmron/robosuite:nav/grasp/place 改为在 process_commands 里同步调真工具执行,
    # 不再用分帧 active-command(那是自定义 freejoint env 的机制)。这里恒空转。
    return False
    cmd = _active_command
    if cmd is None:
        return False
    try:
        cmd_type = cmd["type"]
        state = cmd["state"]
        if cmd_type == "nav":
            path = state.get("path")

            # --- A* 路径跟踪阶段 ---
            if path and state["path_index"] < len(path) - 1:
                for _ in range(NAV_SUBSTEPS):
                    info = get_base_info(env)
                    x_now, y_now = info["pos"][0], info["pos"][1]

                    # 跳过已到达的航点
                    while state["path_index"] < len(path) - 1:
                        p = path[state["path_index"]]
                        if np.hypot(p["x"] - x_now, p["y"] - y_now) <= state["waypoint_threshold"]:
                            state["path_index"] += 1
                        else:
                            break

                    if state["path_index"] >= len(path) - 1:
                        break  # 路径跟踪完成，转入直线 PD 精确对准

                    p = path[state["path_index"]]
                    err_x = p["x"] - x_now
                    err_y = p["y"] - y_now
                    dist = float(np.hypot(err_x, err_y))
                    yaw_now = info["yaw_rad"]
                    angle_to_next = np.arctan2(err_y, err_x)
                    heading_err = np.arctan2(np.sin(angle_to_next - yaw_now), np.cos(angle_to_next - yaw_now))
                    Kp = state["kp"]
                    turn = np.clip(Kp * heading_err, -1.0, 1.0)
                    forward = np.clip(1.4 * dist, 0.0, 0.45)
                    forward *= max(0.0, np.cos(heading_err))
                    move(env, Vx=forward, Vw=turn)
                    state["step"] += 1

                    if state["step"] >= state["max_steps"]:
                        final = get_base_info(env)
                        _finish_command(cmd, {"success": False, "pos": final["pos"], "yaw": final["yaw_deg"], "result": "导航超时"})
                        return True

                if state["path_index"] < len(path) - 1:
                    return True  # 仍在跟踪 A* 路径

            # --- 直线 PD 精确对准阶段（原有逻辑）---
            for _ in range(NAV_SUBSTEPS):
                info = get_base_info(env)
                x_now, y_now = info["pos"][0], info["pos"][1]
                yaw_now = info["yaw_rad"]
                err_x = state["x"] - x_now
                err_y = state["y"] - y_now
                pos_err = float(np.hypot(err_x, err_y))
                yaw_err = 0.0
                yaw_done = state["target_yaw"] is None
                if state["target_yaw"] is not None:
                    yaw_err = _normalize_angle_deg(state["target_yaw"] - info["yaw_deg"])
                    yaw_done = abs(yaw_err) < state["yaw_threshold"]
                if pos_err < state["pos_threshold"] and yaw_done:
                    final = get_base_info(env)
                    print(f"[nav诊断] ✓到达 step={state['step']}/{state['max_steps']} "
                          f"pos=[{final['pos'][0]:.2f},{final['pos'][1]:.2f}] 目标=[{state['x']:.2f},{state['y']:.2f}]",
                          file=_sys.stderr, flush=True)
                    _finish_command(cmd, {"success": True, "pos": final["pos"], "yaw": final["yaw_deg"]})
                    return True
                if state["step"] >= state["max_steps"]:
                    final = get_base_info(env)
                    print(f"[nav诊断] ✗超时(跑满max_steps) step={state['step']} "
                          f"pos=[{final['pos'][0]:.2f},{final['pos'][1]:.2f}] 目标=[{state['x']:.2f},{state['y']:.2f}] "
                          f"残差={pos_err:.2f}m —— 说明到不了该点(卡住/绕障失败)", file=_sys.stderr, flush=True)
                    _finish_command(cmd, {"success": False, "pos": final["pos"], "yaw": final["yaw_deg"], "result": "导航超时"})
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
                # timeout 需要 >0，让主循环 get_base_action 有机会设置 was_active=True
                # 然后返回 [0,0] 停止信号；过期后自动变 None 不再干预
                set_base_velocity(Vx=0.0, Vw=0.0, timeout=1.0)
                stop_base(env)
                final = get_base_info(env)
                _finish_command(cmd, {"success": True, "pos": final["pos"], "yaw": final["yaw_deg"]})
                return True
            move(env, Vx=state["vx"], Vw=state["vw"])
            return True

        if cmd_type == "grasp":
            obj_name = state["obj_name"]
            if obj_name not in env.obj_body_id:
                _finish_command(cmd, {"success": False, "result": f"物体 {obj_name} 不存在"})
                return True
            if state["phase"] == "approach":
                target = env.get_object_pos(obj_name).copy()
                target[2] += 0.04
                if _move_virtual_ee(env, target):
                    env.grasped_object = obj_name
                    state["phase"] = "lift"
                    state["lift_target"] = target + np.array([0.0, 0.0, 0.20])
                return True
            if state["phase"] == "lift":
                if _move_virtual_ee(env, state["lift_target"]):
                    try:
                        move_object(obj_name, "robot_hand")
                    except Exception as e:
                        print(f"[SceneMemory] 更新失败: {e}", flush=True)
                    _finish_command(cmd, {"success": True, "result": f"成功抓取 {obj_name}"})
                return True

        if cmd_type == "place":
            obj_name = state["obj_name"]
            if obj_name not in env.obj_body_id:
                _finish_command(cmd, {"success": False, "result": f"物体 {obj_name} 不存在"})
                return True
            if state["phase"] == "move":
                # 严格:必须真持有该物体才能放置。原先这里强行 set grasped_object=obj_name,
                # 会在抓取失败时"假成功"地把物体瞬移到目标 → 污染裁判(没抓到也判 won)。
                # 现在没真抓到就直接放置失败,让 won 反映真实物理结果。
                if env.grasped_object != obj_name:
                    _finish_command(cmd, {
                        "success": False,
                        "result": f"未持有 {obj_name}(当前持有: {env.grasped_object}),放置失败:请先成功抓取",
                    })
                    return True
                if _move_virtual_ee(env, state["target_pos"]):
                    env.set_object_pos(obj_name, state["target_pos"])
                    print(f"[place后端] {obj_name} 瞬移到 target_pos={np.asarray(state['target_pos']).tolist()}; "
                          f"落点实读={np.asarray(env.get_object_pos(obj_name)).tolist()}", file=_sys.stderr, flush=True)
                    env.grasped_object = None
                    state["phase"] = "retreat"
                    state["retreat_target"] = state["target_pos"] + np.array([0.0, 0.0, 0.20])
                return True
            if state["phase"] == "retreat":
                if _move_virtual_ee(env, state["retreat_target"]):
                    state["phase"] = "settle"
                    state["settle_frames"] = 0
                return True
            if state["phase"] == "settle":
                # 等物体物理静止(线速度 < 阈值)再上报成功。place 把物体瞬移到落点后,
                # 主循环继续 env.step():薄/高 fixture 上物体仍会下落或微滑,若此刻就 finish,
                # slaver 上报 all_done → bench 在物体未稳定时读 won → 假阴性(stovetop 案例)。
                # 最多等 300 帧兜底,避免永不静止时卡死。
                joint_id = env.obj_joint_id[obj_name]
                dadr = env.raw_model.jnt_dofadr[joint_id]
                lin_vel = float(np.linalg.norm(env.raw_data.qvel[dadr:dadr + 3]))
                state["settle_frames"] = state.get("settle_frames", 0) + 1
                # 要连续 10 帧静止才算真稳:物体瞬移到落点时速度被清零(=0),若只看单帧
                # 会在它还没开始下落时就误判稳定。连续静止排除"下落初期瞬时低速"。300 帧兜底。
                state["still_frames"] = (state.get("still_frames", 0) + 1) if lin_vel < 0.02 else 0
                if state["still_frames"] >= 10 or state["settle_frames"] >= 300:
                    final_pos = np.asarray(env.get_object_pos(obj_name))
                    try:
                        # belief 写物体**真实最终落点**的工作点(不是目标点),反映物理结果
                        move_object(obj_name, coords_to_waypoint(final_pos.tolist()))
                    except Exception as e:
                        print(f"[SceneMemory] 更新失败: {e}", flush=True)
                    print(f"[place后端] {obj_name} 已静止(vel={lin_vel:.4f}, "
                          f"frames={state['settle_frames']}); 最终落点="
                          f"{[round(float(x), 3) for x in final_pos]}", file=_sys.stderr, flush=True)
                    _finish_command(cmd, {"success": True, "result": f"成功放置 {obj_name}"})
                return True
    except Exception as e:
        _finish_command(cmd, {"success": False, "error": str(e)})
        return True
    return False


def _move_virtual_ee(env, target):
    target = np.asarray(target, dtype=float)
    delta = target - env.virtual_ee_pos
    dist = float(np.linalg.norm(delta))
    if dist <= ARM_STEP:
        env.virtual_ee_pos = target.copy()
        if env.grasped_object:
            env.set_object_pos(env.grasped_object, env.virtual_ee_pos)
        return True
    env.virtual_ee_pos = env.virtual_ee_pos + delta / dist * ARM_STEP
    if env.grasped_object:
        env.set_object_pos(env.grasped_object, env.virtual_ee_pos)
    return False


# 底盘在目标这个距离内就只原地转向、不再重导航。
# 真臂 reach ~0.8m + 吸附 0.65m ⇒ 底盘离目标 ~1.4m 内伸臂即可够到;取 1.15m 留余量。
# navigate_to_target / discover 通常已把底盘停在 ~1m 处 → 抓放不再多走一段冗余导航(省 ~20s)。
_ARM_STAND_REACH = 1.15


def _nav_within_reach(env, tx, ty, reach=0.6):
    """确保底座在目标 xy 的臂可达范围内并朝向它,让真臂/吸附够得到。

    - 底盘已在 _ARM_STAND_REACH 内(常见:上一步导航已到位):只原地转向目标,不重导航 → 快。
    - 太远才真的挪近:退到目标 reach 米外的接近点(沿目标→底盘方向,落在机器人这侧地面,可站立)。
    """
    info = get_base_info(env)
    bx, by = float(info["pos"][0]), float(info["pos"][1])
    dist = float(np.hypot(tx - bx, ty - by))
    if dist <= _ARM_STAND_REACH:
        yaw = math.degrees(math.atan2(ty - by, tx - bx))
        nav(env, bx, by, target_yaw=yaw, pos_threshold=0.2, max_steps=120)
        return True
    ax = tx + (bx - tx) / dist * reach
    ay = ty + (by - ty) / dist * reach
    yaw = math.degrees(math.atan2(ty - ay, tx - ax))
    # 接近点就在 <1m 外,PD 直线 300 步足够;收窄可让"钻不进去"的坏位姿更快放弃(不空转 500 步)。
    nav(env, ax, ay, target_yaw=yaw, pos_threshold=0.15, max_steps=300)
    # 判"到位"不看是否精确压在接近点上,而看底盘最终离目标够不够近(臂/吸附够得着即可)。
    # 否则接近点差 0.3m 没压到就误报失败,而其实底盘已到目标 0.9m 处完全能抓(cup 案例)。
    fin = get_base_info(env)
    final_dist = float(np.hypot(tx - fin["pos"][0], ty - fin["pos"][1]))
    return final_dist <= _ARM_STAND_REACH


def _avoid_stack(env, obj, tp, clear=0.13, step=0.14, ring=3):
    """放置避让:目标点已被别的物体占着就小幅螺旋偏移到最近空位。

    多样食材都放"同一个盘子中心"时,直接叠一起会互相撞飞/滚下台;偏移开就自然铺成一小簇,
    既不叠飞又都留在盘子附近(组合裁判照样过)。只动 xy,z 不变。"""
    tp = np.asarray(tp, dtype=float)
    others = []
    for o in getattr(env, "objects", []):
        if o == obj:
            continue
        try:
            others.append(env.get_object_pos(o)[:2])
        except Exception:
            pass

    def _free(x, y):
        return all(float(np.hypot(x - ox, y - oy)) > clear for ox, oy in others)

    if _free(tp[0], tp[1]):
        return tp
    for r in range(1, ring + 1):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                x, y = tp[0] + dx * step, tp[1] + dy * step
                if _free(x, y):
                    return np.array([x, y, tp[2]])
    return tp


def process_commands(env):
    """
    主循环调用：处理队列中的所有命令
    在主循环线程中执行，不需要锁
    """
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
                    # 先把底座导航到物体臂可达范围内(接近位姿=物体沿→底座方向退,可站立),
                    # 再真臂 OSC 移过去 → 吸附 → 关夹爪 → 提起。
                    # 底盘没能到位(被障碍卡住)就直接失败,不硬抓 —— 否则"卡住却报成功"。
                    obj = params["obj_name"]
                    if obj in _nongraspable_objects():
                        # 不可抓物体(如 plate 盘子容器):拒绝抓取,免得 planner 误规划"抓盘子"搞乱状态
                        result = {"success": False,
                                  "result": f"{obj} 是不可抓取的容器/固定物,不能抓取(它已在场景里就位,只能把东西放到它上面)"}
                    else:
                        near = True
                        if obj in env.obj_body_id:
                            op = env.get_object_pos(obj)
                            near = _nav_within_reach(env, float(op[0]), float(op[1]))
                        if not near:
                            result = {"success": False,
                                      "result": f"底盘没能到达 {obj} 附近(被障碍卡住/导航失败),抓取失败"}
                        else:
                            ok = grasp(env, obj, snap_threshold=float(params.get("snap_threshold", 0.15)))
                            if ok:
                                env.grasped_object = obj  # 跟踪持有物(供 /status、place 校验)
                            result = {"success": bool(ok),
                                      "result": (f"成功抓取 {obj}" if ok
                                                 else f"未能抓取 {obj}(已到位但吸附范围内没够到)")}
                elif cmd_type == "place":
                    obj = params["obj_name"]
                    # 严格:必须真持有该物体才能放置。抓取失败时 grasped_object 不会被设,
                    # 若这里不拦,place() 会无条件把物体瞬移到目标 → 没抓到却"放置成功"。
                    held = getattr(env, "grasped_object", None)
                    if held != obj:
                        result = {"success": False,
                                  "result": f"未持有 {obj}(当前持有: {held}),放置失败:请先成功抓取该物体"}
                    else:
                        tp = np.asarray(params["target_pos"], dtype=float)
                        # 落点已被别的物体占着就小幅偏移(避免多样食材叠同一盘子中心互相撞飞)
                        tp = _avoid_stack(env, obj, tp)
                        # 底盘必须真到落点臂可达范围内才放;被障碍卡住没到位就失败,
                        # 绝不"瞬移物体到目标坐标"假装放好了(这正是"卡住却放置成功"的根源)。
                        near = _nav_within_reach(env, float(tp[0]), float(tp[1]))
                        if not near:
                            result = {"success": False,
                                      "result": f"底盘没能到达落点附近(被障碍卡住/导航失败),放置失败:{obj} 仍在手上"}
                        else:
                            ok = place(env, obj, tp,
                                       snap_threshold=float(params.get("snap_threshold", 0.15)))
                            if ok:
                                env.grasped_object = None
                            result = {"success": bool(ok),
                                      "result": (f"成功放置 {obj}" if ok else "放置失败")}
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
                    # 差速底盘导航:直线可达就直行 PD;被家具挡住就用 A* 规划绕障路径再跟踪。
                    # (以前只走直线 PD,穿过岛台/柜子就顶着障碍空转到超时 → "卡住";
                    #  现在直线不通先绕行。reached/success 如实反映是否真到位,不再假成功。)
                    gx, gy = float(params["x"]), float(params["y"])
                    tyaw = params.get("target_yaw")
                    yth = float(params.get("yaw_threshold", 5.0))
                    pth = float(params.get("pos_threshold", 0.15))
                    mx = int(params.get("max_steps", 500))
                    path = None
                    if _ASTAR_AVAILABLE:
                        try:
                            b = get_base_info(env)
                            sx, sy = float(b["pos"][0]), float(b["pos"][1])
                            if not _astar_line_clear(sx, sy, gx, gy):
                                path = _astar_plan(sx, sy, gx, gy)  # 直线被挡才规划绕行
                        except Exception as _e:
                            print(f"[nav] A* 规划失败,退回直线: {_e}", file=_sys.stderr, flush=True)
                            path = None
                    if path and len(path) >= 2:
                        # 用可靠的 PD nav() 走 A* 路径(follow_path 的跟踪器不收敛会空转到超时)。
                        # 中间路点 best-effort 走(某段没精确到位不算失败——机器人仍在朝目标推进),
                        # 最后一段直奔目标做精确对准,只有这一段的 reached 才决定成功 → 更鲁棒,
                        # 不会因中间点小偏差就误报导航失败。A* 保证相邻点之间无碰撞。
                        for wp in path[1:-1]:
                            nav(env, float(wp["x"]), float(wp["y"]), target_yaw=None,
                                pos_threshold=0.2, max_steps=200)
                        info = nav(env, gx, gy, target_yaw=tyaw,
                                   pos_threshold=pth, yaw_threshold=yth, max_steps=mx)
                        result = {"success": info.get("reached", False),
                                  "pos": info["pos"], "yaw": info["yaw_deg"]}
                    else:
                        info = nav(env, gx, gy, target_yaw=tyaw,
                                   pos_threshold=pth, yaw_threshold=yth, max_steps=mx)
                        result = {"success": info.get("reached", False),
                                  "pos": info["pos"], "yaw": info["yaw_deg"]}
                elif cmd_type == "move_duration":
                    # 定时速度:按 control_freq 估算步数,连发 move()
                    n = max(1, int(float(params.get("duration", 1.0)) * 20))
                    for _ in range(n):
                        move(env, Vx=float(params.get("vx", 0.0)), Vw=float(params.get("vw", 0.0)))
                    info = get_base_info(env)
                    result = {"success": True, "pos": info["pos"], "yaw": info["yaw_deg"]}
                elif cmd_type == "cmd_vel":
                    info = move(env, **params)
                    result = {"success": True, "pos": info["pos"], "yaw": info["yaw_deg"]}
                elif cmd_type == "screenshot":
                    from PIL import Image as PILImage
                    from io import BytesIO
                    import base64
                    defaults = _screenshot_defaults()
                    cam = params.get("camera_name") or defaults.get("default_camera", "overhead_cam")
                    w = params.get("width") or defaults.get("width", 640)
                    h = params.get("height") or defaults.get("height", 480)
                    quality = int(defaults.get("jpeg_quality", 80))
                    img = env.sim.render(w, h, camera_name=cam)
                    if img is None:
                        result = {"success": False, "result": f"相机 '{cam}' 渲染失败"}
                    else:
                        img = np.flipud(img)  # robosuite render 上下翻,翻正
                        pil_img = PILImage.fromarray(np.ascontiguousarray(img))
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
                elif cmd_type == "scan_rgb":
                    # 转头扫描版 RGB(给 vlm 感知用):非物理地转底盘 base_freejoint yaw 到多个角度,
                    # 每个角度渲一张 RGB,多张一起发 VLM = 一次调用覆盖多视角(提高召回、省 API 次数)。
                    # 只改 base_freejoint 四元数 + forward 渲染、不 step、扫完恢复,不影响物理(等价原地 pan)。
                    # 注意①:PandaOmron 底座是 Option F freejoint,没有 mobilebase yaw 关节 → 转 freejoint quat。
                    # 注意②:写 qpos 必须走 sim.data.set_joint_qpos(robosuite wrapper,同 set_object_pos);
                    #        直接改 raw_data.qpos[...] 渲染不生效(被 wrapper 缓存盖掉)→ 4 帧全同的坑,已踩。
                    from PIL import Image as PILImage
                    from io import BytesIO
                    import base64
                    defaults = _screenshot_defaults()
                    cam = params.get("camera_name") or "robot0_frontview"
                    w = int(params.get("width") or defaults.get("width", 640))
                    h = int(params.get("height") or defaults.get("height", 480))
                    quality = int(defaults.get("jpeg_quality", 80))
                    angles = params.get("angles") or [0, 90, 180, 270]
                    jid = mujoco.mj_name2id(env.raw_model, mujoco.mjtObj.mjOBJ_JOINT, "base_freejoint")
                    qadr = int(env.raw_model.jnt_qposadr[jid]) if jid >= 0 else -1
                    base_qpos = env.raw_data.qpos[qadr: qadr + 7].copy() if qadr >= 0 else None  # 读=view,可靠
                    orig_quat = base_qpos[3:7].copy() if base_qpos is not None else None
                    frames, used_angles = [], []
                    try:
                        for ddeg in angles:
                            if base_qpos is not None:
                                rot = np.zeros(4)
                                mujoco.mju_axisAngle2Quat(rot, np.array([0.0, 0.0, 1.0]),
                                                          math.radians(float(ddeg)))
                                newq = np.zeros(4)
                                mujoco.mju_mulQuat(newq, rot, orig_quat)  # 世界系绕 Z 旋转:rot ⊗ orig
                                q = base_qpos.copy()
                                q[3:7] = newq
                                env.sim.data.set_joint_qpos("base_freejoint", q)  # wrapper 写,渲染才生效
                                env.sim.forward()
                            img = env.sim.render(w, h, camera_name=cam)
                            if img is None:
                                continue
                            img = np.flipud(img)  # robosuite render 上下翻,翻正
                            pil_img = PILImage.fromarray(np.ascontiguousarray(img))
                            buf = BytesIO()
                            pil_img.save(buf, format="JPEG", quality=quality)
                            frames.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
                            used_angles.append(float(ddeg))
                    finally:
                        if base_qpos is not None:
                            env.sim.data.set_joint_qpos("base_freejoint", base_qpos)  # 恢复原朝向
                            env.sim.forward()
                    result = {"success": bool(frames), "camera": cam, "frames": frames,
                              "angles": used_angles, "width": w, "height": h}
                elif cmd_type == "visible_objects":
                    # segmentation 感知:渲染相机分割图,返回视野里可见的物体集。
                    # 部分可观测的真实来源=视角+遮挡+距离(非坐标过滤)。min_pixels 滤掉太小(太远/边缘)的。
                    # 多相机融合:camera="all"=机器人身上相机(head+两腕,真机可复现,不含 overhead 作弊),
                    # 或逗号分隔自定义。取并集=任一相机看到即算看到,减少单视角盲区。
                    # robosuite Panda 自带相机(机器人身上,真机导向):frontview=前视工作区,
                    # eye_in_hand=腕部手眼;agentview_center 备用。"all"=机身相机并集,不含 overhead 作弊。
                    _PERC_CAMS = {"head_cam": "robot0_frontview", "right_arm_cam": "robot0_eye_in_hand",
                                  "left_arm_cam": "robot0_agentview_center"}
                    cam_param = params.get("camera_name") or "head_cam"
                    if cam_param == "all":
                        cams = ["robot0_frontview", "robot0_eye_in_hand", "robot0_agentview_center"]
                    else:
                        cams = [_PERC_CAMS.get(c.strip(), c.strip())
                                for c in cam_param.split(",") if c.strip()] or ["robot0_frontview"]
                    min_px = int(params.get("min_pixels", 1))
                    scan = bool(params.get("scan", False))
                    bid_to_obj = {int(bid): name for name, bid in env.obj_body_id.items()}
                    seen = set()

                    def _collect_visible():
                        for cam in cams:
                            seg = env.render_segmentation(cam)
                            seg_ids = seg[:, :, 0]
                            for gid in np.unique(seg_ids):
                                if gid < 0:
                                    continue
                                bid = int(env.raw_model.geom_bodyid[int(gid)])
                                if bid in bid_to_obj and int((seg_ids == gid).sum()) >= min_px:
                                    seen.add(bid_to_obj[bid])

                    yaw_jid = mujoco.mj_name2id(env.raw_model, mujoco.mjtObj.mjOBJ_JOINT,
                                               "mobilebase0_joint_mobile_yaw")
                    if scan and yaw_jid >= 0:
                        # 转头扫描(robosuite):转移动底座 yaw 关节一圈(每 60°)渲染取并集,扫完恢复。
                        # 只改 qpos + forward 渲染、不 step、不影响物理(等价原地转头 pan)。
                        qadr = int(env.raw_model.jnt_qposadr[yaw_jid])
                        orig = float(env.raw_data.qpos[qadr])
                        for ddeg in range(0, 360, 60):
                            env.raw_data.qpos[qadr] = orig + math.radians(ddeg)
                            env.sim.forward()
                            _collect_visible()
                        env.raw_data.qpos[qadr] = orig
                        env.sim.forward()
                    else:
                        _collect_visible()
                    result = {"success": True, "cameras": cams, "scan": scan, "visible": sorted(seen)}
                elif cmd_type == "inject_to_container":
                    # 把物体藏进容器(set 到容器 fixture pos;高墙柜 z=1.85 机器人 head_cam 1.18
                    # 够不到 → 自然看不到)。记归属,不写 belief(藏=belief unknown,靠开柜门发现)。
                    obj = params["obj_name"]
                    container = params["container"]
                    fx = env.fixtures.get(container)
                    if fx is None:
                        result = {"success": False, "result": f"容器 {container} 不存在"}
                    else:
                        env.set_object_pos(obj, np.asarray(fx.pos, dtype=float))
                        _container_contents.setdefault(container, [])
                        if obj not in _container_contents[container]:
                            _container_contents[container].append(obj)
                        result = {"success": True, "container": container,
                                  "result": f"已把 {obj} 藏进 {container}"}
                elif cmd_type == "open_container":
                    # 开柜门(转门 joint)+ 把里面物体取到机器人脚边台面(z=0.95 可见可抓)。
                    # 模拟"开柜门把东西拿下来",绕开高柜机器人够不到的问题。
                    container = params["container"]
                    opened = env.open_container_door(container)
                    chassis = env.get_body_pos("mobilebase0_base")
                    out = [float(chassis[0]) + 0.3, float(chassis[1]), 0.95]
                    lifted = []
                    for obj in list(_container_contents.get(container, [])):
                        env.set_object_pos(obj, out)
                        lifted.append(obj)
                    _container_contents[container] = []
                    result = {"success": bool(opened or lifted), "opened": opened,
                              "lifted": lifted, "result": f"开 {container}: 门×{len(opened)} 取出 {lifted}"}
                elif cmd_type == "camera_preview":
                    from io import BytesIO
                    import base64
                    from PIL import Image
                    preview = _preview_config()
                    camera_name = params.get("camera_name")
                    camera_names = [camera_name] if camera_name else preview.get("cameras", ["overhead_cam"])
                    w = int(params.get("width") or preview.get("width", 320))
                    h = int(params.get("height") or preview.get("height", 240))
                    quality = int(preview.get("jpeg_quality", 80))
                    panels = _render_camera_preview(env, camera_names, w, h)
                    if len(panels) == 1:
                        image = panels[0]
                    else:
                        while len(panels) < 4:
                            panels.append(Image.new("RGB", (w, h), (0, 0, 0)))
                        image = Image.new("RGB", (w * 2, h * 2))
                        for idx, panel in enumerate(panels[:4]):
                            image.paste(panel, ((idx % 2) * w, (idx // 2) * h))
                    buf = BytesIO()
                    image.save(buf, format="JPEG", quality=quality)
                    result = {
                        "success": True,
                        "image": base64.b64encode(buf.getvalue()).decode("utf-8"),
                        "camera": camera_name,
                    }
                elif cmd_type == "reset_home":
                    # 持续世界复位:全场回初始(物体布局+机械臂位姿+底盘沉降),清空手持。
                    # 每轮 bench 只调一次,settle 几秒可接受,换跨 run 可复现。
                    env.reset()
                    env.grasped_object = None
                    try:
                        # 学习模式:belief 起空(物体未发现,靠探索填回 → 产生学习曲线);
                        # 全可观测模式:belief 直接 = 初始真值。
                        if _learning_mode():
                            reset_belief_unknown()
                        else:
                            reset_to_initial()
                    except Exception as _e:
                        print(f"[reset_home] scene_memory 复位失败: {_e}", flush=True)
                    result = {"success": True}
                elif cmd_type == "inject_move":
                    # 漂移注入:把物体瞬移到目标家具顶面(不计步)。
                    obj = params["obj_name"]
                    target_pos = np.asarray(params["target_pos"], dtype=float)
                    env.set_object_pos(obj, target_pos)
                    if getattr(env, "grasped_object", None) == obj:
                        env.grasped_object = None
                    # 学习模式:只动真值,故意不更新 belief —— 这样机器人的 belief 仍指向旧位置,
                    # 下次导航过去扑空才能"检测到漂移"并触发重搜(这正是漂移恢复实验的核心)。
                    # 全可观测模式:同步 belief 保持一致。
                    if not _learning_mode():
                        try:
                            move_object(obj, coords_to_waypoint(target_pos.tolist()))
                        except Exception as _e:
                            print(f"[inject_move] scene_memory 更新失败: {_e}", flush=True)
                    result = {"success": True, "result": f"moved {obj}"}
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
# 状态查询（直接读取，不走队列）
# ============================================================

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
        return jsonify({"error": "仿真器未初始化"}), 503
    with _env_lock:
        result = {}
        for name in env.objects:
            pos = get_obj_pos(env, name)
            result[name] = {"pos": pos.tolist(), "grasped": is_grasped(env, name)}
    return jsonify(result)

@app.route("/fixtures", methods=["GET"])
def api_fixtures():
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503
    result = {}
    for name, fixture in env.fixtures.items():
        result[name] = {
            "pos": list(np.asarray(fixture.pos, dtype=float)),
            "size": list(np.asarray(fixture.size, dtype=float)),
            "type": type(fixture).__name__,
        }
    return jsonify(result)

@app.route("/scene", methods=["GET"])
def api_scene():
    """返回完整场景信息：物体、家具、机器人"""
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503

    with _env_lock:
        # 物体
        objects = {}
        for name in env.objects:
            pos = get_obj_pos(env, name)
            objects[name] = {"pos": pos.tolist(), "grasped": is_grasped(env, name)}

        # 家具
        fixtures = {}
        for name in env.fixtures:
            fxtr = env.fixtures[name]
            fixtures[name] = {
                "pos": np.asarray(fxtr.pos, dtype=float).tolist(),
                "size": np.asarray(fxtr.size, dtype=float).tolist(),
                "type": type(fxtr).__name__,
            }

        # 机器人
        arm_info = get_arm_info(env)
        base_info = get_base_info(env)
        robot = {
            "base_pos": base_info["pos"],
            "ee_pos": arm_info["ee_pos"],
            "yaw": base_info.get("yaw_deg", 0),
        }

    return jsonify(_np_to_list({
        "objects": objects,
        "fixtures": fixtures,
        "robot": robot,
    }))

@app.route("/scene_state", methods=["GET"])
def api_scene_state():
    """返回当前场景状态记忆"""
    try:
        return jsonify(get_all_locations())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/belief", methods=["GET"])
def api_belief():
    """belief 物体视角: {objects: {obj: {location}}, holding}。前端「物体状态(belief)」表用。

    location = 机器人当前对该物体位置的记忆(家具名/robot_hand/工作点/unknown),
    学习模式下起始多为 unknown,随探索逐步填回;不是物理真值(那是 /objects)。
    """
    try:
        env = _get_env()
        all_objs = list(env.obj_body_id.keys()) if env else None
        held = getattr(env, "grasped_object", None) if env else None
        belief = get_belief_by_object(all_objs)
        # 手上持有物以物理事实为准(belief 若还没写 robot_hand 就纠正)
        if held and held in belief:
            belief[held] = {"location": "robot_hand"}
        return jsonify({"objects": belief, "holding": held})
    except Exception as e:
        return jsonify({"error": str(e), "objects": {}}), 500


# ============================================================
# Phase 1 持续世界 (per-home) 端点 —— 对齐 serve_alfworld，bench 可直接复用
# ============================================================

@app.route("/homes", methods=["GET"])
def api_homes():
    """MuJoCo 单场景:只有一个 home(id=1)。保留接口形状以兼容 bench。"""
    return jsonify({"split": "mujoco", "homes": {"1": [0]}, "count": 1})


@app.route("/reset_home", methods=["POST"])
def api_reset_home():
    """载入/复位持续世界:物体回初始布局,置 persistent,清零步数。Body: {floorplan_id?}。"""
    env = _get_env()
    if env is None:
        return jsonify({"success": False, "result": "仿真器未初始化"}), 503
    r = submit_command("reset_home", {})
    if not r.get("success"):
        return jsonify({"success": False, "result": r})
    _home["persistent"] = True
    _home["steps"] = 0
    _home["task"] = ""
    return jsonify({"success": True, "result": {
        "persistent": True,
        "task": "",
        "objects": list(env.objects),
        "fixtures": list(env.fixtures.keys()),
    }})


@app.route("/set_task", methods=["POST"])
def api_set_task():
    """持续模式专用:切换当前任务文字(不复位世界/belief)。Body: {task}。"""
    data = request.json or {}
    task = (data.get("task") or "").strip()
    if not task:
        return jsonify({"success": False, "result": "missing task"})
    if not _home["persistent"]:
        return jsonify({"success": False, "result": "set_task 仅持续模式可用(先 POST /reset_home)"})
    _home["task"] = task
    return jsonify({"success": True, "task": task, "shadow_done_now": _judge_task(task)})


@app.route("/inject_to_container", methods=["POST"])
def api_inject_to_container():
    """把物体藏进容器(测 speedup:藏起来→首次要开柜翻找)。Body: {obj, container}。"""
    data = request.json or {}
    obj = data.get("obj") or data.get("obj_name")
    container = data.get("container")
    if not obj or not container:
        return jsonify({"success": False, "result": "missing obj or container"})
    return jsonify(submit_command("inject_to_container", {"obj_name": obj, "container": container}))


@app.route("/containers", methods=["GET"])
def api_containers():
    """列出可藏物的容器(有门 joint 的高柜)+ 坐标。slaver 逐柜翻找用。
    Query: ?min_height=1.2(过滤矮柜)。"""
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503
    try:
        min_h = float(request.args.get("min_height", 1.2))
    except (TypeError, ValueError):
        min_h = 1.2
    with _env_lock:
        containers = env.list_containers(min_height=min_h)
    return jsonify({"success": True, "containers": containers})


@app.route("/open_container", methods=["POST"])
def api_open_container():
    """开柜门(转门 joint)+ 取出里面物体到台面(露出可见可抓)。Body: {container}。
    每次 open 计一步(持续模式)=逐柜翻找的搜索成本。"""
    data = request.json or {}
    container = data.get("container")
    if not container:
        return jsonify({"success": False, "result": "missing container"})
    _count_step()  # 持续模式步数指标:每开一个柜 = 一步搜索成本
    return jsonify(submit_command("open_container", {"container": container}))


@app.route("/inject_move", methods=["POST"])
def api_inject_move():
    """测试注入:把物体瞬移到目标家具(模拟家里东西被挪了)。Body: {obj, to}。仅持续模式。"""
    data = request.json or {}
    obj = data.get("obj") or data.get("object_name") or data.get("obj_name")
    to = data.get("to") or data.get("to_receptacle")
    if not obj or not to:
        return jsonify({"success": False, "result": "missing obj or to"})
    if not _home["persistent"]:
        return jsonify({"success": False, "result": "inject_move 仅持续模式可用"})
    target_obj = _match_object(str(obj))
    if not target_obj:
        return jsonify({"success": False, "result": f"全场未找到 '{obj}'"})
    fixes = _fixtures_for_base(str(to))
    if not fixes:
        return jsonify({"success": False, "result": f"未找到目标家具 '{to}'"})
    target_pos = _fixture_top_center(fixes[0])
    r = submit_command("inject_move", {"obj_name": target_obj, "target_pos": target_pos})
    if r.get("success"):
        return jsonify({"success": True, "result": f"You move the {target_obj} to the {fixes[0]}."})
    return jsonify({"success": False, "result": r})


@app.route("/success", methods=["GET"])
def api_success():
    """完成裁判:持续模式用几何裁判(物体落在目标家具上),替代 oracle won。"""
    if _home["persistent"] and _home["task"]:
        return jsonify({"won": _judge_task(_home["task"]), "steps": _home["steps"], "shadow_judge": True})
    return jsonify({"won": False, "steps": _home["steps"], "shadow_judge": False})


@app.route("/shadow_state", methods=["GET"])
def api_shadow_state():
    """belief 层视图:物体→最近家具(held 优先) + holding。供 bench 调试/对照。"""
    env = _get_env()
    held = getattr(env, "grasped_object", None) if env else None
    return jsonify({"at": _shadow_at(), "holding": held})

# ============================================================
# 视频录制（工具函数内主动调用 record_hook.try_record_frame）
# ============================================================

@app.route("/record/start", methods=["POST"])
def api_record_start():
    """开始录制"""
    if _recording["active"]:
        return jsonify({"success": False, "message": "已在录制中"})

    data = request.json or {}
    _recording["fps"] = data.get("fps", 1)
    _recording["interval"] = 1.0 / _recording["fps"]
    _recording["frames"] = []
    _recording["last_capture"] = 0
    _recording["active"] = True

    total_w = _recording["view_width"] * 2
    total_h = _recording["view_height"] * 2
    print(f"[record] 开始录制 ({_recording['fps']}fps, 4视角, {total_w}x{total_h})", flush=True)
    return jsonify({"success": True, "message": "录制已开始"})


@app.route("/record/stop", methods=["POST"])
def api_record_stop():
    """停止录制并保存为视频文件"""
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
    """下载录制的视频文件"""
    filepath = os.path.join(_video_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(filepath, mimetype="video/mp4", as_attachment=True)


@app.route("/record/status", methods=["GET"])
def api_record_status():
    """查询录制状态"""
    return jsonify({
        "active": _recording["active"],
        "frames": len(_recording["frames"]),
        "fps": _recording["fps"],
    })


# ============================================================
# 控制命令（走队列）
# ============================================================

@app.route("/grasp", methods=["POST"])
def api_grasp():
    data = request.json or {}
    obj_name = data.get("obj_name")
    if not obj_name:
        return jsonify({"error": "缺少 obj_name 参数"}), 400
    params = {
        "obj_name": obj_name,
        "snap_threshold": data.get("snap_threshold", 0.15),
    }
    res = submit_command("grasp", params)
    _count_step()  # 持续模式步数指标
    return jsonify(res)


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
    res = submit_command("place", params)
    _count_step()  # 持续模式步数指标
    return jsonify(res)


@app.route("/move_to", methods=["POST"])
def api_move_to():
    data = request.json or {}
    target = data.get("target")
    if target is None:
        return jsonify({"error": "缺少 target 参数"}), 400
    params = {
        "target_pos": target,
        "max_steps": data.get("max_steps", 200),
        "pos_threshold": data.get("pos_threshold", 0.03),
    }
    res = submit_command("move_to", params)
    _count_step()  # 持续模式步数指标
    return jsonify(res)


@app.route("/move_duration", methods=["POST"])
def api_move_duration():
    """以指定速度持续移动底盘一段时间（走命令队列）"""
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
    # 兼容旧参数名 w
    elif data.get("w") is not None:
        params["target_yaw"] = data["w"]
    res = submit_command("nav", params)
    _count_step()  # 持续模式步数指标
    return jsonify(res)


@app.route("/cmd_vel", methods=["POST"])
def api_cmd_vel():
    """接收速度命令，单步执行（供 Nav2 桥接节点调用）"""
    data = request.json or {}
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503
    set_base_velocity(
        Vx=data.get("vx", 0.0),
        Vy=data.get("vy", 0.0),
        Vw=data.get("vw", 0.0),
        timeout=data.get("duration", 0.25),
    )
    with _env_lock:
        info = get_base_info(env)
    return jsonify({"success": True, "pos": info["pos"], "yaw": info["yaw_deg"]})


@app.route("/open_gripper", methods=["POST"])
def api_open_gripper():
    return jsonify(submit_command("open_gripper", {}))


@app.route("/close_gripper", methods=["POST"])
def api_close_gripper():
    return jsonify(submit_command("close_gripper", {}))


# ============================================================
# 地图生成数据（供 map_generator.py --from-sim 使用）
# ============================================================

def _map_request_params():
    resolution = float(request.args.get("resolution", 0.05))
    x_min = float(request.args.get("x_min", -1.0))
    x_max = float(request.args.get("x_max", 8.0))
    y_min = float(request.args.get("y_min", -6.0))
    y_max = float(request.args.get("y_max", 1.0))
    return resolution, x_min, x_max, y_min, y_max


def _build_occupancy_grid(env, resolution, x_min, x_max, y_min, y_max):
    width = int((x_max - x_min) / resolution)
    height = int((y_max - y_min) / resolution)

    with _env_lock:
        # 必须用原始 MjModel/MjData:mujoco.mj_name2id / mj_id2name 只接受原始句柄,
        # 传 robosuite 的 env.sim.model 包装会 TypeError(incompatible function arguments)→ /map_data 500。
        model = env.raw_model
        data = env.raw_data

        # 排除机器人 body(PandaOmron:robot0_* / mobilebase0_* / gripper0_*),
        # 从底座根 robot0_base 子树排除,避免地图把机器人自身投影成静态障碍物。
        robot_bodies = set()
        chassis_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "robot0_base")
        for i in range(model.nbody):
            name = (mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, i) or "").lower()
            if chassis_id >= 0 and _is_body_descendant(model, i, chassis_id):
                robot_bodies.add(i)
            elif any(k in name for k in (
                "robot0", "mobilebase", "gripper0", "eef_target",
            )):
                robot_bodies.add(i)

        rects = []
        for i in range(model.ngeom):
            body_id = model.geom_bodyid[i]
            if body_id in robot_bodies:
                continue

            name = (mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, i) or "").lower()
            if any(k in name for k in ("floor", "ground", "ceiling", "skybox", "visual", "light")):
                continue

            pos = data.geom_xpos[i]
            size = model.geom_size[i]
            mat = data.geom_xmat[i].reshape(3, 3)

            top_z = pos[2] + abs(size[2]) * 2
            if pos[2] - abs(size[2]) > 2.0 or top_z < 0.05:
                continue

            xs, ys = [], []
            for sx in (-size[0], size[0]):
                for sy in (-size[1], size[1]):
                    for sz in (-size[2], size[2]):
                        corner = pos + mat @ np.array([sx, sy, sz])
                        xs.append(corner[0])
                        ys.append(corner[1])

            x1, x2 = min(xs), max(xs)
            y1, y2 = min(ys), max(ys)
            col_min = max(0, int((x1 - x_min) / resolution))
            col_max = min(width - 1, int((x2 - x_min) / resolution))
            row_min = max(0, int((y_max - y2) / resolution))
            row_max = min(height - 1, int((y_max - y1) / resolution))
            if col_min <= col_max and row_min <= row_max:
                rects.append((col_min, row_min, col_max, row_max))

        grid = bytearray([255] * (width * height))
        for c1, r1, c2, r2 in rects:
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    grid[r * width + c] = 0

    return {
        "grid": list(grid),
        "width": width,
        "height": height,
        "resolution": resolution,
        "origin": [x_min, y_min],
    }


@app.route("/map_data", methods=["GET"])
def api_map_data():
    """从上往下投射：读取所有 MuJoCo geom 的世界坐标 2D 投影，生成占据栅格"""
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503

    return jsonify(_build_occupancy_grid(env, *_map_request_params()))


@app.route("/scan", methods=["GET"])
def api_scan():
    """基于当前占据栅格和机器人位姿生成 2D LaserScan 数据。"""
    env = _get_env()
    if env is None:
        return jsonify({"error": "仿真器未初始化"}), 503

    angle_min = float(request.args.get("angle_min", -math.pi))
    angle_max = float(request.args.get("angle_max", math.pi))
    angle_increment = float(request.args.get("angle_increment", math.radians(1.0)))
    range_min = float(request.args.get("range_min", 0.05))
    range_max = float(request.args.get("range_max", 5.0))

    grid_data = _build_occupancy_grid(env, *_map_request_params())
    with _env_lock:
        base = _read_base_info(env)

    grid = grid_data["grid"]
    width = int(grid_data["width"])
    height = int(grid_data["height"])
    resolution = float(grid_data["resolution"])
    x_min, y_min = grid_data["origin"]
    y_max = y_min + height * resolution

    x0, y0 = float(base["pos"][0]), float(base["pos"][1])
    yaw = float(base.get("yaw_rad", math.radians(float(base.get("yaw_deg", 0.0)))))

    def is_occupied(x, y):
        col = int((x - x_min) / resolution)
        row = int((y_max - y) / resolution)
        if col < 0 or col >= width or row < 0 or row >= height:
            return False
        return grid[row * width + col] < 128

    count = max(1, int(math.floor((angle_max - angle_min) / angle_increment)) + 1)
    step = max(0.02, resolution * 0.5)
    ranges = []
    for i in range(count):
        rel_angle = angle_min + i * angle_increment
        world_angle = yaw + rel_angle
        hit = None
        dist = range_min
        while dist <= range_max:
            x = x0 + dist * math.cos(world_angle)
            y = y0 + dist * math.sin(world_angle)
            if is_occupied(x, y):
                hit = round(dist, 3)
                break
            dist += step
        ranges.append(hit)

    return jsonify({
        "success": True,
        "frame_id": "laser",
        "base_frame_id": "base_link",
        "angle_min": angle_min,
        "angle_max": angle_max,
        "angle_increment": angle_increment,
        "range_min": range_min,
        "range_max": range_max,
        "ranges": ranges,
        "pose": {
            "x": x0,
            "y": y0,
            "yaw": yaw,
        },
    })


# ============================================================
# 截图（走命令队列，在主线程渲染，避免 EGL 跨线程问题）
# ============================================================

@app.route("/screenshot", methods=["POST"])
def api_screenshot():
    """从指定相机捕获单帧截图，返回 base64 编码的 JPEG 图像"""
    data = request.json or {}
    defaults = _screenshot_defaults()
    params = {
        "camera_name": data.get("camera_name") or defaults.get("default_camera", "overhead_cam"),
        "width": data.get("width") or defaults.get("width", 640),
        "height": data.get("height") or defaults.get("height", 480),
    }
    return jsonify(submit_command("screenshot", params))


@app.route("/scan_rgb", methods=["POST"])
def api_scan_rgb():
    """转头扫描:非物理转底盘 yaw 到多个角度,每个角度渲一张 RGB,返回 base64 帧列表。
    给 vlm 感知用——多角度一次发 VLM,提高目标召回。走命令队列在主线程渲染。
    Body: {camera_name, width, height, angles:[deg,...]}"""
    data = request.json or {}
    defaults = _screenshot_defaults()
    params = {
        "camera_name": data.get("camera_name") or "robot0_frontview",
        "width": data.get("width") or defaults.get("width", 640),
        "height": data.get("height") or defaults.get("height", 480),
        "angles": data.get("angles") or [0, 90, 180, 270],
    }
    return jsonify(submit_command("scan_rgb", params))


@app.route("/visible_objects", methods=["GET"])
def api_visible_objects():
    """机器人相机视野里可见的物体集(segmentation 部分可观测感知)。
    Query: ?camera=head_cam&min_pixels=1。走命令队列在主线程渲染。"""
    params = {
        "camera_name": request.args.get("camera", "head_cam"),
        "min_pixels": int(request.args.get("min_pixels", 1)),
        "scan": request.args.get("scan", "0") in ("1", "true", "True"),
    }
    return jsonify(submit_command("visible_objects", params))


@app.route("/camera/status", methods=["GET"])
def api_camera_status():
    """返回相机渲染配置"""
    return jsonify({
        "success": True,
        "config": _camera_config(),
    })


@app.route("/camera/latest", methods=["GET"])
def api_camera_latest():
    """按需渲染相机图片；默认四宫格，?camera=name 返回单路相机。走命令队列渲染(serve 空闲时很快;
    正巧命令执行中会排队等它结束再渲——时间线因此天然落在子任务边界,正是我们要的效果)。"""
    result = submit_command("camera_preview", {
        "camera_name": request.args.get("camera"),
        "width": request.args.get("width"),
        "height": request.args.get("height"),
    })
    if not result.get("success"):
        return jsonify(result), 500
    import base64
    return Response(base64.b64decode(result["image"]), mimetype="image/jpeg")


@app.route("/live", methods=["GET"])
def api_live():
    """极简"边跑边看"页:浏览器轮询 /camera/latest 自动刷新——离屏渲染,不需原生窗口/主线程,
    绕开 macOS mjpython viewer 抢主线程崩溃的坑。任务执行中渲染排在命令后面 → 按子任务边界更新
    (每步做完刷一帧);空闲时刷新很快。默认四宫格;?camera=overhead_cam 看单路俯视。"""
    cam = request.args.get("camera", "")
    html = """<!doctype html><html><head><meta charset="utf-8"><title>FQPlanner Live</title>
<style>html,body{margin:0;background:#0f172a;color:#cbd5e1;font-family:system-ui;text-align:center}
img{max-width:100vw;max-height:92vh;border-radius:6px}#s{padding:6px;font-size:13px}</style></head>
<body><div id="s">连接中…</div><img id="v" alt="live"><script>
var cam=%s, img=document.getElementById('v'), s=document.getElementById('s'), n=0;
function load(){var i=new Image();
 i.onload=function(){img.src=i.src;n++;
   s.textContent='● 实时观看 · '+(cam||'四宫格')+' · 已刷新 '+n+' 帧 · 任务执行中按子任务边界更新';
   setTimeout(load,500);};
 i.onerror=function(){s.textContent='等待 serve…';setTimeout(load,1500);};
 i.src='/camera/latest?'+(cam?('camera='+encodeURIComponent(cam)+'&'):'')+'t='+Date.now();}
load();
</script></body></html>""" % (repr(cam),)
    return Response(html, mimetype="text/html")


# ============================================================
# 启动函数
# ============================================================

def start_server(env, port=5001):
    _env_holder["env"] = env

    # 不再挂步进钩子:实时四宫格已移除。以前每步渲染 4 路相机(占主循环、并发抢渲染)会把 nav/grasp/place
    # 拖慢一倍甚至卡死;现在命令跑纯物理速度,时间线只在子任务边界(serve 空闲)按需渲一帧。
    def run():
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    print(f"[service] Flask API 已启动: http://localhost:{port}")
    return thread
