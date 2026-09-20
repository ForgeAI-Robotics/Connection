"""事后反思 —— 顶层任务结束时的通用复盘。

兼容旧的桌面 skills / 接待 rounds trace；新路径吃统一 episode
(fq/reflection-episode/v1)。不区分仿真或真机：由调用方记账，这里只提炼事实。

两档:结构化事实 → deepseek 写候选经验 / 模板档降级。
默认只落 master/memory/reflections/,不改 SOP;mock 接待仍可 write_sop。

用法:
  python reflect.py                              # 桌面(默认读 last_trace.json)
  python reflect.py last_reception_trace.json    # 接待(自动识别 rounds)
"""

import json
import os
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

import yaml

try:
    from episode import episode_from_reception_trace
except ImportError:  # master/ on sys.path
    from sop.episode import episode_from_reception_trace

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
    "抓取失败": "抓取失败后必须重新观测手上是否有物体,未抓住则重试或上报,不进入下一段导航。",
    "导航受阻": "导航未到达声明目标时不得当作成功,应记录终态并停下,不继续抓放。",
    "任务未完成": "顶层任务失败后把失败步骤和证据写入复盘,下次先核对手账再重放动作。",
    "需要人工恢复": "进入 RECOVERY_REQUIRED 后禁止自动重放,须人工确认现场状态再开新任务。",
    "观察完成": "现场观察任务以相机画面为准,描述未见物体时不要编造。",
}


def generic_findings(episode):
    """Fact extraction for the unified episode ledger (sim and real)."""
    findings = []
    steps = (episode or {}).get("steps") or []
    lies, grasp, nav, other = [], [], [], []
    for st in steps:
        phase = str(st.get("phase") or "")
        detail = str(st.get("detail") or st.get("verify_detail") or "")
        blob = f"{phase} {detail}"
        claimed = st.get("claimed_ok")
        verified = st.get("verify_ok")
        status = str(st.get("status") or "")
        if claimed is True and verified is False:
            lies.append(f"{phase}: {st.get('verify_detail') or detail or '复查未通过'}")
        failed = status in ("failure", "exception", "timeout", "unknown") or verified is False
        if not failed:
            continue
        if any(k in blob for k in ("抓", "pick", "grasp", "夹", "持有")):
            grasp.append(detail or phase)
        elif any(k in blob for k in ("导航", "walk", "nav", "行走", "过门", "table")):
            nav.append(detail or phase)
        else:
            other.append(detail or phase)
    if lies:
        findings.append({"type": "执行报告与实际不符", "detail": lies[0]})
    if grasp:
        findings.append({"type": "抓取失败", "detail": grasp[0]})
    if nav:
        findings.append({"type": "导航受阻", "detail": nav[0]})
    final = str((episode or {}).get("final") or "")
    if final == "recovery":
        findings.append({
            "type": "需要人工恢复",
            "detail": (episode or {}).get("error") or "任务进入 RECOVERY_REQUIRED",
        })
    elif final == "failure" and not findings:
        extra = other[0] if other else ((episode or {}).get("error") or "顶层任务未成功结束")
        findings.append({"type": "任务未完成", "detail": extra})
    elif (episode or {}).get("task_type") == "look" and final == "success":
        seen = next((str(s.get("detail") or "") for s in steps if s.get("detail")), "")
        findings.append({"type": "观察完成", "detail": seen or "已完成现场观察"})
    if not findings:
        leftover = other[0] if other else ""
        if leftover:
            findings.append({"type": "任务未完成", "detail": leftover})
        else:
            findings.append({"type": "顺利完成", "detail": "记录的步骤均成功结束"})
    return findings


def reflection_enabled(config=None):
    env = os.environ.get("REFLECTION_ENABLED")
    if env not in (None, ""):
        return str(env).strip().lower() not in ("0", "false", "no", "off")
    if not isinstance(config, dict):
        return True
    block = config.get("reflection")
    if not isinstance(block, dict):
        return True
    return bool(block.get("enabled", True))


def _llm_allowed(config=None):
    env = os.environ.get("FQPLANNER_REFLECTION_LLM")
    if env not in (None, ""):
        return str(env).strip().lower() not in ("0", "false", "no", "off")
    if isinstance(config, dict):
        block = config.get("reflection") or {}
        if isinstance(block, dict) and "use_llm" in block:
            return bool(block.get("use_llm"))
    return True


def _write_sop_requested(config, write_sop):
    if write_sop is not None:
        return bool(write_sop)
    if isinstance(config, dict):
        block = config.get("reflection") or {}
        if isinstance(block, dict) and "write_sop" in block:
            return bool(block.get("write_sop"))
    return False


def _reflection_root():
    override = os.environ.get("FQPLANNER_REFLECTION_DIR")
    if override:
        return Path(override)
    return Path(_ROOT) / "master" / "memory" / "reflections"


