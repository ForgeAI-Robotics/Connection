"""大脑闭环 driver —— 八月「G1 会议接待」demo(当前主线,替代桌面 run_loop.py)。

与桌面 run_loop 的关键区别:桌面是【线性】subtask_list(for skill in needed 逐个执行+确认);
这里是【带循环/条件的补货大脑程序】——
  触发 → 开灯 → 扫描会议室+茶水间 → 数饮料 vs 人数 → 算缺口
   → while 缺口>0:【一个一个】补货一轮(walk→茶水间 · pick · walk→会议室 · place),
       **每步重新观测做状态确认**(不采信 VLA/导航自报);失败后按【观测到的状态】(手上有没有
       可乐、人在哪个房间)决定从哪步恢复,而不是盲目重跑整轮 → 重新计数更新缺口
   → 复核前后状态 → 语音播报 → 生成新版 SOP → trace 存档(事后反思的输入)。

闭环抽成 run_reception(..., on_step=回调):既能 CLI 直接跑,也能被 reception_skill 当作
【一个 skill】整体调用(master 识别到接待任务就调它,不劳 LLM 拆子任务)。on_step 在每个子任务
边界回调 → master 用它往 task_status 写(前端复用 master/slaver 输出区显示 🧠思考 + 子任务✓)。

pick / walk / place 拆成独立技能调用(见 reception_skills),大脑在每步之间插确认/纠正 —— 这是
「大脑杠杆」的着力点(给弱 VLA 套 critic 闭环)。执行/观测两个 backend:
  mock(进程内世界,验证逻辑) | robot_api(真实执行/观测总闸,"接入 VLA/导航"切这个,大脑零改)。

用法:
  python master/sop/reception_loop.py                                # mock 正常场景(补货循环)
  python master/sop/reception_loop.py --scenario place_miss          # mock 故障:VLA 放偏谎报→大脑逮住
  python master/sop/reception_loop.py --single                       # mock 单次补货(补一罐)
  python master/sop/reception_loop.py --single --backend robot_api   # 单次补货,接真实执行/观测
  可选场景(仅 mock):normal | grasp_fail | walk_blocked | place_miss | label_wrong
"""

import argparse
import json
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_ROOT = os.path.dirname(os.path.dirname(_HERE))

import reception_skills as sk                                        # noqa: E402
from reception_world import reset_world, inject_fault, w_headcount    # noqa: E402

MAX_RETRY = 2   # 每轮补货最多重试次数(抓不到/过不去/放偏)


def _load_sop():
    path = os.path.join(_HERE, "reception_sop.yaml")
    try:
        import yaml
        return yaml.safe_load(open(path, encoding="utf-8"))
    except Exception as exc:
        print(f"⚠ SOP 读取失败({exc}) → 用内置默认(per_person=1)")
        return {}


# ---------- 任务完成确认 & 上报(SOP §6 completion + task_state.success/failure_conditions)----------

