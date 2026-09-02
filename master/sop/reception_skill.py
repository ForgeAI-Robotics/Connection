"""接待 skill —— 把「会议接待补货」封装成一个自带 SOP + 经验的高层技能。

设计(HarnessVLA「大脑杠杆」+ agent skill):master 识别到接待任务,就【整体调用这个 skill】
(内部自跑补货循环、每步重新确认、失败按现状恢复、事后反思),而不是让 LLM 把接待拆成子任务
——接待的 know-how(带循环 / 纠错)锁在 skill 内,规划器只需知道"有个接待 skill"。跟 master 里
桌面整理走 _plan_desk_tidy 专门分支是同一路子。

skill 的三样东西:
  执行体: reception_loop.run_reception 的闭环(内部调 walk/pick/place 原语,backend 可切真机)
  SOP    : reception_sop.yaml(标准流程 + learned_rules,事后反思会 update 出 reception_sop_v2.yaml)
  经验   : global_memory.json(成功规则 + 失败模型 empty_grasp / false_success / ...)

用法:
  import reception_skill as rsk
  if rsk.is_reception_task("开始接待"):
      # on_step 供 master 把子任务写进 task_status(前端复用 master/slaver 输出区)
      trace = rsk.run_reception_skill(on_step=lambda no, ph, de, st: ...)
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# skill 卡片:master 规划器 / 前端据此知道"有这么个 skill",不必懂它内部怎么补货
RECEPTION_SKILL = {
    "name": "reception",
    "label": "会议接待补货",
    "description": ("会议前接待:开灯 → 扫描会议室/茶水间 → 按参会人数数可乐缺口 → "
                    "一个一个补货(每步重新观测确认,失败按现状恢复) → 复核 → 语音播报 → 事后反思生成新SOP"),
    "triggers": ["接待", "补货", "会议接待", "开始接待", "迎宾", "准备会议", "会议准备"],
    "robot_name": "FQrobot",             # 与 _plan_desk_tidy 一致:接待由这台执行
    "sop_file": "reception_sop.yaml",
    "memory_file": "global_memory.json",
}


def is_reception_task(task) -> bool:
    """任务是否命中接待 skill(关键词)。master 路由用它,和 _is_desk_tidy_task 同款。"""
    t = task if isinstance(task, str) else (" ".join(map(str, task)) if task else "")
    return any(k in t for k in RECEPTION_SKILL["triggers"])


def skill_card() -> dict:
    """skill 卡片(name/描述/触发词/SOP/经验) + 当前 learned_rules —— 体现"skill 带知识"。
    给规划器/前端展示"有这么个自带 SOP 和经验的接待 skill"。"""
    card = dict(RECEPTION_SKILL)
    try:
        import yaml
        v2 = os.path.join(_HERE, "reception_sop_v2.yaml")     # 反思沉淀后的版本优先
        p = v2 if os.path.exists(v2) else os.path.join(_HERE, RECEPTION_SKILL["sop_file"])
        with open(p, encoding="utf-8") as f:
            sop = yaml.safe_load(f) or {}
        card["learned_rules"] = sop.get("learned_rules", [])
        card["sop_version"] = "v2" if p.endswith("v2.yaml") else "v1"
    except Exception:
        card["learned_rules"], card["sop_version"] = [], "?"
    return card


def run_reception_skill(task="开始接待", on_step=None, backend="mock",
                        scenario="normal", headcount=4, reflect=True,
                        mode=None, task_id=None, real_config=None,
                        on_state=None, **kw):
    """执行接待 skill:跑闭环,通过 on_step 上报【子任务级】进度。返回 trace(dict)。

    master 传的 on_step 把每个子任务写进 task_status(前端复用 🧠思考 + 子任务✓ 显示)。
    mode=mock 保留原虚拟补货循环；mode=real 在当前 Master 进程内执行固定
    DREAM→VLA→图片判真→DREAM→VLA→图片判真的真机单链路。
    """
    import sys
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)

    selected_mode = str(
        mode or os.environ.get("RECEPTION_MODE") or "mock"
    ).strip().lower()
    if selected_mode == "real":
        if not task_id:
            raise ValueError("真机接待必须提供task_id")
        try:
            from .reception_real import run_reception_real
        except ImportError:  # 独立脚本兼容
            from sop.reception_real import run_reception_real
        return run_reception_real(
            task_id,
            real_config or {},
            on_step=on_step,
            on_state=on_state,
        )
    if selected_mode != "mock":
        raise ValueError(f"不支持的接待模式: {selected_mode}")

    from reception_loop import run_reception

    # retrieve Task Specific Memory(上次人类示范学到的【具体实例/特情】)→ re-ground 到本次接待。
    # 内容全部来自 demonstration_*.json、不写死:换示范视频→learn_from_demo 写新文件→这里自动读到新的。
    # 现在先"读出来 + 上报展示"把链路接通(mock 下体现为一条参考经验);真机时可进一步喂给找物/导航 re-ground。
    try:
        from learn_from_demo import retrieve_demonstration
        dm = retrieve_demonstration()
        if dm["task_specific"] and on_step:
            ref = " / ".join(dm["task_specific"][:3])
            tag = f"({dm['learned_at']}) " if dm.get("learned_at") else ""
            on_step(0, "📖 参考人类示范经验", f"{tag}{ref}", "success")
    except Exception:
        pass

    return run_reception(scenario=scenario, backend=backend, headcount=headcount,
                         reflect=reflect, on_step=on_step, **kw)


if __name__ == "__main__":
    # 自测:识别 + 卡片 + 收集子任务序列(验证 on_step 回调)
    print("识别 '开始接待' :", is_reception_task("开始接待"))
    print("识别 '去卧室'   :", is_reception_task("去卧室"))
    card = skill_card()
    print(f"卡片: {card['label']} · SOP {card['sop_version']} · learned_rules {len(card['learned_rules'])} 条")
    steps = []
    run_reception_skill(reflect=False, on_step=lambda no, ph, de, st: steps.append((no, ph, de, st)))
    print(f"\n子任务序列({len(steps)} 条,master 会照这个写 task_status):")
    for no, ph, de, st in steps:
        print(f"  [{no}] {'✓' if st == 'success' else '✗'} {ph} — {de}")
