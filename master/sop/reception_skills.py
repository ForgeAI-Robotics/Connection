"""技能层 + 观测层 —— 大脑与外部执行/观测的接口抽象(八月接待 demo;呼应桌面 skill_executor.py)。

大脑只发【语义指令】(开灯/扫描/取可乐/走到会议室/摆放/播报),不给坐标;verify 只信【重新观测】。
三个 backend(拿到真机端口零改大脑决策逻辑):
  - mock:      调进程内 reception_world(验证大脑逻辑,可注入 fault)。
  - robot_api: 调 robot_api.client —— 统一执行/观测总闸(sim_atomic / robocasa / dream / VLA 组)。
  - pbd:       调 pbd_world.PBDWorld —— 从 PBD-AG 语义地图(HTTP 轮询)拉观测(数物体/位姿)+ 发工作点
               导航(navigate)。**操作(pick/place)不归 PBD 归 VLA** → pbd 模式下 pick/place/夹爪
               回退 robot_api;灯控/播报回退占位。真机 = PBD(导航+语义) + VLA(操作) 双后端并存。

切 backend:环境变量 RECEPTION_BACKEND=pbd,或 reception_loop.py --backend pbd。
"""

import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# mock 世界(仅 mock backend 用)
from reception_world import (w_set_light, w_pick_cola, w_walk, w_place_cola,   # noqa: E402
                             w_count_cola, w_chairs_blocking, w_light_state,
                             w_robot_holding, w_robot_at, w_last_slot, w_all_slots)

COLA = "可乐"
BACKEND = os.environ.get("RECEPTION_BACKEND", "mock")

# 语义地名 → robot_api 区域名(robot_api 后端用)
REGION = {"茶水间": "tea_room", "会议室": "meeting_room"}

# ---- PBD backend:接待语义区域 → PBD 语义类别(区域用该类物体定位)----
PBD_REGION_CLASS = {"会议室": "table", "茶水间": "fridge"}   # 会议室=桌子区, 茶水间=冰箱区
PBD_DRINK_CLASSES = ["bottle", "cola"]                       # 可乐(联调按 PBD 实际类名调)
PBD_STANDOFF_M = 0.7                                         # 工作点离目标物体的站位距离


def set_backend(name):
    global BACKEND
    BACKEND = name


def _api():
    """延迟 import robot_api.client(mock/pbd 模式不触碰)。"""
    _ROOT = os.path.dirname(os.path.dirname(_HERE))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from robot_api import client as api
    return api


_PBD = None


def _pbd():
    """延迟构造 PBDWorld(轮询 PBD 语义地图)。"""
    global _PBD
    if _PBD is None:
        from pbd_world import PBDWorld
        _PBD = PBDWorld()
    return _PBD


def _pbd_region_center(room):
    """区域代表物体的中心 xy(会议室→桌子, 茶水间→冰箱);拿不到返 None。"""
    cls = PBD_REGION_CLASS.get(room, room)
    objs = _pbd().objects(cls)
    if objs:
        c = objs[0].get("center") or [0, 0, 0]
        return [float(c[0]), float(c[1])]
    return None


def _pbd_count_cola(room):
    """PBD 里的可乐数。TODO 按 room 区域过滤(现数全局;当前快照无可乐→0)。"""
    w = _pbd()
    return sum(w.count(c) for c in PBD_DRINK_CLASSES)


def _is_cola(name, v):
    s = (str(name) + str(v.get("category", ""))).lower()
    return "可乐" in (str(name) + str(v.get("category", ""))) or "cola" in s or "coke" in s


# ============ 技能(大脑发语义指令) ============

def turn_on_lights(room):
    if BACKEND == "mock":
        return {"success": w_set_light(room, True), "room": room}
    return {"success": True, "room": room, "note": "TODO 接灯控/IoT"}


def scan(room):
    if BACKEND == "mock":
        return {"success": True, "cola": w_count_cola(room),
                "chairs_blocking": w_chairs_blocking(room)}
    if BACKEND == "pbd":
        # PBD 语义地图:数该房间可乐 + 有无办公椅
        return {"success": True, "cola": _pbd_count_cola(room),
                "chairs_blocking": _pbd().count("chair") > 0}
    # robot_api
    return {"success": True, "cola": obs_count_cola(room),
            "chairs_blocking": False, "note": "TODO 办公椅检测接场景图"}


