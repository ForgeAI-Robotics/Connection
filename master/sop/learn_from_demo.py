"""以人类示教视频更新 SOP —— demo 准备第4步"人类示范一次 → 可复用记忆"的大脑端。

分工:
  [video_to_actions·上游] 第一人称示范视频 → qwen-vl → 动作序列(segments + summary)
  [本模块·下游] 动作序列 + 现有SOP → LLM 提炼 → 【只给大脑,分两层】(对齐 HarnessVLA 双记忆)

★ 只提炼给【大脑】的两层,层与层别重复:
  · Task Specific Memory:这次示范的【具体实例事实】(哪里取/放哪/这次特情)。换场景就可能变。
      → reception_memory/demonstration_*.json,部署时 retrieve 给规划器 re-ground。
  · Global Memory / SOP:【通用规则】(换任何接待都成立)。
      → global_memory.json 的 success_rules + 追加进 reception_sop.yaml 的 learned_rules。

★★ 反幻觉是硬要求:只提【动作序列里明确出现】的,严禁 LLM 靠常识脑补视频里没有的东西
   (位置别精确化、没出现的动作别补)。宁可少写。
   操作手法(how·怎么抓/搬/放)【不在这里提】——粗动作序列没有手部细节,让 LLM 编必是幻觉;
   要真手法得靠宋丽萍的手物重建(WiLoR/FoundationPose),不归本模块。

接口 = 动作序列 JSON(video_to_actions 的输出):
  {"task_summary": "...", "segments": [{"start_time","end_time","action","object","confidence"}, ...]}

用法:
  python learn_from_demo.py demo_actions.json --dry-run   # 只提炼打印不落盘
  python learn_from_demo.py demo_actions.json             # 提炼并写入 Task Specific + Global + SOP
"""

import json
import os
import re
import sys
import urllib.request
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_ROOT = os.path.dirname(os.path.dirname(_HERE))
MEM_DIR = os.path.join(_HERE, "reception_memory")

import reflect                                   # noqa: E402  复用 _api_key(deepseek 凭证)

# 内置示例(格式对齐 video_to_actions 的输出);真跑请传 demo_actions.json
_EXAMPLE = {
    "task_summary": "人类示范:会议接待——从茶水间取可乐送会议室",
    "segments": [
        {"start_time": 0.0, "end_time": 3.0, "action": "走到茶水间冰箱前", "object": "冰箱", "confidence": 0.9},
        {"start_time": 3.0, "end_time": 5.5, "action": "开冰箱取一罐可乐", "object": "可乐", "confidence": 0.88},
        {"start_time": 5.5, "end_time": 10.0, "action": "搬运可乐走向会议室", "object": "可乐", "confidence": 0.9},
        {"start_time": 10.0, "end_time": 12.5, "action": "把可乐放到会议桌", "object": "可乐", "confidence": 0.85},
    ],
}

_CONTEXT = ("宇树 G1 会议接待:开灯 → 扫描会议室/茶水间 → 按人数数可乐缺口 → "
            "从茶水间取可乐送会议室 → 间隔摆放 → 复核 → 播报。大脑管高层规划+复核,不控制手的具体动作。")

