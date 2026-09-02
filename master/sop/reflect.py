"""事后反思模块 —— demo 任务④:执行 trace → 总结异常 → 生成新版 SOP。

双模(自动检测 trace 结构):
  · 桌面整理(trace.skills):谎报被逮 / 抓取失败恢复 / 重试耗尽跳过 / 个人物品被挪动。
  · 会议接待(trace.rounds,带循环补货):放偏谎报被逮 / 摆放姿态不良 / 抓取失败恢复 /
    导航受阻 / 终局摆放复核不合格 / 补货中止。

两档(在线失败自动降级):结构化事实提取 → deepseek 生成新经验(LLM 档) / 模板经验(降级档)。
输出:反思报告(stdout)+ 新版 SOP(learned_rules 追加 [v2·auto] 条目,不覆盖原 SOP)。

用法:
  python reflect.py                              # 桌面(默认读 last_trace.json)
  python reflect.py last_reception_trace.json    # 接待(自动识别 rounds → reception 模式)
"""

import json
import os
import re
import sys
import urllib.request
from datetime import date

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

SKILL_LABEL = {
    "clean_trash": "清理垃圾", "tidy_milk": "整理牛奶",
    "tidy_cola": "整理可乐", "tidy_penholder": "整理笔筒",
}


# ---------------- 结构化反思:桌面整理 ----------------

def structural_findings(trace):
    findings = []
    for s in trace.get("skills", []):
        label = SKILL_LABEL.get(s["skill"], s["skill"])
        lies = [a for a in s["attempts"] if a["exec"].get("success") and not a["verify_ok"]]
        grasp_fails = [a for a in s["attempts"]
                       if not a["exec"].get("success") and a["exec"].get("fail_stage") == "grasp"]
        if lies:
            findings.append({
                "type": "执行报告与实际不符",
                "detail": f"「{label}」执行模块报告成功,但状态确认发现未达标"
                          f"({lies[0]['verify_detail']}),重新执行后恢复",
            })
        if grasp_fails:
            recovered = s["final"] == "success"
            findings.append({
                "type": "抓取失败" + ("(重试后恢复)" if recovered else "(未恢复)"),
                "detail": f"「{label}」{grasp_fails[0]['exec'].get('fail_reason')}",
            })
        if s["final"] == "skipped_after_retries":
            findings.append({
                "type": "漏整理/漏清理",
                "detail": f"「{label}」重试 {len(s['attempts']) - 1} 次仍未达标,被跳过",
            })
    fc = trace.get("final_check", {})
    if not fc.get("protection_ok", True):
        findings.append({"type": "误操作(个人物品被挪动)",
                         "detail": "终局复核发现受保护的个人物品位置变化"})
    if not findings:
        findings.append({"type": "顺利完成", "detail": "全部技能一次通过,无异常"})
    return findings


# ---------------- 结构化反思:会议接待补货 ----------------

def reception_findings(trace):
    """遍历所有轮/尝试/步,提取补货闭环里的异常事实。"""
    findings = []
    place_lies, label_bad, grasp_fails, walk_blocks = [], [], [], []
    for rnd in trace.get("rounds", []):
        for att in rnd.get("attempts", []):
            for st in att.get("steps", []):
                step, exec_r, ok, det = st["step"], st["exec"], st["verify_ok"], st["verify_detail"]
                if step.startswith("place") and exec_r.get("success") and not ok:
                    (label_bad if ("朝向" in det or "间隔" in det) else place_lies).append(det)
                elif step.startswith("pick") and not exec_r.get("success"):
                    grasp_fails.append(exec_r.get("fail_reason"))
                elif step.startswith("walk") and not exec_r.get("success"):
                    walk_blocks.append(exec_r.get("fail_reason"))
    if place_lies:
        findings.append({"type": "执行报告与实际不符(放偏谎报)",
                         "detail": f"摆放时执行模块报成功,重新计数发现{place_lies[0]}"})
    if label_bad:
        findings.append({"type": "摆放姿态不良(朝向/间隔)",
                         "detail": f"可乐{label_bad[0]}"})
    if grasp_fails:
        findings.append({"type": "抓取失败(重试后恢复)", "detail": f"取可乐时{grasp_fails[0]}"})
    if walk_blocks:
        findings.append({"type": "导航受阻(重试后恢复)", "detail": f"行走时{walk_blocks[0]}"})
    fc = trace.get("final_check", {})
    if not fc.get("layout_ok", True):
        findings.append({"type": "终局摆放复核不合格",
                         "detail": f"终局逐罐复核发现 {fc.get('bad_slots')} 罐间隔/朝向不合格"})
    if fc.get("aborted"):
        findings.append({"type": "补货中止", "detail": "补货循环因重试超限/缺货中途停下上报"})
    if not findings:
        findings.append({"type": "顺利完成", "detail": "全部步骤一次通过,无异常"})
    return findings