def pick(target=COLA, at="茶水间"):
    """取物(接触操作,归 VLA)。mock:world;pbd/robot_api:VLA /grasp。"""
    if BACKEND == "mock":
        ok = w_pick_cola(at)
        return {"success": True} if ok else {
            "success": False, "fail_stage": "grasp", "fail_reason": f"没抓到{target}"}
    r = _api().grasp_object(target) or {}
    if r.get("success"):
        return {"success": True}
    return {"success": False, "fail_stage": "grasp", "fail_reason": r.get("result")}


def walk(to="会议室"):
    """行走。mock:world;robot_api:/nav 语义地名;pbd:区域→工作点坐标→PBD navigate。"""
    if BACKEND == "mock":
        ok = w_walk(to)
        return {"success": True, "at": to} if ok else {
            "success": False, "fail_reason": "办公椅挡路,过不去"}
    if BACKEND == "pbd":
        c = _pbd_region_center(to)
        if c is None:
            return {"success": False, "fail_reason": f"PBD 里没有 {to} 的代表物体"}
        # 简化工作点:离目标物体 STANDOFF 米站位(TODO 用 workpoints_generator 正式生成 + 朝向)
        wx, wy, yaw = c[0] - PBD_STANDOFF_M, c[1], 0.0
        nav = _pbd().navigate(wx, wy, yaw)
        return {"success": True, "at": to,
                "workpoint": [round(wx, 2), round(wy, 2)], "nav": nav}
    # robot_api
    r = _api().navigate_to(REGION.get(to, to)) or {}
    if r.get("success", True):
        return {"success": True, "at": to}
    return {"success": False, "fail_reason": r.get("result")}


def place(target=COLA, to="会议室"):
    """摆放(接触操作,归 VLA)。mock:world;pbd/robot_api:VLA /place。"""
    if BACKEND == "mock":
        return {"success": w_place_cola(to)}
    r = _api().place_object(target, REGION.get(to, to)) or {}
    if r.get("success"):
        return {"success": True}
    return {"success": False, "fail_reason": r.get("result")}


def speak(text):
    print(f"    🔊 [语音播报] {text}")
    return {"success": True}   # robot_api/pbd 模式 TODO 接 TTS 服务


# ============ 观测层(大脑 verify 用;永远重新观测,不采信技能自报) ============

def obs_light_on(room):
    if BACKEND == "mock":
        return w_light_state(room) == "on"
    return True   # TODO 接灯控状态回读


def obs_holding():
    """手上有没有可乐(= empty-grasp 检测;归 VLA 夹爪,PBD 不知道)。"""
    if BACKEND == "mock":
        return w_robot_holding()
    return COLA if _api().is_object_grasped(COLA) else None


def obs_robot_at():
    """机器人在哪个房间。"""
    if BACKEND == "mock":
        return w_robot_at()
    if BACKEND == "pbd":
        xyt = _pbd().robot_xyt()
        if not xyt:
            return None   # PBD stale 时无实时位姿 → verify 降级暂信自报
        rx, ry = float(xyt[0]), float(xyt[1])
        best, best_d = None, 1e9
        for room in PBD_REGION_CLASS:
            c = _pbd_region_center(room)
            if c:
                d = math.hypot(rx - c[0], ry - c[1])
                if d < best_d:
                    best, best_d = room, d
        return best
    return None   # robot_api:TODO base_status 位姿→区域映射


def obs_count_cola(room):
    """room 里的可乐数(缺口/放置确认)。"""
    if BACKEND == "mock":
        return w_count_cola(room)
    if BACKEND == "pbd":
        return _pbd_count_cola(room)
    objs = _api().get_objects() or {}
    return sum(1 for k, v in objs.items() if _is_cola(k, v))


def obs_last_slot(room):
    """最近摆放槽位(间隔/朝向);最简版不查姿态。"""
    if BACKEND == "mock":
        return w_last_slot(room)
    return None   # TODO 姿态复核靠 capture_image + vlm_judge


def obs_all_slots(room):
    """所有已摆放槽位(终局全局摆放复核)。"""
    if BACKEND == "mock":
        return w_all_slots(room)
    return []     # TODO 靠 vlm_judge 逐罐复核