_PROMPT = """你是机器人系统的「人类示范学习」模块。人类演示了一次会议接待补货,下面是从视频解析出的动作序列。
请【只从动作序列里明确出现的事实】提炼给大脑,分两层。

【硬性约束——严禁脑补,违反即错】:
- 只写动作序列里【明确有】的。看不出来、要靠常识补的,【一律不写】,宁可少写几条;
- 位置照动作序列的【原粒度】写:动作序列说"会议桌"就写"会议桌",【严禁】脑补成"座位右上角""右上桌角"这类精确位置;
- 动作序列里【没出现】的步骤【绝不】补(如没有"确认缺口/清点数量"这个动作,就别写"先确认缺口");
- 每条都要能对应到上面某个动作段;凭空想的删掉。
- 【不要】写抓握/搬运/放置的手部手法(那是 VLA 的事,不在这提)。

【① Task Specific Memory】(这次示范的具体实例:哪里取、放哪、这次的特情):
task_specific:如"可乐在茶水间冰箱、开门取""放到会议桌上""这次取第二罐前等了他人取物"。只写动作序列支持的。

【② Global Memory / SOP】(通用规则,换任何接待都成立):
global_rules:只从动作序列【明确体现】的行为提通用规则(如动作序列真出现"等他人取物"→可提"遇人占用就等")。
没有明确依据的规则【宁可不提,留空】。别和下面既有规则重复。

【接待任务背景】{context}
【人类示范动作序列】
{actions}
【SOP 既有通用规则(global_rules 别和这些重复)】
{existing}

只输出 JSON,不要其它内容:
{{"task_specific": ["..."], "global_rules": ["..."]}}"""


def _norm(s):
    """归一化(去标点/空格/括号)后再比,防中英文逗号不同导致去重漏判。"""
    return re.sub(r"[，,、。.\s（）()：:]", "", str(s))


