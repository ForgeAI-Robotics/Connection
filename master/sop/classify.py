"""物体判断器 —— demo 任务②「多模态判断需要清理/整理的物体」的框架层。

输入:结构化 SOP(demo_sop.yaml) + 场景图物体(建图组 total_scene_graph,含 qwen 属性)
输出:每个物体 → action ∈ {clean 清理, restock 饮料统计补货, organize 归位, keep 保留禁动}
      + 依据 + 置信度(规则明确=high;SOP 未覆盖的边界=low,标记待 VLM/人确认)

分两层(现在做第一层,图片到位接第二层):
  ① 规则层(本文件):class_name + qwen 属性(fullness/package/drink_type)+ SOP 关键词 → 判类。
     覆盖大部分明确 case,给出 baseline 与置信度。
  ② VLM 层(待接):low 置信 / 需看图的(饮料满空、是否垃圾、遮挡)再送 qwen-vl-max 看图裁决。
"""

import json
import os
import sys

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

ACTION_LABEL = {
    "clean": "清理(垃圾)",
    "restock": "饮料·统计补货",
    "organize": "整理归位",
    "keep": "保留·禁动",
}


def load_sop(path=None):
    with open(path or os.path.join(_HERE, "demo_sop.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _hit(text, keywords):
    return any(k.lower() in text for k in keywords)


def classify_object(cls_name, attrs, sop):
    """规则层判类。返回 (action, category, reason, confidence)。"""
    # class_name 是二次核验(mllm_verification)纠正后的权威标签,类别判断只认它。
    # taxonomy_category / grounding_prompt 是初检值,可能过时(如 screwdriver 初检成
    # mobile_phone,已纠正 class_name 但 taxonomy 没跟上),盲用会误判 → 不参与类别匹配。
    # attributes 只取 fullness 判饮料满空(这类属性不涉及类别纠正)。
    cls = (cls_name or "").lower()
    fullness = str(attrs.get("fullness", "")).lower()

    # 1) 个人物品 → 保留禁动(最高优先,SOP 硬规则)
    for k in sop["keep_in_place"]:
        if _hit(cls, k["keywords"]):
            return "keep", k["type"], f"SOP-保持原位: {k['rule']}", "high"

    # 2) 垃圾 → 清理
    for t in sop["trash"]:
        if _hit(cls, t["keywords"]):
            return "clean", t["type"], f"SOP-垃圾: {t['action']}", "high"

    # 3) 饮料:空瓶=多余饮料垃圾,满的=进补货统计
    for r in sop["restock"]:
        if _hit(cls, r["keywords"]):
            if fullness in ("empty", "空"):
                return "clean", "多余饮料瓶", f"空瓶按 SOP 丢回收区(原是{r['item']})", "medium"
            return "restock", r["item"], f"SOP-补货: <{r['min']} 去{r['source']}补齐", "high"

    # 4) 标准摆放物(笔筒/纸巾盒/盆栽)→ 归位
    for s in sop["standard_layout"]:
        kws = [s["category"].lower()]
        if s["category"] == "笔筒和笔":
            kws += ["pen", "holder", "笔筒"]
        elif s["category"] == "纸巾盒":
            kws += ["tissue box", "纸巾盒"]
        elif s["category"] == "盆栽":
            kws += ["plant", "potted", "盆栽"]
        if _hit(cls, kws):
            return "organize", s["category"], f"SOP-标准位: {s.get('rule','')}", "high"

    # 5) SOP 未覆盖(工具等边界)→ 默认保留原位 + 低置信,标记待 VLM/人确认
    return "keep", "未分类(SOP未覆盖)", "SOP无匹配,默认保持原位;建议 VLM/人确认", "low"


def _iter_scene_objects(graph):
    """场景图里挂在家具上的叶子 = 桌面物体;取 class_name + evidence.attributes。"""
    nodes = graph.get("nodes", {})
    fixture_ids = {nid for nid, n in nodes.items()
                   if n.get("parent_id") == "floor_0" and n.get("class_name") != "floor"}
    for n in nodes.values():
        if n.get("parent_id") in fixture_ids:
            attrs = (n.get("evidence") or {}).get("attributes", {}) or {}
            yield n.get("object_id") or n["node_id"], n.get("class_name", ""), attrs


def main():
    graph_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _ROOT, "serve_dream", "sample", "total_scene_graph_latest.json")
    sop = load_sop()
    graph = json.load(open(graph_path, encoding="utf-8"))

    print(f"SOP: {os.path.relpath(os.path.join(_HERE,'demo_sop.yaml'), _ROOT)}")
    print(f"场景图: {os.path.relpath(graph_path, _ROOT)}\n")
    print(f"{'物体':22}{'class':16}{'判断':16}{'类别/依据':32}{'置信'}")
    print("-" * 100)

    counts, todo = {}, []
    for name, cls, attrs in _iter_scene_objects(graph):
        action, category, reason, conf = classify_object(cls, attrs, sop)
        counts[action] = counts.get(action, 0) + 1
        flag = "  ⚠ 待确认" if conf == "low" else ""
        if conf == "low":
            todo.append(name)
        print(f"{name:22}{cls:16}{ACTION_LABEL[action]:16}{category:28}{conf}{flag}")

    print("-" * 100)
    summary = "  ".join(f"{ACTION_LABEL[a]}×{c}" for a, c in counts.items())
    print(f"汇总: {summary}")
    # 补货统计:饮料计数 vs SOP min
    restock_cnt = {}
    for name, cls, attrs in _iter_scene_objects(graph):
        a, cat, _, _ = classify_object(cls, attrs, sop)
        if a == "restock":
            restock_cnt[cat] = restock_cnt.get(cat, 0) + 1
    for r in sop["restock"]:
        have = restock_cnt.get(r["item"], 0)
        need = max(0, r["min"] - have)
        print(f"补货: {r['item']} 现有 {have}/{r['min']}"
              + (f" → 去{r['source']}补 {need} 个" if need else " → 够,无需补货"))
    if todo:
        print(f"边界待 VLM/人确认: {todo}")


if __name__ == "__main__":
    main()
