"""serve_desk — 定点桌面世界 mock 后端(大脑闭环开发用,零仿真依赖)。

对齐 robot_api 后端契约(/objects /grasp /place),物体直接用 demo 语义(牛奶/可乐/
纸巾团/笔筒/手机)。核心能力 = **可控失败注入**:开发"失败重规划"闭环需要可控、
可复现的失败,这是真 VLA(随机失败)给不了的。

  GET  /objects            {name: {pos, grasped, category}}
  GET  /zones              区域定义 {zone: {pos, radius}}(大脑几何确认用)
  POST /grasp {obj_name}   受 /fault 控制
  POST /place {obj_name, target}   target=区域名;place_miss 注入 = 放偏但仍报成功
                                    (模拟 VLA 以为自己成功了 → 靠大脑确认抓出来)
  POST /fault {next: "grasp_fail"|"place_miss"} | {rate: 0.3} | {clear: true}
  POST /reset              回到初始乱桌
  GET  /health

用法: python serve_desk/main.py   (默认 :5008)
"""

import argparse
import copy
import random

from flask import Flask, jsonify, request

app = Flask(__name__)

# 桌面区域(抽象桌面坐标,米)
ZONES = {
    "trash_bin":       {"pos": [1.00, 0.00], "radius": 0.12},   # 桌面垃圾桶
    "drinks_area":     {"pos": [0.85, 0.55], "radius": 0.15},   # 可乐指定区
    "milk_area":       {"pos": [0.55, 0.60], "radius": 0.15},   # 牛奶指定区
    "penholder_spot":  {"pos": [0.15, 0.50], "radius": 0.10},   # 笔筒指定位
}

# 初始乱桌:牛奶/可乐/笔筒不在位,纸巾团在桌面,手机(个人物品)在场
_INIT = {
    "milk_1":         {"pos": [0.30,  0.10], "category": "milk"},
    "milk_2":         {"pos": [0.50, -0.20], "category": "milk"},
    "cola_1":         {"pos": [0.70, -0.10], "category": "cola"},
    "cola_2":         {"pos": [0.40,  0.30], "category": "cola"},
    "pen_holder_1":   {"pos": [0.60,  0.20], "category": "penholder"},
    "tissue_ball_1":  {"pos": [0.35, -0.15], "category": "trash"},
    "tissue_ball_2":  {"pos": [0.55,  0.05], "category": "trash"},
    "mobile_phone_1": {"pos": [0.25,  0.40], "category": "personal"},  # SOP: 禁动
}

_world = {}
_fault = {"next": None, "rate": 0.0}
_holding = {"obj": None}


def _reset_world():
    global _world
    _world = copy.deepcopy(_INIT)
    for o in _world.values():
        o["grasped"] = False
    _fault["next"], _fault["rate"] = None, 0.0
    _holding["obj"] = None


_reset_world()


def _consume_fault(kind):
    if _fault["next"] == kind:
        _fault["next"] = None
        return True
    return _fault["rate"] > 0 and random.random() < _fault["rate"]


@app.route("/health")
def health():
    return jsonify({"success": True, "backend": "desk", "objects": len(_world),
                    "fault": dict(_fault), "holding": _holding["obj"]})


@app.route("/objects")
def objects():
    return jsonify({k: {"pos": v["pos"] + [0.75], "grasped": v["grasped"],
                        "category": v["category"]} for k, v in _world.items()})


@app.route("/zones")
def zones():
    return jsonify(ZONES)


@app.route("/grasp", methods=["POST"])
def grasp():
    name = (request.json or {}).get("obj_name")
    if name not in _world:
        return jsonify({"success": False, "result": f"物体不存在: {name}"})
    if _holding["obj"]:
        return jsonify({"success": False, "result": f"手上已有 {_holding['obj']},先放下"})
    if _consume_fault("grasp_fail"):
        return jsonify({"success": False, "result": f"抓取失败(注入): 夹爪闭合但未夹住 {name}"})
    _world[name]["grasped"] = True
    _holding["obj"] = name
    return jsonify({"success": True, "result": f"成功抓取 {name}"})


@app.route("/place", methods=["POST"])
def place():
    data = request.json or {}
    name, target = data.get("obj_name"), data.get("target")
    if name not in _world:
        return jsonify({"success": False, "result": f"物体不存在: {name}"})
    if _holding["obj"] != name:
        return jsonify({"success": False, "result": f"未持有 {name},无法放置"})
    if target not in ZONES:
        return jsonify({"success": False, "result": f"未知目标区域: {target}"})
    zone = ZONES[target]
    if _consume_fault("place_miss"):
        # 放偏但报成功 —— 模拟 VLA 自认为成功,考验大脑的状态确认
        off = zone["radius"] + 0.20
        _world[name]["pos"] = [zone["pos"][0] + off, zone["pos"][1] + off]
        _world[name]["grasped"] = False
        _holding["obj"] = None
        return jsonify({"success": True, "result": f"已放置 {name} 到 {target}"})
    jitter = lambda: random.uniform(-0.03, 0.03)  # noqa: E731
    _world[name]["pos"] = [zone["pos"][0] + jitter(), zone["pos"][1] + jitter()]
    _world[name]["grasped"] = False
    _holding["obj"] = None
    return jsonify({"success": True, "result": f"已放置 {name} 到 {target}"})


@app.route("/fault", methods=["POST"])
def fault():
    data = request.json or {}
    if data.get("clear"):
        _fault["next"], _fault["rate"] = None, 0.0
    if data.get("next") in ("grasp_fail", "place_miss"):
        _fault["next"] = data["next"]
    if "rate" in data:
        _fault["rate"] = float(data["rate"])
    return jsonify({"success": True, "fault": dict(_fault)})


@app.route("/reset", methods=["POST"])
def reset():
    _reset_world()
    return jsonify({"success": True, "result": "桌面已复位(乱桌初始态)"})


# 契约兼容:桌面 demo 不涉及的端点给出明确响应
@app.route("/screenshot", methods=["POST"])
def screenshot():
    return jsonify({"success": False, "result": "desk mock 无渲染;状态确认走 /objects+/zones 几何档"}), 501


@app.route("/nav", methods=["POST"])
def nav():
    return jsonify({"success": True, "result": "定点 demo,无需导航(no-op)"})


@app.route("/base_status")
def base_status():
    return jsonify({"pos": [0.0, 0.0, 0.0], "yaw_deg": 0.0})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5008)
    args = parser.parse_args()
    print(f"[serve_desk] 定点桌面 mock 世界: http://localhost:{args.port}  "
          f"(物体 {len(_world)},区域 {list(ZONES)})")
    app.run(host="0.0.0.0", port=args.port, threaded=True)
