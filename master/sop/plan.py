"""SOP → 整理计划 —— demo 第 4 步「具身大脑读 SOP 生成整理计划」的大脑侧。

判断结果(classify) + SOP 顺序规则 → 有序子任务链:
  阶段1 清理垃圾 → 阶段2 饮料补货(先统计再取) → 阶段3 归位 → 阶段4 关电视(最后)
个人物品保留不动;SOP 未覆盖的边界物体列为"待确认",不自动动。

子任务格式对齐 master 骨架与 VLA/导航对接手册的区域名(trash_bin/tea_room/drinks_area/*_spot),
生成的 subtask_list 可直接下发给已验证的「计划→导航下发」链路。
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from classify import load_sop, classify_object, _iter_scene_objects  # noqa: E402

# 目标区域(与 docs/对接手册 的区域命名约定一致)
TRASH_BIN = "trash_bin"
DRINKS_AREA = "drinks_area"
TEA_ROOM = "tea_room"


def build_plan(graph, sop):
    items = []
    for name, cls, attrs in _iter_scene_objects(graph):
        action, cat, reason, conf = classify_object(cls, attrs, sop)
        items.append({"obj": name, "cls": cls, "action": action, "cat": cat, "conf": conf})

    subtasks, plan_text = [], []

    def add(subtask):
        subtasks.append({"robot_name": "FQrobot", "subtask": subtask,
                         "subtask_order": len(subtasks) + 1})

    # 阶段1 清理垃圾(SOP: 清理前移遮挡但不动个人物品 —— 个人物品本就 keep,不在此列)
    trash = [it for it in items if it["action"] == "clean"]
    if trash:
        plan_text.append("阶段1 · 清理垃圾")
        for it in trash:
            add(f"搜索并抓取 {it['obj']}")
            add(f"放置 {it['obj']} 到 {TRASH_BIN}")
            plan_text.append(f"    抓 {it['obj']}({it['cat']}) → {TRASH_BIN}   [执行后确认:桌面已无此物]")

    # 阶段2 饮料补货(SOP: 先统计当前数量,不足去茶水间补)
    restock_cnt = {}
    for it in items:
        if it["action"] == "restock":
            restock_cnt[it["cat"]] = restock_cnt.get(it["cat"], 0) + 1
    plan_text.append("阶段2 · 饮料补货(先统计再取,避免重复)")
    for r in sop["restock"]:
        have = restock_cnt.get(r["item"], 0)
        need = max(0, r["min"] - have)
        plan_text.append(f"    {r['item']}: 现有 {have}/{r['min']}"
                         + (f" → 去{r['source']}补 {need} 个" if need else " → 够,跳过"))
        if need:
            add(f"导航到 {TEA_ROOM}")
            for _ in range(need):
                add(f"搜索并抓取 {r['item']}")
                add(f"放置 {r['item']} 到 {DRINKS_AREA}")

    # 阶段3 归位(笔筒/纸巾盒/盆栽放标准位)
    organize = [it for it in items if it["action"] == "organize"]
    if organize:
        plan_text.append("阶段3 · 归位到标准位置")
        for it in organize:
            spot = f"{it['cat']}_spot"
            add(f"搜索并抓取 {it['obj']}")
            add(f"放置 {it['obj']} 到 {spot}")
            plan_text.append(f"    {it['obj']}({it['cat']}) → {spot}")
    else:
        plan_text.append("阶段3 · 归位:本场景无需归位物体")

    # 阶段4 关电视(SOP learned_rule: 放在整理完成后最后一步)
    plan_text.append("阶段4 · 关闭电视(整理完成后最后一步)")
    add("关闭电视")

    keep = [it["obj"] for it in items if it["action"] == "keep" and it["conf"] != "low"]
    todo = [it["obj"] for it in items if it["conf"] == "low"]
    return plan_text, subtasks, keep, todo


def main():
    graph_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _ROOT, "serve_dream", "sample", "total_scene_graph_latest.json")
    sop = load_sop()
    graph = json.load(open(graph_path, encoding="utf-8"))
    plan_text, subtasks, keep, todo = build_plan(graph, sop)

    print("=" * 66)
    print("整理计划(SOP + 场景图判断 → 有序子任务)")
    print("=" * 66)
    for line in plan_text:
        print(line)
    print(f"\n保留不动(个人物品): {keep}")
    print(f"待人/VLM 确认(SOP 未覆盖,不自动动): {todo}")

    print("\n" + "=" * 66)
    print(f"可执行子任务序列(subtask_list,共 {len(subtasks)} 步,可直接下发)")
    print("=" * 66)
    for st in subtasks:
        print(f"  {st['subtask_order']:2}. {st['subtask']}")

    out = os.path.join(_HERE, "last_plan.json")
    json.dump({"subtask_list": subtasks, "keep": keep, "need_confirm": todo},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[已存 subtask_list → {os.path.relpath(out, _ROOT)}]")


if __name__ == "__main__":
    main()