def _llm_learn(actions, existing, context):
    key = reflect._api_key()
    if not key:
        return None, "无 CLOUD_API_KEY"
    acts = "\n".join(
        f"- {s.get('start_time','?')}~{s.get('end_time','?')}s: {s['action']}"
        + (f" [{s['object']}]" if s.get("object") else "")
        for s in actions)
    known = "\n".join(f"- {r}" for r in existing) or "(无)"
    prompt = _PROMPT.format(context=context, actions=acts, existing=known)
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps({"model": "deepseek-chat",
                         "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0, "max_tokens": 600}).encode("utf-8"),  # temp0:别发挥
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content = json.loads(r.read())["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        return (json.loads(m.group(0)) if m else {}), None
    except Exception as exc:
        return None, str(exc)


def _template_learn(actions):
    """LLM 不可用时降级:只把涉及可乐的动作原样当具体记忆,不提通用规则(避免瞎编)。"""
    cola = [s["action"] for s in actions if s.get("object") in ("可乐", "可乐罐")]
    return {"task_specific": cola[:5] or [s["action"] for s in actions[:5]], "global_rules": []}


def _existing_rules():
    import yaml
    sop = yaml.safe_load(open(os.path.join(_HERE, "reception_sop.yaml"), encoding="utf-8")) or {}
    return sop.get("learned_rules", [])


def _append_learned_rules(new_rules):
    """通用规则文本追加到 reception_sop.yaml 的 learned_rules 末尾(它是文件最后一节,
    保留全部原注释;safe_dump 会丢注释,故用文本追加)。返回追加条数。"""
    if not new_rules:
        return 0
    tag = f"[demo·{date.today().isoformat()}]"
    with open(os.path.join(_HERE, "reception_sop.yaml"), "a", encoding="utf-8") as f:
        for r in new_rules:
            f.write(f"  - {tag} {r}\n")
    return len(new_rules)


def save_demonstration(task_specific, raw, name="reception_restock"):
    """Task Specific Memory → reception_memory/demonstration_{name}.json(给大脑,部署时 retrieve)。"""
    os.makedirs(MEM_DIR, exist_ok=True)
    rec = {"source": "人类示范视频(video_to_actions 动作解析)",
           "learned_at": date.today().isoformat(),
           "task_summary": raw.get("task_summary", ""),
           "task_specific": task_specific,     # 这次的具体位置/对象/特情
           "raw_segments": raw.get("segments", [])}
    out = os.path.join(MEM_DIR, f"demonstration_{name}.json")
    json.dump(rec, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return out


def retrieve_demonstration(name="reception_restock"):
    """读回 Task Specific Memory(save_demonstration 落的那份),部署接待时 re-ground 到本次。
    读的是【文件内容】、不写死任何位置/特情——换示范视频 → learn_from_demo 写新的
    demonstration_{name}.json → 这里自动读到新的,无需改代码。文件不存在返回空(不影响接待)。"""
    path = os.path.join(MEM_DIR, f"demonstration_{name}.json")
    if not os.path.exists(path):
        return {"task_specific": [], "task_summary": "", "learned_at": ""}
    try:
        rec = json.load(open(path, encoding="utf-8"))
        return {"task_specific": rec.get("task_specific", []) or [],
                "task_summary": rec.get("task_summary", ""),
                "learned_at": rec.get("learned_at", "")}
    except Exception:
        return {"task_specific": [], "task_summary": "", "learned_at": ""}


def persist(task_specific, global_rules, demo, name="reception_restock"):
    """把【已确认的】提炼结果【直接】落盘(不重新提炼):Task Specific → 记忆;Global → global_memory + SOP。
    给前端「确认落盘」用——所见即所得,落的就是页面展示、你确认的那份,不再调 LLM 重算(避免二次提炼漂移)。"""
    import reception_memory as mem
    save_demonstration(task_specific, demo, name)
    mem.update_global_memory(new_rules=global_rules, seed=True)
    _append_learned_rules(global_rules)


def learn_from_demo(demo, write=True, context=None):
    """人类示范动作序列 → 只给大脑、分两层提炼(反幻觉):Task Specific / Global。
    context: 任务背景(前端可传,换任务不改代码);None 用默认接待背景 _CONTEXT。"""
    actions = demo.get("segments", [])
    existing = _existing_rules()
    exist_norm = [_norm(r) for r in existing]

    print("=" * 66)
    print("人类示范学习(demo 第4步:一次示范 → 分两层 Task Specific / Global;只提动作序列真支持的)")
    print("=" * 66)
    print(f"示范摘要: {demo.get('task_summary','')}")
    print(f"动作序列({len(actions)} 段):")
    for s in actions:
        obj = f" [{s['object']}]" if s.get("object") else ""
        print(f"  · {s.get('start_time','?')}~{s.get('end_time','?')}s {s['action']}{obj}")

    result, err = _llm_learn(actions, existing, context or _CONTEXT)
    source = "LLM(deepseek)"
    if not result:
        result, source = _template_learn(actions), f"模板档(LLM 不可用: {err})"
    task_specific = result.get("task_specific", [])
    global_rules = [r for r in result.get("global_rules", [])          # 标点归一去重(和既有 SOP 规则)
                    if not any(_norm(r) in e or e in _norm(r) for e in exist_norm)]

    print(f"\n提炼(来源: {source}) —— 只提动作序列真支持的,分两层:")
    print("  ① Task Specific Memory(这次示范的具体实例:哪里取/放哪/特情)")
    for d in task_specific:
        print(f"      [Task Specific] {d}")
    print("  ② Global Memory / SOP(通用规则)")
    if global_rules:
        for r in global_rules:
            print(f"      [Global]        {r}")
    else:
        print("      (无——本次动作序列没有明确支持、且非既有规则的新通用规则)")

    if not write:
        print("\n[--dry-run] 只提炼不落盘。")
        return task_specific, global_rules

    import reception_memory as mem
    demo_path = save_demonstration(task_specific, demo)                 # ① Task Specific
    g = mem.update_global_memory(new_rules=global_rules, seed=True)     # ② Global success_rules
    n = _append_learned_rules(global_rules)                             # ② 也落 SOP 文件
    print(f"\n  ① Task Specific → {os.path.relpath(demo_path, _ROOT)}({len(task_specific)} 条)")
    print(f"  ② Global → global_memory.json success_rules {len(g['success_rules'])} 条 · reception_sop.yaml 追加 {n} 条")
    return task_specific, global_rules


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    files = [a for a in argv if not a.startswith("-")]
    demo = json.load(open(files[0], encoding="utf-8")) if files else _EXAMPLE
    learn_from_demo(demo, write=not dry)


if __name__ == "__main__":
    main()
