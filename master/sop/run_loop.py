"""大脑闭环 driver —— 定点桌面 demo(任务③「状态确认」+ 失败重规划 的核心实现)。

闭环:
  观测(/objects+/zones) → 判断(哪些技能需要执行,SOP: 先清垃圾;个人物品记录基线)
   → 逐技能执行(skill_executor,VLA 黑盒) → **每技能后重新观测做状态确认**(不信 VLA 的
  自我报告 —— place_miss 注入时 VLA 报成功但物体在区外,靠这里逮住) → 失败 →
  重新观测定位问题 → 决策(重试≤MAX_RETRY / 跳过并记录) → 终局判定(全类别达标 +
  个人物品未被挪动) → trace 存档(任务④事后反思的输入)。

用法:
  python master/sop/run_loop.py            # 需 serve_desk 在跑 + active_backend: desk
"""

import json
import math
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from skill_executor import (SKILLS, execute_skill, get_zones,   # noqa: E402
                            in_zone, pending_objects)
_ROOT = os.path.dirname(os.path.dirname(_HERE))
from robot_api import client as api                              # noqa: E402

MAX_RETRY = 2
# SOP 顺序:先清垃圾(避免遮挡),再整理各类;(关电视等真机技能到位再加)
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
    from vlm_judge import judge as _vlm_judge
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


def observe():
    return api.get_objects(), get_zones()


def verify_skill(skill_name, objects, zones):
    """状态确认(几何档):该技能类别的物体是否全部在目标区。不采信 VLA 自报。"""
    left = pending_objects(skill_name, objects, zones)
    if not left:
        return True, "该类别全部在位"
    spec = SKILLS[skill_name]
    zone = zones[spec["zone"]]
    detail = []
    for obj in left:
        p = objects[obj]["pos"]
        d = math.hypot(p[0] - zone["pos"][0], p[1] - zone["pos"][1])
        detail.append(f"{obj} 距 {spec['zone']} {d:.2f}m(>{zone['radius']:.2f})")
    return False, "; ".join(detail)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--vlm", action="store_true",
                    help="用 qwen 看 photos/ 真照片决定技能集(否则用 serve_desk 几何档)")
    args = ap.parse_args()

    print("=" * 64)
    print("大脑闭环 · 定点桌面整理(观测→判断→执行→确认→重规划)")
    print("=" * 64)

    objects, zones = observe()
    # SOP 保护基线:个人物品初始位置(终局验"没被动过")
    protected = {k: list(v["pos"]) for k, v in objects.items()
                 if v.get("category") == PROTECTED_CATEGORY}
    if protected:
        print(f"SOP 保护(禁动): {list(protected)}")

    # 判断:哪些技能需要执行
    from classify import load_sop
    sop = load_sop()
    no_skill = []
    if args.vlm:
        print("判断源: VLM(qwen-vl-max 看 photos/current + standard)...")
        needed, no_skill, keeps, vlm_result = needed_skills_from_vlm(sop)
        print("  VLM 判断: " + ", ".join(
            f"{it['object']}×{it.get('count','?')}→{it['action']}" for it in vlm_result))
        if no_skill:
            print(f"  ⚠ VLM 判要处理但 VLA 无对应技能(跳过/需人): {no_skill}")
    else:
        print("判断源: 几何档(serve_desk /objects)")
        needed = [s for s in SKILL_ORDER if pending_objects(s, objects, zones)]
    print("判断 → 需执行技能: " + (", ".join(SKILLS[s]["label"] for s in needed) or "无(桌面已达标)"))

    trace = {"task": "定点桌面整理", "started_at": datetime.now().isoformat(),
             "judge_source": "vlm" if args.vlm else "geometric",
             "needed_skills": needed, "no_skill_gap": no_skill,
             "skills": [], "protected": list(protected)}

    for skill in needed:
        label = SKILLS[skill]["label"]
        print(f"\n▶ 技能: {label}")
        rec = {"skill": skill, "attempts": [], "final": None}
        for attempt in range(1, MAX_RETRY + 2):
            exec_r = execute_skill(skill)
            objects, zones = observe()                      # 重新观测
            ok, detail = verify_skill(skill, objects, zones)  # 状态确认
            att = {"n": attempt, "exec": exec_r, "verify_ok": ok, "verify_detail": detail}
            vla_said = "VLA报成功" if exec_r.get("success") else f"VLA报失败({exec_r.get('fail_stage')}: {exec_r.get('fail_reason')})"
            print(f"  尝试{attempt}: {vla_said} | 大脑确认: {'✓ 达标' if ok else '✗ ' + detail}")
            if ok:
                att["decision"] = "done"
                rec["attempts"].append(att)
                rec["final"] = "success"
                break
            att["decision"] = "retry" if attempt <= MAX_RETRY else "skip"
            rec["attempts"].append(att)
            if attempt <= MAX_RETRY:
                print(f"  ↻ 重规划: 基于重新观测({detail}),重试(第{attempt}/{MAX_RETRY}次)")
            else:
                print(f"  ⤼ 重试{MAX_RETRY}次仍未达标 → 跳过并记录(进事后反思)")
                rec["final"] = "skipped_after_retries"
        trace["skills"].append(rec)

    # 终局判定:全类别达标 + 个人物品未被挪动
    print("\n" + "=" * 64)
    objects, zones = observe()
    all_ok = True
    for s in needed:   # 只验实际执行的技能;skip/keep 的大脑已判已就位,不纳入终局
        ok, detail = verify_skill(s, objects, zones)
        all_ok &= ok
        print(f"终局 · {SKILLS[s]['label']}: {'✓' if ok else '✗ ' + detail}")
    if not needed:
        print("终局 · 无需执行的技能(全部已就位/保留)")
    prot_ok = True
    for k, p0 in protected.items():
        p1 = objects[k]["pos"]
        moved = math.hypot(p1[0] - p0[0], p1[1] - p0[1]) > 0.05
        prot_ok &= not moved
        print(f"终局 · 保护 {k}: {'✓ 未动' if not moved else '✗ 被挪动了(违规!)'}")
    verdict = all_ok and prot_ok
    print(f"\n任务{'完成 ✅' if verdict else '未完成 ❌(见上;失败项已记录进 trace)'}")

    trace["final_check"] = {"all_zones_ok": all_ok, "protection_ok": prot_ok, "verdict": verdict}
    out = os.path.join(_HERE, "last_trace.json")
    json.dump(trace, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[trace → {os.path.relpath(out, _ROOT)}](任务④事后反思的输入)")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
