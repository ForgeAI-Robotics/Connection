"""Desk relation planning; no execution or recovery loop."""
import os
import yaml
from connection.paths import workspace_root
from connection.brain.skills.desk import SKILLS
_HERE = str(workspace_root() / "master/sop")

SKILL_ORDER = ["clean_trash", "tidy_milk", "tidy_cola", "tidy_penholder"]
PROTECTED_CATEGORY = "personal"   # SOP: 手机等个人物品禁动

# VLM 照片判断 → VLA 技能:照片里的类别映射到 4 个技能;映射不到 = VLA 无此能力
CATEGORY_SKILL = {"trash": "clean_trash", "cola": "tidy_cola",
                  "milk": "tidy_milk", "penholder": "tidy_penholder"}


def _obj_category(name):
    n = (name or "").lower()
    if any(k in name for k in ("纸巾", "垃圾", "废纸")) or "trash" in n or "tissue" in n:
        return "trash"
    if "可乐" in name or "cola" in n or "coke" in n:
        return "cola"
    if "牛奶" in name or "milk" in n:
        return "milk"
    if "笔筒" in name or "pen" in n:
        return "penholder"
    return None


def needed_skills_from_vlm(sop):
    """qwen 看 photos/current+standard → 判断 → 映射成需执行的技能集。
    返回 (有序技能列表, 无对应技能的物体[(名,动作)], 保留物体, 原始VLM结果)。"""
    from connection.brain.adapters.vlm import judge as _vlm_judge
    result, err = _vlm_judge(os.path.join(_HERE, "photos", "current"),
                             os.path.join(_HERE, "photos", "standard"), sop)
    if not result:
        raise RuntimeError(f"VLM 判断失败: {err}")
    skills, no_skill, keeps = set(), [], []
    for it in result:
        act = it.get("action")
        if act in ("keep", "skip"):   # skip=关系没变已就位;keep=个人物品:都不生成技能
            keeps.append(f"{it.get('object')}({'已就位' if act == 'skip' else '保留'})"); continue
        sk = CATEGORY_SKILL.get(_obj_category(it.get("object", "")))
        (skills.add(sk) if sk else no_skill.append((it.get("object"), act)))
    ordered = [s for s in SKILL_ORDER if s in skills]
    return ordered, no_skill, keeps, result


def plan_relations(task):
    """demo 规划:vlm_judge 关系判断(当前图 vs 标准图,关系变了才动) → 技能 subtask_list。
    复用 master/sop 关系对比闭环。返回 {reasoning_explanation, subtask_list}。"""
    with open(os.path.join(_HERE, "demo_sop.yaml"), encoding="utf-8") as handle:
        sop = yaml.safe_load(handle)
    needed, no_skill, keeps, vlm_result = needed_skills_from_vlm(sop)
    judged = "; ".join(
        f"{it.get('object')}[{it.get('current_rel', '')}→{it.get('goal_rel', '')}]:{it.get('action')}"
        for it in vlm_result)
    reasoning = (
        f"读 SOP + 对比桌面当前图/标准图,按物体空间关系判断(关系变了才动): {judged}。"
        f" → 需执行: {[SKILLS[s]['label'].split('(')[0] for s in needed]};"
        f" 已就位/保留跳过: {keeps}。"
        + (f" 判断需处理但无对应技能(需人): {no_skill}。" if no_skill else ""))
    subtask_list = [
        {"robot_name": "FQrobot",
         "subtask": SKILLS[s]["label"].split("(")[0].strip(),
         "subtask_order": i + 1}
        for i, s in enumerate(needed)]
    return {"reasoning_explanation": reasoning, "subtask_list": subtask_list}