_TEMPLATE_RULES = {
    # —— 桌面整理 ——
    "执行报告与实际不符": "不采信执行模块的成功报告:每个技能执行后必须重新观测并确认实际状态,未达标立即重做。",
    "抓取失败(重试后恢复)": "抓取失败(如夹爪未夹住)后重试通常有效;连续 2 次失败应跳过该物体并上报,不阻塞后续任务。",
    "漏整理/漏清理": "被跳过的技能要在任务报告中显式列出,并在下次执行前优先检查该类物体的状态与可达性。",
    "误操作(个人物品被挪动)": "执行任何技能前把个人物品列入禁动清单,终局必须复核其位置未变。",
    # —— 会议接待补货 ——
    "执行报告与实际不符(放偏谎报)": "摆放后不采信执行模块的成功报告,须重新数会议室数量确认真正入位,未入位立即重取重放。",
    "摆放姿态不良(朝向/间隔)": "标签朝向/间隔属于摆放合格标准;姿态不良应【原地重新摆正】,而非再取一罐(避免多补掩盖问题)。",
    "导航受阻(重试后恢复)": "行走被可移动障碍(办公椅)挡住时重试或规避;超限停下上报,不硬闯。",
    "终局摆放复核不合格": "终局复核不能只看数量,要逐罐核对间隔与朝向,防止姿态问题被多补数量掩盖成假完成。",
    "补货中止": "茶水间缺货或重试耗尽导致补货中止时,显式上报已补数量与缺口,交人工接手。",
}


def template_rules(findings):
    return [_TEMPLATE_RULES[f["type"]] for f in findings if f["type"] in _TEMPLATE_RULES]


# ---------------- LLM 反思(deepseek,失败降级) ----------------

def _api_key():
    key = os.environ.get("CLOUD_API_KEY")
    if key:
        return key
    env_path = os.path.join(_ROOT, ".env")
    if os.path.isfile(env_path):
        for line in open(env_path, encoding="utf-8"):
            m = re.match(r"\s*CLOUD_API_KEY\s*=\s*(\S+)", line)
            if m:
                return m.group(1).strip().strip('"')
    return None


