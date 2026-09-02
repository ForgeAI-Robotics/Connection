"""技能执行层 —— 大脑与 VLA 的接口抽象(demo 定点桌面版)。

大脑只调 execute_skill("tidy_cola") 这种技能级指令,不关心底下是谁在干:
  backend = "sim_atomic": 技能 → robot_api 原子 grasp/place 循环(现在;desk mock / 仿真)。
                          "整理X" = 该类别所有不在位物体逐个抓放,模拟 VLA "多瓶一起整理"。
  backend = "vla_http":   技能 → POST 到 VLA 组服务(将来他们训好,填 url 即切,闭环零改)。

返回统一契约: {success, moved, fail_obj?, fail_stage?, fail_reason?}
"""

import json
import os
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from robot_api import client as api                      # noqa: E402
from robot_api.config import load_robot_api_config       # noqa: E402

# demo 四技能(VLA 组当前能力范围)+ 目标区域(desk mock 的 zones;真机=标准图指定位置)
SKILLS = {
    "clean_trash":    {"category": "trash",     "zone": "trash_bin",      "label": "清理垃圾(纸巾团→桌面垃圾桶)"},
    "tidy_milk":      {"category": "milk",      "zone": "milk_area",      "label": "整理牛奶"},
    "tidy_cola":      {"category": "cola",      "zone": "drinks_area",    "label": "整理可乐"},
    "tidy_penholder": {"category": "penholder", "zone": "penholder_spot", "label": "整理笔筒"},
}


def backend_url():
    cfg = load_robot_api_config()
    for b in cfg.backends:
        if b.name == cfg.active_backend:
            return b.url
    raise RuntimeError(f"active_backend {cfg.active_backend} 未找到 url")


def get_zones():
    with urllib.request.urlopen(backend_url() + "/zones", timeout=10) as r:
        return json.loads(r.read())


def in_zone(pos, zone, tol=0.05):
    dx, dy = pos[0] - zone["pos"][0], pos[1] - zone["pos"][1]
    return (dx * dx + dy * dy) ** 0.5 <= zone["radius"] + tol


def pending_objects(skill_name, objects=None, zones=None):
    """该技能类别下、还不在目标区的物体(= 这个技能当前要处理的)。"""
    spec = SKILLS[skill_name]
    objects = objects if objects is not None else api.get_objects()
    zones = zones or get_zones()
    zone = zones[spec["zone"]]
    return [k for k, v in objects.items()
            if v.get("category") == spec["category"] and not in_zone(v["pos"], zone)]


def execute_skill(skill_name, backend="sim_atomic", vla_url=None):
    spec = SKILLS[skill_name]

    if backend == "vla_http":
        # 将来对接 VLA 组:整技能下发,他们内部决定数量/顺序
        req = urllib.request.Request(
            (vla_url or "").rstrip("/") + "/skill",
            data=json.dumps({"name": skill_name}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except Exception as exc:
            return {"success": False, "fail_stage": "vla_http", "fail_reason": str(exc), "moved": []}

    # --- sim_atomic:原子 grasp/place 循环 ---
    todo = pending_objects(skill_name)
    if not todo:
        return {"success": True, "moved": [], "note": "该类别已全部在位,无需操作"}
    moved = []
    for obj in todo:
        g = api.grasp_object(obj)
        if not g.get("success"):
            return {"success": False, "fail_obj": obj, "fail_stage": "grasp",
                    "fail_reason": g.get("result"), "moved": moved}
        p = api.place_object(obj, spec["zone"])
        if not p.get("success"):
            return {"success": False, "fail_obj": obj, "fail_stage": "place",
                    "fail_reason": p.get("result"), "moved": moved}
        moved.append(obj)
    return {"success": True, "moved": moved}