def _notify_feishu(card):
    """上报飞书(预留):设了 FEISHU_WEBHOOK 环境变量才发;③飞书触发接入后完善卡片格式。现在发纯文本。"""
    url = os.environ.get("FEISHU_WEBHOOK")
    if not url:
        return
    try:
        import urllib.request
        text = f"[G1 接待] {'✅ 完成' if card['verdict'] else '❌ 未完成'} · {card['spoken']}"
        req = urllib.request.Request(
            url, data=json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        print(f"       (飞书上报失败,跳过:{exc})")


def _report_out(card):
    """结构化上报:控制台逐条 + 落 last_reception_report.json(前端可读)+ 飞书(预留)。"""
    print(f"       📋 完成确认报告 [{'✅ 完成' if card['verdict'] else '❌ 未完成'}]")
    for c in card["checks"]:
        print(f"          {'✓' if c['pass'] else '✗'} {c['name']}:{c['detail']}")
    if card["anomalies"]:
        print(f"          ⚠ 异常:{'; '.join(card['anomalies'])}")
    print(f"          → {card['advice']}")
    try:
        out = os.path.join(_HERE, "last_reception_report.json")
        json.dump(card, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    _notify_feishu(card)


# ---------- 状态确认(重新观测,不采信技能自报;观测源 mock/robot_api 由 reception_skills.obs_* 统一)----------

def _v_light(room):
    ok = sk.obs_light_on(room)
    return ok, "灯已亮" if ok else "灯没亮"


def _v_pick():
    ok = sk.obs_holding() == sk.COLA
    return ok, "手上有可乐" if ok else "手上没拿到可乐"


def _v_walk(to):
    at = sk.obs_robot_at()
    if at is None:      # robot_api 位姿→区域未接:暂信导航自报(接 base_status 映射后自动转严格)
        return True, f"已到{to}(真机位姿未接,暂信自报)"
    ok = at == to
    return ok, f"已到{to}" if ok else f"没到{to}(还在{at})"


def _v_place(expected, simple=False):
    """放对没:会议室可乐数要 +1(到位);非最简版再查最近槽位间隔✓朝向✓。"""
    have = sk.obs_count_cola("会议室")
    if have < expected:
        return False, f"会议室可乐仍 {have} 罐(放偏/未入位)"
    if simple:
        return True, f"已放置(会议室桌上 {have} 罐)"
    slot = sk.obs_last_slot("会议室")
    if not slot or not slot.get("label_aligned"):
        return False, "已入位但标签朝向不一致"
    if not slot.get("spaced"):
        return False, "已入位但没间隔摆放"
    return True, f"已就位(会议室 {have} 罐,间隔✓ 朝向✓)"


def _rec(step, exec_report, ok, detail):
    """打印「技能自报 vs 大脑确认」+ 返回结构化记录(进 trace,供事后反思)。"""
    said = "自报成功" if exec_report.get("success") else \
        f"自报失败({exec_report.get('fail_reason', '?')})"
    print(f"       {step}:{said} | 大脑确认:{'✓ ' if ok else '✗ '}{detail}")
    return {"step": step, "exec": exec_report, "verify_ok": ok, "verify_detail": detail}


def _restock_one_round(round_no, simple=False):
    """补一罐(一轮):walk→茶水间 · pick · walk→会议室 · place,每步重新确认。
    状态守卫(手上有没有可乐 / 人在哪)决定跳过已完成步 → 失败后按现状智能恢复。
    (robot_api 位姿未接时 obs_robot_at 返回 None,守卫不跳步 = 每轮老实走两趟;接位姿映射后自动智能跳。)
    返回 (是否成功, 本轮各次尝试记录)。"""
    attempts = []
    for attempt in range(1, MAX_RETRY + 2):
        steps, fail_at = [], None
        # (1)去茶水间取 —— 手上没可乐才需要;已在茶水间则跳过走这步
        if sk.obs_holding() != sk.COLA:
            if sk.obs_robot_at() != "茶水间":
                r = sk.walk("茶水间"); ok, det = _v_walk("茶水间")
                steps.append(_rec("walk→茶水间", r, ok, det))
                if not ok:
                    fail_at = "walk→茶水间"
            if fail_at is None:
                r = sk.pick("可乐", "茶水间"); ok, det = _v_pick()
                steps.append(_rec("pick 可乐@茶水间", r, ok, det))
                if not ok:
                    fail_at = "pick"
        # (2)回会议室放 —— 不在会议室才需要走
        if fail_at is None and sk.obs_robot_at() != "会议室":
            r = sk.walk("会议室"); ok, det = _v_walk("会议室")
            steps.append(_rec("walk→会议室", r, ok, det))
            if not ok:
                fail_at = "walk→会议室"
        # (3)摆放
        if fail_at is None:
            expected = sk.obs_count_cola("会议室") + 1
            r = sk.place("可乐", "会议室"); ok, det = _v_place(expected, simple)
            steps.append(_rec("place 可乐→会议室", r, ok, det))
            if not ok:
                fail_at = "place"
        attempts.append({"n": attempt, "steps": steps, "fail_at": fail_at})
        if fail_at is None:
            return True, attempts
        if attempt <= MAX_RETRY:
            print(f"       ↻ {fail_at} 失败 → 按现状恢复"
                  f"(手上{'有' if sk.obs_holding() == sk.COLA else '无'}可乐/在{sk.obs_robot_at() or '未知'}),"
                  f"重试(第{attempt}/{MAX_RETRY}次)")
        else:
            print(f"       ⤼ 重试{MAX_RETRY}次仍失败 → 停下上报")
    return False, attempts


def run_reception(scenario="normal", backend="mock", headcount=4, meeting_cola=1,
                  tea_cola=10, single=False, simple_place=False, reflect=True,
                  on_step=None):
    """接待补货闭环(可被 reception_skill / CLI 调用)。返回 trace(dict)。

    on_step(no, phase, detail, status):【子任务级】进度回调,供 master 写 task_status。
      no=1..8 阶段号;phase 子任务名;detail 结果说明;status="success"/"failure"(这步结果)。
      每个子任务边界(开灯/扫描/数缺口/补第N罐/复核/播报/反思)回调一次;补货每补一罐一条。
    """
    def _emit(no, phase, detail, status="success"):
        if on_step:
            try:
                on_step(no, phase, detail, status)
            except Exception:
                pass

    sk.set_backend(backend)
    if single:                            # 单次补货 = 需求 1 罐、会议室现有 0 → 补一轮
        headcount, meeting_cola = 1, 0
    simple_place = simple_place or single

    # 世界初始化 + 故障注入(仅 mock 用;robot_api 下 world 不被读)
    reset_world(headcount=headcount, meeting_cola=meeting_cola, tea_cola=tea_cola)
    if backend == "mock" and scenario != "normal":
        inject_fault(scenario)

    sop = _load_sop()
    per_person = (sop.get("beverage_restock") or {}).get("per_person", 1)
    room = "会议室"

    flow = "取放一轮" if single else "补货循环"
    print("=" * 66)
    print(f"大脑闭环 · G1 会议接待{'· 单次补货' if single else ''}"
          f"(触发→开灯→扫描→数缺口→{flow}→复核→播报)")
    print(f"backend:{backend}" + (f" · 场景:{scenario}" if backend == "mock" else ""))
    print("=" * 66)

    trace = {"task": "G1 会议接待补货", "scenario": scenario, "backend": backend,
             "single": single, "started_at": datetime.now().isoformat(),
             "steps": {}, "rounds": []}

    # ① 触发(demo 硬编码;真机接飞书日程/语音,含会议室+时间+人数)
    hc = w_headcount()
    print(f"① 触发:会议室「阳光厅」10:00 会议,参会 {hc} 人")
    trace["trigger"] = {"room": room, "headcount": hc, "per_person": per_person}
    _emit(1, "触发", f"会议室「阳光厅」· 参会 {hc} 人")

    # ② 开灯 + 确认
    print("② 开灯:")
    r = sk.turn_on_lights(room); ok, det = _v_light(room)
    trace["steps"]["light"] = _rec(f"开灯@{room}", r, ok, det)
    _emit(2, "开灯", det, "success" if ok else "failure")

    # ③ 扫描会议室 + 茶水间(观测源:VLA看图/世界模型场景图)
    print("③ 扫描:")
    m = sk.scan(room); t = sk.scan("茶水间")
    have0, stock, blocked = m["cola"], t["cola"], m["chairs_blocking"]
    print(f"       会议室:可乐 {have0} 罐,办公椅挡路 {'是' if blocked else '否'}")
    print(f"       茶水间:可乐库存 {stock} 罐")
    trace["scan_before"] = {"meeting_cola": have0, "tea_stock": stock, "chairs_blocking": blocked}
    _emit(3, "扫描会议室+茶水间",
          f"会议室可乐 {have0} 罐 · 茶水间库存 {stock} 罐" + (" · 办公椅挡路" if blocked else ""))

    # ④ 数饮料 vs 人数 → 算缺口
    need = hc * per_person
    gap = need - have0
    print(f"④ 数缺口:需求 {need} 罐({hc}人 × {per_person})− 现有 {have0} = 缺口 {max(gap, 0)} 罐")
    trace["gap"] = {"need": need, "have": have0, "gap": max(gap, 0)}
    _emit(4, "数缺口", f"需求 {need} 罐 − 现有 {have0} = 缺口 {max(gap, 0)} 罐")

    # ⑤ 补货:一个一个补,每轮 walk→pick→walk→place + 每步确认,补完重新数
    print("⑤ " + ("单次取放(walk→pick→walk→place,每步确认):" if single
                 else "补货循环(一个一个补,每步重新确认):"))
    aborted, placed = False, 0
    while gap > 0:
        stock = sk.scan("茶水间")["cola"]
        if stock <= 0:
            print(f"     ✗ 茶水间可乐不足(已补 {placed} 罐,还缺 {gap})→ 停下上报")
            _emit(5, f"补第 {placed + 1} 罐可乐", f"茶水间可乐不足(已补 {placed} 罐)", "failure")
            aborted = True
            break
        print(f"   ── 第 {placed + 1} 罐(还缺 {gap},茶水间余 {stock})──")
        ok, attempts = _restock_one_round(placed + 1, simple_place)
        trace["rounds"].append({"round": placed + 1, "ok": ok, "attempts": attempts})
        if not ok:
            print(f"     ✗ 本轮补货失败 → 停下上报(已补 {placed} 罐)")
            _emit(5, f"补第 {placed + 1} 罐可乐", "重试仍失败,停下上报", "failure")
            aborted = True
            break
        placed += 1
        have_now = sk.obs_count_cola(room)      # 重新计数
        gap = need - have_now
        print(f"       重新数:会议室 {have_now} 罐 → 还缺 {max(gap, 0)}")
        retried = len(attempts) > 1
        _emit(5, f"补第 {placed} 罐可乐",
              f"会议室 {have_now} 罐,还缺 {max(gap, 0)}" + ("(失败后按现状恢复,重试成功)" if retried else ""),
              "success")

    # ⑥ 复核前后状态(单次只看数量;完整 demo 再查灯 + 全局摆放)
    print("⑥ 复核:")
    have_final = sk.obs_count_cola(room)
    light_ok = sk.obs_light_on(room)
    satisfied = have_final >= need
    bad_slots = [s for s in sk.obs_all_slots(room)
                 if not (s.get("spaced") and s.get("label_aligned"))]
    layout_ok = not bad_slots
    print(f"       会议室可乐 {have0}→{have_final},满足 {hc} 人需求:{'✓' if satisfied else '✗'}")
    if not single:
        print(f"       灯光:{'亮 ✓' if light_ok else '未亮 ✗'}")
        print(f"       全局摆放(间隔/朝向):{'✓ 全部合格' if layout_ok else f'✗ {len(bad_slots)} 罐不合格(需复摆)'}")
    verdict = satisfied and not aborted
    if not single:
        verdict = verdict and light_ok and layout_ok
    trace["final_check"] = {"have_before": have0, "have_after": have_final, "need": need,
                            "satisfied": satisfied, "light_ok": light_ok,
                            "layout_ok": layout_ok, "bad_slots": len(bad_slots),
                            "aborted": aborted, "verdict": verdict}
    _emit(6, "复核",
          f"会议室可乐 {have0}→{have_final}"
          + ("" if single else f" · 灯{'✓' if light_ok else '✗'} · 摆放{'✓' if layout_ok else '✗'}"),
          "success" if verdict else "failure")

    # ⑦ 语音播报
    print("⑦ 播报:")
    if verdict:
        report = (f"{room}已放置可乐 {have_final} 罐。" if single
                  else f"{room}已布置就绪,可乐 {have_final} 罐,满足 {hc} 位参会者,灯光已开。")
    else:
        reasons = []
        if not satisfied:  reasons.append(f"可乐 {have_final}/{need}")
        if not layout_ok and not single:  reasons.append(f"{len(bad_slots)} 罐摆放不合格")
        if not light_ok and not single:   reasons.append("灯未开")
        if aborted:        reasons.append("补货中途中止")
        report = f"{room}尚未就绪({'、'.join(reasons)}),已停下并上报,请人工协助。"
    sk.speak(report)
    trace["report"] = report
    _emit(7, "语音播报", report, "success")

    # 完成确认报告(逐条对照 success_conditions)+ 结构化上报
    checks = [{"name": "饮料满足人数", "pass": satisfied,
               "detail": f"会议室可乐 {have_final}/{need} 罐"}]
    if not single:
        checks.append({"name": "会议室灯已开", "pass": light_ok,
                       "detail": "灯亮" if light_ok else "灯未亮"})
        checks.append({"name": "摆放合格(间隔/朝向)", "pass": layout_ok,
                       "detail": "全部合格" if layout_ok else f"{len(bad_slots)} 罐不合格"})
    checks.append({"name": "未中途中止", "pass": not aborted,
                   "detail": "正常完成" if not aborted else "补货中途停下上报"})
    anomalies = [f"{c['name']}({c['detail']})" for c in checks if not c["pass"]]
    advice = ("任务完成,无需人工干预。" if verdict
              else "任务未就绪,需人工协助:" + "、".join(anomalies))
    report_card = {"task": "会议接待补货", "room": room, "headcount": hc,
                   "verdict": verdict, "checks": checks, "anomalies": anomalies,
                   "advice": advice, "spoken": report,
                   "reported_at": datetime.now().isoformat()}
    trace["report_card"] = report_card
    _report_out(report_card)

    print("\n" + "=" * 66)
    print(f"任务{'完成 ✅' if verdict else '未完成 ❌(失败项已记录进 trace,供事后反思)'}\n")

    # ⑧ 生成新版 SOP(事后反思:读本次 trace 提炼经验规则 → reception_sop_v2.yaml)
    if reflect:
        try:
            from reflect import reflect_reception
            findings, new_rules, source, sop_out = reflect_reception(trace)
            trace["reflection"] = {
                "findings": findings, "new_rules": new_rules, "source": source,
                "sop_v2": os.path.relpath(sop_out, _ROOT) if sop_out else None}
            nr = f"新增 {len(new_rules)} 条经验规则" if new_rules else "无新增规则"
            _emit(8, "事后反思 → 新SOP", f"{source}:{nr}", "success")
        except Exception as exc:
            print(f"⑧ 事后反思跳过: {exc}")
            trace["reflection"] = {"error": str(exc)}
            _emit(8, "事后反思", f"跳过({exc})", "failure")

    # trace 落盘(含反思;deploy 展示 + 下次反思输入)
    out = os.path.join(_HERE, "last_reception_trace.json")
    json.dump(trace, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[trace → {os.path.relpath(out, _ROOT)}]")
    return trace


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="normal",
                    choices=["normal", "grasp_fail", "walk_blocked", "place_miss", "label_wrong"],
                    help="故障注入,仅 mock backend 有效")
    ap.add_argument("--backend", default=os.environ.get("RECEPTION_BACKEND", "mock"),
                    choices=["mock", "robot_api"],
                    help="mock=进程内世界(验证逻辑);robot_api=真实执行/观测总闸(接 VLA/导航)")
    ap.add_argument("--single", action="store_true",
                    help="单次补货:补一罐(walk→pick→walk→place 一轮),隐含最简放置")
    ap.add_argument("--simple-place", action="store_true",
                    help="最简放置:只确认放上桌,不查间隔/朝向")
    ap.add_argument("--headcount", type=int, default=4)
    ap.add_argument("--meeting-cola", type=int, default=1)
    ap.add_argument("--tea-cola", type=int, default=10)
    ap.add_argument("--no-reflect", action="store_true", help="跳过 ⑧ 事后反思(不联网 deepseek)")
    args = ap.parse_args()

    def _cli_step(no, phase, detail, status):
        mark = "✓" if status == "success" else ("✗" if status == "failure" else "·")
        print(f"   └[子任务{no}] {mark} {phase} — {detail}")

    trace = run_reception(
        scenario=args.scenario, backend=args.backend, headcount=args.headcount,
        meeting_cola=args.meeting_cola, tea_cola=args.tea_cola, single=args.single,
        simple_place=args.simple_place, reflect=not args.no_reflect, on_step=_cli_step)
    return 0 if trace.get("final_check", {}).get("verdict") else 1


if __name__ == "__main__":
    sys.exit(main())
