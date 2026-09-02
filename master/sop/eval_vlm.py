"""eval_vlm.py — 任务② VLM 判断准确率评测台。

把 vlm_judge 对每张 current 照片的判断,映射到 4 个技能维度
(cola/milk/pen/trash)+ personal(个人物品保护),对照人工标准答案
photos/ground_truth.json 打分,输出逐张对照表 + 各技能/整体准确率。

用法:
  python eval_vlm.py                 # 重跑 qwen-vl-max 判所有 current 图,再打分
  python eval_vlm.py --from x.json   # 用缓存的 batch 结果(见 --save)打分,不调 API
  python eval_vlm.py --save x.json   # 重跑并把结果缓存到 x.json
"""

import argparse
import glob
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from classify import load_sop              # noqa: E402
from vlm_judge import judge, _endpoint_cfg  # noqa: E402

CUR_DIR = os.path.join(_HERE, "photos", "current")
STD_DIR = os.path.join(_HERE, "photos", "standard")
GT_FILE = os.path.join(_HERE, "photos", "ground_truth.json")

SKILLS = ["cola", "milk", "pen", "trash", "personal"]
SKILL_CN = {"cola": "可乐", "milk": "牛奶", "pen": "笔筒", "trash": "垃圾", "personal": "个人"}
ACT_CN = {"O": "整理", "C": "清理", "S": "跳过", "K": "保留", "-": "无"}


def categorize(obj_name):
    """把 VLM 的物体中文名映射到技能维度;环境/容器返回 None(不评分)。"""
    o = obj_name or ""
    s = o.lower()
    # 先排除环境物 / 容器(纸巾盒=垃圾桶容器本身,不算垃圾)
    if any(k in o for k in ("盆栽", "电视", "显示器", "插线板", "插座", "线缆", "机械臂", "桌子", "纸巾盒")):
        return None
    if any(k in s for k in ("可乐", "cola", "coke")):
        return "cola"
    if "牛奶" in o or "milk" in s:
        return "milk"
    if "笔" in o:                       # 笔 / 笔筒
        return "pen"
    if (any(k in o for k in ("耳机", "耳塞", "手机", "眼镜", "白色小物")) or
            any(k in s for k in ("airpods", "earbud", "earphone", "phone"))):
        return "personal"
    if "tissue" in s or any(k in o for k in ("纸巾", "垃圾", "废纸", "包装", "空瓶", "空罐", "纸团")):
        return "trash"
    return None


def vlm_to_skill_actions(result):
    """一张图的 VLM 判断列表 → {skill: action};同维度多物体取优先级 O>C>K>S。"""
    prio = {"organize": 3, "clean": 2, "keep": 1, "skip": 0}
    code = {"organize": "O", "clean": "C", "keep": "K", "skip": "S"}
    best = {}
    for it in result:
        cat = categorize(it.get("object", ""))
        if not cat:
            continue
        act = it.get("action", "skip")
        if cat not in best or prio.get(act, 0) > prio.get(best[cat][0], -1):
            best[cat] = (act, code.get(act, "?"))
    return {k: v[1] for k, v in best.items()}


def score(results, gt):
    """results: {name: [judge...]}; gt: {name: {skill: code}}。返回逐张明细 + 汇总。"""
    per_skill = {s: [0, 0] for s in SKILLS}   # [correct, total]
    rows = []
    for name in sorted(gt):
        if name not in results:
            continue
        vlm = vlm_to_skill_actions(results[name])
        g = gt[name]
        row = {"name": name, "cells": {}}
        for s in SKILLS:
            if s not in g:                     # 该维度不在标准答案(如无个人物品)→ 不评
                # 但若 VLM 主动判了个人物品动作(误报),仍算一次错
                if s == "personal" and s in vlm and vlm[s] != "S":
                    per_skill[s][1] += 1
                    row["cells"][s] = (vlm[s], "-", False)
                continue
            gt_act = g[s]
            vlm_act = vlm.get(s, "S")           # VLM 没提这维度 = 不动作 = S
            if s == "personal":
                # 安全指标:个人物品只要"没被去动"就算对(keep 或没提=没碰=安全);
                # 只有把它判成 clean/organize(去扔/去挪)才算失败。
                ok = vlm_act not in ("C", "O")
            else:
                ok = (gt_act == vlm_act)
            per_skill[s][0] += int(ok)
            per_skill[s][1] += 1
            row["cells"][s] = (vlm_act, gt_act, ok)
        rows.append(row)
    return rows, per_skill


def print_full_table(rows):
    print("\n" + "=" * 76)
    print(f"{'照片':12}{'可乐':8}{'牛奶':8}{'笔筒':8}{'垃圾':8}{'个人':8}")
    print("-" * 76)
    for row in rows:
        line = f"{row['name']:12}"
        for s in SKILLS:
            c = row["cells"].get(s)
            line += f"{'·':8}" if not c else f"{ACT_CN.get(c[0], c[0])}{'✓' if c[2] else '✗'}   "
        print(line)
    print("-" * 76)
    print("(格子 = VLM判断 + ✓/✗对错;· = 该维度无需评)")