def persist_reflection(episode, result):
    root = _reflection_root()
    root.mkdir(parents=True, exist_ok=True)
    day = date.today().isoformat()
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    task_id = str((episode or {}).get("task_id") or "unknown").replace("/", "_")
    path = folder / f"{task_id}.json"
    payload = dict(result or {})
    payload["episode"] = episode
    payload["path"] = str(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (root / "candidates.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "task_id": (episode or {}).get("task_id"),
            "task": (episode or {}).get("task"),
            "task_type": (episode or {}).get("task_type"),
            "backend": (episode or {}).get("backend"),
            "final": (episode or {}).get("final"),
            "findings": (result or {}).get("findings"),
            "new_rules": (result or {}).get("new_rules"),
            "source": (result or {}).get("source"),
            "summary": (result or {}).get("summary"),
            "path": str(path),
        }, ensure_ascii=False) + "\n")
    return payload


def format_summary(result):
    if not result:
        return ""
    if result.get("skipped"):
        return ""
    if result.get("error") and not result.get("findings"):
        return f"复盘失败：{result.get('error')}"
    parts = []
    for item in result.get("findings") or []:
        kind = str(item.get("type") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if kind and detail:
            parts.append(f"{kind}：{detail}")
        elif kind or detail:
            parts.append(kind or detail)
    rules = [str(r).strip() for r in (result.get("new_rules") or []) if str(r).strip()]
    if rules:
        parts.append("经验：" + "；".join(rules[:3]))
    source = str(result.get("source") or "").strip()
    if source:
        parts.append(f"（{source}）")
    return " ".join(parts)[:1500]


def maybe_reflect(
    episode,
    *,
    config=None,
    write_sop=None,
    sop_filename="reception_sop.yaml",
    sop_out_name="reception_sop_v2.yaml",
    task_desc=None,
    findings_fn=None,
    update_global=False,
    llm_fn=None,
    quiet=False,
):
    """Run reflection on a unified episode. Returns a JSON-ready dict."""
    if not reflection_enabled(config):
        return {"skipped": True, "reason": "disabled"}
    episode = dict(episode or {})
    extract = findings_fn or generic_findings
    findings = extract(episode)
    existing = []
    sop = None
    do_write = _write_sop_requested(config, write_sop)
    if do_write:
        sop_path = os.path.join(_HERE, sop_filename)
        if os.path.isfile(sop_path):
            sop = yaml.safe_load(open(sop_path, encoding="utf-8")) or {}
            existing = list(sop.get("learned_rules") or [])
    desc = task_desc or (
        f"机器人刚完成一次任务「{episode.get('task') or episode.get('task_type') or '未命名'}」"
        f"（{episode.get('backend') or 'unknown'}）"
    )
    if llm_fn is not None:
        produce = llm_fn
    elif _llm_allowed(config):
        produce = llm_rules
    else:
        produce = lambda *_args, **_kwargs: (None, "LLM 已关闭")
    rules, err = produce(findings, existing, desc)
    source = "LLM(deepseek)"
    if not rules:
        rules = template_rules(findings)
        source = f"模板档(LLM 不可用: {err})" if err else "模板档"
    new_rules = [r for r in (rules or []) if not any(r[:12] in ex or ex[:12] in r for ex in existing)]
    if not quiet:
        print("=" * 62)
        print("事后反思")
        print("=" * 62)
        for item in findings:
            print(f"  · [{item.get('type')}] {item.get('detail')}")
        print(f"\n新增经验规则(来源: {source}):")
        if not new_rules:
            print("  (无新增)")
        for rule in new_rules:
            print(f"  + {rule}")
    sop_out = None
    if do_write and sop is not None and new_rules:
        sop_out = write_sop_v2(sop, new_rules, sop_out_name)
        if not quiet:
            print(f"\n新版 SOP → {os.path.relpath(sop_out, _ROOT)}")
    if update_global:
        _update_global_from_reflection(findings, new_rules)
    result = {
        "findings": findings,
        "new_rules": new_rules,
        "source": source,
        "sop_v2": os.path.relpath(sop_out, _ROOT) if sop_out else None,
        "task_id": episode.get("task_id"),
        "task_type": episode.get("task_type"),
        "backend": episode.get("backend"),
        "final": episode.get("final"),
    }
    result["summary"] = format_summary(result)
    return persist_reflection(episode, result)


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
    "抓取失败": "empty_grasp",
    "导航受阻(重试后恢复)": "walk_blocked",
    "导航受阻": "walk_blocked",
    "执行报告与实际不符": "false_success",
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
    episode = episode_from_reception_trace(trace, backend="mock")
    result = maybe_reflect(
        episode,
        write_sop=True,
        sop_filename="reception_sop.yaml",
        sop_out_name="reception_sop_v2.yaml",
        task_desc="机器人(宇树 G1 人形)刚完成一次会议室饮料补货接待任务",
        findings_fn=lambda _ep: reception_findings(trace),
        update_global=True,
    )
    sop_out = result.get("sop_v2")
    return (
        result.get("findings") or [],
        result.get("new_rules") or [],
        result.get("source") or "",
        sop_out,
        result,
    )


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
