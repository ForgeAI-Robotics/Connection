"""接待 demo 的【双记忆】结构 —— 对齐 HarnessVLA(见 memory harnessvla-paper-alignment)。

两层记忆(8/15 接入"一次人类示范 → 可复用记忆";现在也可从 reception_loop 自跑的 trace bootstrap):
  · Task Specific Memory(每个任务一份):
      - skeleton(JSONL):【参数化的原语解法骨架】,不是开环轨迹。接待原语本来就语义化
        (walk 茶水间 / pick 可乐),天然不含坐标 → 天然可 re-ground;要参数化的是【数量】(按现场
        人数重算,不固定示范时的 3 罐)。
      - summary(JSON):{task, success, strategy, avoid, recovery_decisions, failure_modes}
        —— 记"为什么这么解 / 要避免什么 / 失败怎么恢复"。
  · Global Memory(跨任务一份):success_rules + failure_models(含 empty-grasp / false-success)。

迭代构建(对齐论文):每次执行后,把可恢复失败的处理写进 summary、把失败模式并进 Global;
更短更可靠的 skeleton 可替换旧的。部署时 retrieve() 取回骨架 + 摘要 + 全局知识供 planner re-ground。

用法:python reception_memory.py   # 从 last_reception_trace.json 生成示例 + seed 全局记忆 + 演示 retrieve
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
MEM_DIR = os.path.join(_HERE, "reception_memory")


# ============ Task Specific Memory ============

# 恢复策略库(该任务已知的失败→恢复;示范/多次执行累积;fail_at → (失败模式, 恢复决策))
_RECOVERY = {
    "pick":       ("grasp_fail",   "原地重试 pick(不重走)"),
    "walk→茶水间": ("walk_blocked", "重试或绕行,超上限停下上报"),
    "walk→会议室": ("walk_blocked", "重试或绕行,超上限停下上报"),
    "place":      ("place_miss",   "可乐已脱手→重取重放;若只是间隔/朝向不良→原地重摆(不重取)"),
}


def _task_skeleton():
    """接待补货的【任务级解法骨架】—— 语义原语 + 参数化数量(不含坐标)。"""
    return [
        {"action": "turn_on_lights", "room": "会议室"},
        {"action": "scan", "room": "会议室", "observe": ["可乐数", "办公椅"]},
        {"action": "scan", "room": "茶水间", "observe": ["可乐库存"]},
        {"action": "compute_gap", "need": "headcount × per_person", "have": "会议室现有可乐"},
        {"action": "walk", "to": "茶水间"},
        {"action": "pick", "target": "可乐", "at": "茶水间", "verify": "手上有可乐(empty-grasp 检测)"},
        {"action": "walk", "to": "会议室"},
        {"action": "place", "target": "可乐", "to": "会议室", "verify": "桌上 +1(false-success 检测)"},
        {"action": "recount", "room": "会议室", "loop_until": "gap == 0"},
    ]


def build_task_memory(trace, name="reception_restock"):
    """从 reception trace(或人类示范)抽 Task Specific Memory:参数化骨架 + 语义摘要。"""
    recovery = {}
    for _, (k, v) in _RECOVERY.items():
        recovery[k] = v      # 全集(该任务已知恢复策略);trace 里触发过的标 verified
    triggered = {att.get("fail_at") for rnd in trace.get("rounds", [])
                 for att in rnd.get("attempts", []) if att.get("fail_at")}
    verified = sorted({_RECOVERY[f][0] for f in triggered if f in _RECOVERY})

    summary = {
        "task": "会议接待饮料补货",
        "success": trace.get("final_check", {}).get("verdict"),
        "strategy": "先扫描数缺口,再【一个一个】补:walk→pick→walk→place,每步重新观测确认,补完重新数直到够",
        "avoid": ["不复用示范时的具体数量/坐标,按现场人数和现有量重新算",
                  "不采信执行模块自报,每步重新观测确认(pick/walk/place)"],
        "recovery_decisions": recovery,
        "verified_this_run": verified,          # 本次执行真实触发并验证过的恢复
        "failure_modes": sorted({v[0] for v in _RECOVERY.values()}),
    }

    os.makedirs(MEM_DIR, exist_ok=True)
    skeleton = _task_skeleton()
    with open(os.path.join(MEM_DIR, f"task_{name}.jsonl"), "w", encoding="utf-8") as f:
        for row in skeleton:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    json.dump(summary, open(os.path.join(MEM_DIR, f"task_{name}.summary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return skeleton, summary


# ============ Global Memory ============

_GLOBAL_SEED = {
    "success_rules": [
        "接触密集阶段(pick/place)交给 VLA,非接触(walk/staging/release)用确定性原语",
        "补货前先统计现有再算缺口,避免多取或少取",
        "一个一个补,每罐补完重新数,数够即停(不预设次数)",
    ],
    "failure_models": [
        {"name": "empty_grasp", "detect": "pick 后重新观测手上没物(is_object_grasped=False)",
         "recover": "重新定位 + 重新摆位再抓"},
        {"name": "false_success", "detect": "place 自报成功但重数目标区没增加",
         "recover": "重取重放,不采信自报"},
        {"name": "walk_blocked", "detect": "walk 后位姿没到目标房间(办公椅挡路)",
         "recover": "重试或绕行,超上限停下上报"},
        {"name": "pose_bad", "detect": "到位但间隔/朝向不合格",
         "recover": "原地重摆(不重取)"},
    ],
}


def load_global():
    p = os.path.join(MEM_DIR, "global_memory.json")
    if os.path.isfile(p):
        return json.load(open(p, encoding="utf-8"))
    return {"success_rules": [], "failure_models": []}


def update_global_memory(new_rules=None, new_failure_models=None, seed=False):
    """把 reflect 的 success rules / failure models 并进全局记忆(去重)。seed=首次灌入种子。"""
    g = load_global()
    if seed and not g["success_rules"] and not g["failure_models"]:
        g = {"success_rules": list(_GLOBAL_SEED["success_rules"]),
             "failure_models": [dict(m) for m in _GLOBAL_SEED["failure_models"]]}
    for r in (new_rules or []):
        if r not in g["success_rules"]:
            g["success_rules"].append(r)
    have = {m["name"] for m in g["failure_models"]}
    for m in (new_failure_models or []):
        if m.get("name") not in have:
            g["failure_models"].append(m)
            have.add(m["name"])
    os.makedirs(MEM_DIR, exist_ok=True)
    json.dump(g, open(os.path.join(MEM_DIR, "global_memory.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return g


def failure_model(name):
    """按名取标准 failure_model(reflect 反思时把触发的失败模式并进 Global 用)。"""
    for m in _GLOBAL_SEED["failure_models"]:
        if m["name"] == name:
            return dict(m)
    return None


# ============ 部署检索(planner re-ground 用)============

def retrieve(name="reception_restock"):
    """部署时取回:任务骨架 + 摘要 + 全局知识。planner 用骨架结构,数量/目标从现场观测重新 ground。"""
    ts = os.path.join(MEM_DIR, f"task_{name}.jsonl")
    sm = os.path.join(MEM_DIR, f"task_{name}.summary.json")
    skeleton = [json.loads(l) for l in open(ts, encoding="utf-8")] if os.path.isfile(ts) else []
    summary = json.load(open(sm, encoding="utf-8")) if os.path.isfile(sm) else {}
    return {"skeleton": skeleton, "summary": summary, "global": load_global()}


if __name__ == "__main__":
    trace_path = os.path.join(_HERE, "last_reception_trace.json")
    trace = json.load(open(trace_path, encoding="utf-8")) if os.path.isfile(trace_path) else {}

    print("=== ① 生成 Task Specific Memory(从 last_reception_trace bootstrap)===")
    skeleton, summary = build_task_memory(trace)
    print(f"  skeleton {len(skeleton)} 步 → reception_memory/task_reception_restock.jsonl")
    print(f"  summary.strategy: {summary['strategy']}")
    print(f"  summary.failure_modes: {summary['failure_modes']}")
    print(f"  本次验证过的恢复: {summary['verified_this_run'] or '(无故障)'}")

    print("\n=== ② seed Global Memory ===")
    g = update_global_memory(seed=True)
    print(f"  success_rules {len(g['success_rules'])} 条")
    print(f"  failure_models: {[m['name'] for m in g['failure_models']]}")

    print("\n=== ③ retrieve(部署时 planner 取用,数量/目标现场 re-ground)===")
    r = retrieve()
    print(f"  骨架 {len(r['skeleton'])} 步 + 摘要(strategy/avoid/recovery)+ 全局(rules {len(r['global']['success_rules'])}"
          f" / failure_models {len(r['global']['failure_models'])})")