def skill_pcts(per_skill):
    """{skill:(correct,total)} → per-skill pct, overall pct, correct, total。"""
    out, tc, tt = {}, 0, 0
    for s in SKILLS:
        c, t = per_skill[s]; tc += c; tt += t
        out[s] = (100 * c / t) if t else None
    return out, (100 * tc / tt if tt else 0), tc, tt


def summarize(runs, n, model="", avg_latency=None):
    """runs: [(pcts_dict, overall), ...] → 多次汇总表(输出到 bash)。"""
    print("\n" + "=" * 62)
    lat = f",平均推理 {avg_latency:.1f}s/次" if avg_latency else ""
    print(f"VLM 判断准确率汇总({model or 'VLM'},{n} 次{lat})")
    print("=" * 62)
    print("技能".ljust(6) + "".join(f"第{i+1}次".rjust(8) for i in range(n)) +
          "平均".rjust(9) + "区间".rjust(11))
    print("-" * 62)
    for s in SKILLS:
        vals = [r[0][s] for r in runs if r[0][s] is not None]
        if not vals:
            continue
        cells = "".join((f"{r[0][s]:.0f}%".rjust(8) if r[0][s] is not None else "—".rjust(8)) for r in runs)
        print(f"{SKILL_CN[s]:6}{cells}{f'{sum(vals)/len(vals):.0f}%':>9}{f'{min(vals):.0f}-{max(vals):.0f}%':>11}")
    print("-" * 62)
    ov = [r[1] for r in runs]
    cells = "".join(f"{v:.0f}%".rjust(8) for v in ov)
    print(f"{'整体':5}{cells}{f'{sum(ov)/len(ov):.0f}%':>9}{f'{min(ov):.0f}-{max(ov):.0f}%':>11}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", help="用缓存结果打分,不调 API(单次dict / 多次list 都行)")
    ap.add_argument("--save", help="把原始判断缓存到该 json(多次=list,便于改 GT 后离线重算)")
    ap.add_argument("--runs", type=int, default=1, help="重跑 N 次取平均(VLM 有温度,看稳定性)")
    args = ap.parse_args()

    if not os.path.isfile(GT_FILE):
        print(f"缺标准答案: {GT_FILE}"); return
    gt = {k: v for k, v in json.load(open(GT_FILE, encoding="utf-8")).items() if not k.startswith("_")}

    # 离线打分路径(不调 API):支持单次 dict 或 多次 list
    if args.src:
        raw = json.load(open(args.src, encoding="utf-8"))
        run_dicts = raw if isinstance(raw, list) else [raw]
        runs = []
        for rd in run_dicts:
            results = {n: v["judge"] for n, v in rd.items() if "judge" in v}
            rows, per_skill = score(results, gt)
            pcts, overall, tc, tt = skill_pcts(per_skill)
            if len(run_dicts) == 1:
                print_full_table(rows)
                print(f"\n{'技能':10}{'准确率':12}\n" + "-" * 30)
                for s in SKILLS:
                    c, t = per_skill[s]
                    print(f"{SKILL_CN[s]:10}{(f'{c}/{t} = {pcts[s]:.0f}%' if t else '—'):12}")
                print("-" * 30 + f"\n{'整体':10}{tc}/{tt} = {overall:.0f}%")
            runs.append((pcts, overall))
        if len(run_dicts) > 1:
            summarize(runs, len(run_dicts))
        return

    sop = load_sop()
    imgs = sorted(glob.glob(os.path.join(CUR_DIR, "*.heic")) +
                  glob.glob(os.path.join(CUR_DIR, "*.HEIC")))

    runs, raw_runs, times = [], [], []
    for i in range(args.runs):
        print(f"\n=== 第 {i+1}/{args.runs} 次 ===")
        cache = {}
        for img in imgs:
            name = os.path.splitext(os.path.basename(img))[0]
            sys.stdout.write(f"  [{name}] "); sys.stdout.flush()
            t0 = time.perf_counter()
            r, err = judge(CUR_DIR, STD_DIR, sop, current_img=img)
            dt = time.perf_counter() - t0
            times.append(dt)
            print(f"ok {dt:.1f}s" if r else f"失败: {err}")
            if r:
                cache[name] = {"judge": r}
        raw_runs.append(cache)
        results = {n: v["judge"] for n, v in cache.items() if "judge" in v}
        rows, per_skill = score(results, gt)
        if args.runs == 1:
            print_full_table(rows)
        pcts, overall, tc, tt = skill_pcts(per_skill)
        runs.append((pcts, overall))
        print(f"  → 本次整体 {overall:.0f}% ({tc}/{tt})")

    if args.save:
        json.dump(raw_runs if args.runs > 1 else raw_runs[0],
                  open(args.save, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[缓存 → {args.save}]")

    avg_lat = sum(times) / len(times) if times else None
    summarize(runs, args.runs, model=_endpoint_cfg()["model"], avg_latency=avg_lat)


if __name__ == "__main__":
    main()
