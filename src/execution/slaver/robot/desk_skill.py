"""Existing Slaver tidy tool implementation, using the selected robot endpoint."""
import json
import urllib.request
from execution.robot_api import client as api
from execution.robot_api.desk import SKILLS, pending_objects

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