def llm_rules(findings, existing_rules, task_desc="机器人刚完成一次桌面整理任务"):
    key = _api_key()
    if not key:
        return None, "无 CLOUD_API_KEY"
    facts = "\n".join(f"- [{f['type']}] {f['detail']}" for f in findings)
    known = "\n".join(f"- {r}" for r in existing_rules)
    prompt = f"""你是机器人的「事后反思」模块。{task_desc},以下是执行记录的事实摘要和既有经验规则。
请基于事实总结 1-3 条【新的】经验规则:
- 每条一句话、具体可执行、面向未来的任务执行(风格与既有规则一致)
- 不得与既有规则重复或近似
- 只基于事实,不编造未发生的情况
- 直接输出规则,每行一条,不要编号、解释或其他内容

【本次执行事实】
{facts}

【既有经验规则】
{known}"""
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps({
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2, "max_tokens": 300,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content = json.loads(r.read())["choices"][0]["message"]["content"]
        rules = [ln.strip("-• ").strip() for ln in content.strip().splitlines() if ln.strip()]
        return [r for r in rules if len(r) > 8], None
    except Exception as exc:
        return None, str(exc)


# ---------------- 新版 SOP ----------------

def write_sop_v2(sop, new_rules, out_name="demo_sop_v2.yaml"):
    sop2 = dict(sop)
    tag = f"[v2·auto {date.today().isoformat()}]"
    sop2["learned_rules"] = list(sop.get("learned_rules", [])) + [f"{tag} {r}" for r in new_rules]
    out = os.path.join(_HERE, out_name)
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(sop2, f, allow_unicode=True, sort_keys=False, width=100)
    return out


# ---------------- 反思 driver(桌面 / 接待共用) ----------------

def run_reflection(trace, findings_fn, sop_filename, out_name, task_desc):
    """返回 (findings, new_rules, source, out_path)。stdout 打印反思报告。"""
    sop = yaml.safe_load(open(os.path.join(_HERE, sop_filename), encoding="utf-8"))
    existing = sop.get("learned_rules", [])

    print("=" * 62)
    print("事后反思报告(任务④)")
    print("=" * 62)
    findings = findings_fn(trace)
    for f in findings:
        print(f"  · [{f['type']}] {f['detail']}")

    rules, err = llm_rules(findings, existing, task_desc)
    source = "LLM(deepseek)"
    if not rules:
        rules = template_rules(findings)
        source = f"模板档(LLM 不可用: {err})"
    new_rules = [r for r in rules if not any(r[:12] in ex or ex[:12] in r for ex in existing)]

    print(f"\n新增经验规则(来源: {source}):")
    if not new_rules:
        print("  (无新增——本次执行未暴露既有规则之外的问题)")
        return findings, [], source, None
    for r in new_rules:
        print(f"  + {r}")
    out = write_sop_v2(sop, new_rules, out_name)
    print(f"\n新版 SOP → {os.path.relpath(out, _ROOT)}"
          f"(learned_rules {len(existing)}→{len(existing) + len(new_rules)} 条)")
    return findings, new_rules, source, out


# 反思发现的失败类型 → Global Memory 的标准 failure_model 名
_FINDING_FAILURE_NAME = {
    "执行报告与实际不符(放偏谎报)": "false_success",
    "摆放姿态不良(朝向/间隔)": "pose_bad",
    "终局摆放复核不合格": "pose_bad",
    "抓取失败(重试后恢复)": "empty_grasp",
    "导航受阻(重试后恢复)": "walk_blocked",
}


def _update_global_from_reflection(findings, new_rules):
    """双 memory 闭环:反思产出 → Global Memory(new_rules→success_rules、findings→failure_models)。"""
    try:
        import reception_memory as mem
        names = {_FINDING_FAILURE_NAME[f["type"]] for f in findings
                 if f["type"] in _FINDING_FAILURE_NAME}
        fms = [m for m in (mem.failure_model(n) for n in names) if m]
        g = mem.update_global_memory(new_rules=new_rules or [], new_failure_models=fms, seed=True)
        print(f"  ↳ Global Memory: success_rules {len(g['success_rules'])} 条 / "
              f"failure_models {[m['name'] for m in g['failure_models']]}")
    except Exception as exc:
        print(f"  ↳ Global Memory 更新跳过: {exc}")


def reflect_reception(trace):
    """会议接待补货的事后反思(供 reception_loop ⑧ 调用)。反思结果同时写入 Global Memory(双 memory 闭环)。"""
    findings, new_rules, source, out = run_reflection(
        trace, reception_findings, "reception_sop.yaml", "reception_sop_v2.yaml",
        "机器人(宇树 G1 人形)刚完成一次会议室饮料补货接待任务")
    _update_global_from_reflection(findings, new_rules)
    return findings, new_rules, source, out


def main():
    trace_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "last_trace.json")
    trace = json.load(open(trace_path, encoding="utf-8"))
    if "rounds" in trace:      # 接待 trace(带循环补货)
        reflect_reception(trace)
    else:                      # 桌面整理 trace
        run_reflection(trace, structural_findings, "demo_sop.yaml",
                       "demo_sop_v2.yaml", "机器人刚完成一次桌面整理任务")


if __name__ == "__main__":
    main()
