"""Existing desk skill declarations and scene predicates; no physical loop."""
import json
import urllib.request

SKILLS = {
    "clean_trash":    {"category": "trash",     "zone": "trash_bin",      "label": "清理垃圾(纸巾团→桌面垃圾桶)"},
    "tidy_milk":      {"category": "milk",      "zone": "milk_area",      "label": "整理牛奶"},
    "tidy_cola":      {"category": "cola",      "zone": "drinks_area",    "label": "整理可乐"},
    "tidy_penholder": {"category": "penholder", "zone": "penholder_spot", "label": "整理笔筒"},
}


def backend_url():
    from robot_api.config import load_robot_api_config
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
    from robot_api import client as api
    spec = SKILLS[skill_name]
    objects = objects if objects is not None else api.get_objects()
    zones = zones or get_zones()
    zone = zones[spec["zone"]]
    return [k for k, v in objects.items()
            if v.get("category") == spec["category"] and not in_zone(v["pos"], zone)]
