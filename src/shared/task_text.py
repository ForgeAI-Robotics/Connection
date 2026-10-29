"""Deterministic Chinese presentation; never changes task state or permissions."""
import json
import math
import re
from datetime import datetime


def _log_stamp(moment=None):
    """Process-log timestamp: ``2026-09-30 17:29:39,461``."""
    if isinstance(moment, str) and moment.strip():
        text = moment.strip().replace('T', ' ')
        if len(text) >= 23 and text[19] == '.':
            return text[:19] + ',' + text[20:23]
        return text[:23]
    stamp_at = moment or datetime.now().astimezone()
    return stamp_at.strftime('%Y-%m-%d %H:%M:%S,') + f'{stamp_at.microsecond // 1000:03d}'


def _compact_fields(body):
    """Fold single-line fields onto the message. Multiline errors stay intact below it."""
    lines = str(body or '').splitlines()
    if not lines:
        return ''
    head = lines[0].strip()
    index = 1
    while index < len(lines):
        line = lines[index]
        if not line.startswith('  ') or 'Traceback' in line:
            break
        nxt = index + 1
        if nxt < len(lines) and lines[nxt].strip() and not lines[nxt].startswith('  '):
            break
        folded = line.strip()
        if folded:
            head += ' ' + folded
        index += 1
    rest = '\n'.join(lines[index:]).strip('\n')
    return head if not rest else head + '\n' + rest


def beijing_block(level, body, *, name='master', moment=None):
    """One event per line: ``time - name - LEVEL - message fields``."""
    message = _compact_fields(body)
    return f'{_log_stamp(moment)} - {name} - {level} - {message}'


def _raw_value(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _exception_summary(value):
    """Keep a Python traceback in the archive. The process log only needs its last line."""
    text = _raw_value(value)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.startswith('Traceback') for line in lines):
        return lines[-1]
    return text


STATES = {'running': '执行中', 'verifying': '核验执行结果', 'waiting_human': '等待人工处理',
          'paused': '已暂停', 'cancelling': '等待停止确认', 'cancelled': '已取消',
          'recovery_required': '需要恢复处理', 'succeeded': '已完成', 'failed': '已失败'}
PHASES = {
    'BODY_NAV_TABLE2': '导航到 2 号桌', 'BODY_NAV_RELAY1': '导航到中转点 1',
    'BODY_NAV_RELAY2': '导航到门前', 'BODY_NAV_RELAY3': '横移通过门段',
    'BODY_NAV_TABLE1': '导航到 1 号桌', 'BODY_PICK': '抓取饮料', 'BODY_PLACE': '放置饮料',
    'VERIFY_BODY_PICK': '确认饮料已抓稳', 'VERIFY_BODY_PLACE': '确认饮料已放好',
    'INITIALIZING': '初始化接待任务', 'FETCHING_WORLD': '读取现场信息',
    'NAVIGATING_TO_TABLE2': '导航到 2 号桌', 'DREAM_INSPECTING_TABLE2': '检查 2 号桌上的物品',
    'VLA_PICKING': '抓取饮料', 'VERIFYING_GRASP': '确认饮料已抓稳',
    'NAVIGATING_TO_RELAY1': '导航到中转点 1',
    'NAVIGATING_TO_RELAY2': '导航到中转点 2', 'LATERAL_TO_RELAY3': '横移到中转点 3',
    'NAVIGATING_TO_TABLE1': '导航到 1 号桌', 'VLA_PLACING': '放置饮料',
    'VERIFYING_PLACE': '确认饮料已放好',
}
REASONS = {
    'STATIC_ACTIVATION_UNCONFIRMED': '导航准备未通过当前状态检查，目标尚未下发',
    'RECOVERY_COMMAND_REQUIRED': '目标与导航记录不一致，导航要求关联恢复命令；原命令结果仍需核对',
    'NAVIGATION_INPUT_INTERRUPTED': '导航定位输入中断或过期',
    'NAVIGATION_LOCALIZATION_LOST': '导航定位校验未通过',
    'MOTOR_HEALTH_NOT_READY': '电机保护或降温确认中，指令未受理',
    'SAFETY_NOT_READY': '导航执行条件未就绪，指令未受理',
    'STOP_NOT_CONFIRMED': '导航未收到新鲜的停止反馈，本次新指令未受理；原动作停止情况仍需核对',
    'submission_rejected': '指令被拒绝，未开始执行',
    'INVALID_LEG_ORDER': '导航步骤顺序不满足要求，具体前置步骤见原始错误',
    'NAVIGATION_NO_PATH': '导航无法规划可通行路径',
    'NAVIGATION_NO_PROGRESS': '导航持续无进展',
    'NAVIGATION_TERMINAL_UNREACHABLE': '导航末端未满足到达条件',
    'arrival_yaw_timeout': '最终朝向调整超时',
    'arrival_clearance_wait_timeout': '末端净空等待超时',
    'lateral_stop_not_settled': '横移停止未稳定',
    'NAVIGATION_EXECUTION_FAILED': '导航执行失败，具体原因见导航原始回包',
    'NAVIGATION_PROOF_INVALID': '缺少原导航到达并停止的有效回执，操控尚未执行；人工跳过导航不代表已经到达',
    'final_path_not_straight': '末端接近路径不满足直线要求，导航无法完成末端修正',
    'insufficient_final_straight': '末端接近的直线段不足，导航无法完成末端修正',
    'terminal_heading_recovery_requires_replan': '末端朝向恢复需要重新规划路径',
    'no_reliable_predictive_retry_window': '未满足到达条件，且没有可靠的预测停车重试空间',
    'stationary_goal_pose_drift': '停车后定位位姿偏离目标',
    'goal_overshot_without_reverse_token': '已越过目标，当前无可用后退动作',
    'residual_below_reliable_token_displacement': '剩余距离小于可靠动作位移',
    'position_hard_failure': '末端位置未达到要求',
    'final_yaw_position_hard_failure': '最终转向阶段位置未达到要求',
    'final_yaw_reversal_limit': '最终转向触发反向调整次数限制',
    'terminal_heading_drift': '末段行进朝向偏离',
    'fail': '执行未通过核验',
    'timeout': '未能在期限内确认执行结果',
    'real_execution_disabled': '真机执行未开启',
    'navigation_gate': '导航执行条件尚未满足',
    'command_unknown': '原动作指令的执行状态尚未确认',
    'vla_effect_confirmation': '操控等待暂停及真实效果核验；完成确认后继续核验原命令',
    'handoff_blocked': '导航与操控之间的控制权交接尚未确认',
    'Task accepted': '任务已接收', 'Task was not accepted': '任务未被接受',
}
HANDOFFS = {
    'simulated_manipulation_boundary': '模拟操控衔接已核验，未进行实体控制权交接',
    'manual_skip': '因人工跳过由大脑补齐，未向下游核验',
    'transport_ready_without_receipt': '下游未提供控制器凭证，按通路空闲放行',
    'controller_confirmed': '控制器凭证核验通过',
    'transport_not_ready': '导航或操控仍在占用动作通路',
    'source_stop_unconfirmed': '上一动作的停止尚未确认',
    'source_resources_unreleased': '上一动作的资源归还尚未确认',
    'source_receipt_invalid': '上一动作回执的身份或时间未通过核验',
    'source_failed': '上一动作已失败且已停止、资源已归还，不满足成功交接条件',
    'source_cancelled': '上一动作已取消且已停止、资源已归还，不满足成功交接条件',
    'source_success_unconfirmed': '上一动作尚无成功终态，交接未完成',
    'source_command_unavailable': '找不到上一动作的原指令',
    'control_status_unavailable': '无法读取导航或操控状态',
    'controller_receipt_invalid': '控制器凭证无效',
}
ACTIONS = {'query_original': '查询原指令的执行状态', 'request_help': '请现场负责人确认执行条件',
           'wait': '等待执行条件就绪', 'cancel': '通过原任务入口取消任务',
           'continue': '确认执行条件后，通过原任务入口继续'}
BACKENDS = {'reception_real': '真机接待', 'reception_protocol': '接待协议模拟',
            'reception_sim_navigation_real': '导航模拟＋操控真机（不含真实移动与运送）',
            'reception_ros_sim_manipulation': '真实 ROS 导航项目仿真＋操控模拟',
            'reception_real_sim_manipulation': '导航真机＋操控模拟（不含真实抓放）',
            'reception_simple': 'SIMPLE 物理仿真接待',
            'body_simple': 'SIMPLE 通用身体任务', 'body_real': '真机通用身体任务',
            'unselected': '尚未选择', 'desk': '桌面仿真'}
SKILLS = {'local': '本地', 'navigate': '导航', 'inspect': '检查', 'pick': '抓取',
          'place': '放置', 'verify': '核验', 'describe': '观察', 'desk_check': '桌面检查',
          'mock': '模拟'}
EVENTS = {
    'task_restored': '已读取原任务状态',
    'task_opened': '已接收任务', 'execution_selected': '已选择执行流程', 'plan_ready': '计划已生成',
    'drive_cursor': '准备执行', 'gate': '执行条件未满足', 'recovery_advised': '已生成处理建议',
    'recovery_scene_observed': '已读取执行端状态，等待重新分析',
    'recovery_plan_applied': '已接受恢复方案，等待执行与核验',
    'submit_intent': '准备下发动作指令', 'observe_intent': '准备读取现场状态',
    'action_finished': '已收到动作返回，等待核验', 'step_passed': '当前步骤核验通过',
    'task_succeeded': '任务完成', 'task_finished_with_skips': '流程结束，含人工跳过的步骤',
    'execution_error': '执行异常', 'unconfirmed': '执行结果尚未确认',
    'submission_rejected': '指令被下游拒绝，未开始执行',
    'pause_intent': '收到暂停请求', 'pause_requested': '暂停请求已处理',
    'pause_resumed': '任务恢复', 'human_continue': '收到人工继续请求',
    'cancel_intent': '收到取消请求', 'cancel_unclear': '取消结果尚未核清',
    'task_cancelled': '本轮任务已取消，不再执行后续步骤',
    'cancel_accepted': '取消请求已受理，等待停止确认', 'cancel_resolved': '取消已确认',
    'requery': '查询原指令', 'requery_unknown': '原指令状态未知',
    'requery_not_started_seen': '已确认原指令未开始', 'requery_stopped_seen': '已确认原指令已停止',
    'requery_ended_running': '查询结束时原指令仍在运行', 'requery_rejected': '原指令查询被拒绝',
    'continue_verifying_original': '继续核验原指令', 'continue_attempt_prepared': '已准备重新尝试当前步骤',
    'step_manually_skipped': '当前步骤已人工跳过，未确认成功',
    'same_attempt': '继续原执行尝试', 'new_attempt': '已创建新的执行尝试',
    'handoff_observed': '已收到控制权交接结果', 'handoff_blocked': '控制权交接受阻',
    'handoff_recheck_requested': '重新检查控制权交接', 'resume_original_confirmed': '已确认可继续原指令',
    'submit_precondition_rejected': '指令下发前置检查未通过', 'verdict': '已得到核验结论',
    'observation_incomplete': '现场观测信息不完整', 'scene_observed': '已更新现场观测',
    'progress_ignored': '忽略不匹配的进度回报',
    'reflection_completed': '任务复盘完成', 'reflection_failed': '任务复盘失败',
}


def reason_text(reason):
    value = str(reason or '')
    if value.startswith('submission_rejected:'):
        code = value.split(':', 1)[1]
        return '指令被拒绝，未开始执行：' + reason_text(code)
    return f'{REASONS[value]}（{value}）' if value in REASONS else value


FAILURE_PHRASES = {
    'terminal correction scan coverage is insufficient': '末端修正受阻：扫描覆盖不足，导航无法确认修正区域的通行条件',
    'Agent navigation result wait timed out': '导航等待执行结果超时',
    'terminal correction is not bound to this stopped goal': '导航末端修正与已停止目标的绑定校验失败',
    'localization pose is not in inflated known-free space': '当前定位点不在地图膨胀后的已知可通行区域内',
    'goal is not in inflated known-free space': '目标点不在地图膨胀后的已知可通行区域内',
    'no route exists in inflated known-free space': '地图膨胀后的已知可通行区域内不存在连通路径',
    'terminal pose adjustment made no progress for ': '末端位姿调整持续无进展，导航已报告失败',
    'terminal correction has insufficient known-free turn clearance': '导航判断末端修正的可通行转向空间不足',
    'terminal correction has a live nearby obstacle; holding stand': '导航检测到附近障碍，末端修正受阻，正在保持站立',
}


def phase_labels(record):
    """Names belong to the task's frozen steps, never to today's skill file."""
    labels = dict((record or {}).get('phase_labels') or {})
    for step in (record or {}).get('phase_specs') or []:
        if step.get('display_name'):
            labels[step['step_id']] = step['display_name']
    return labels


def target_labels(record):
    candidates = {}
    for step in (record or {}).get('phase_specs') or []:
        if step.get('target_name') and step.get('kind') == 'navigate':
            for key in (step.get('key'), (step.get('navigation') or {}).get('target_id')):
                if key:
                    candidates.setdefault(key, set()).add(step['target_name'])
    # A shared target_id (for example two door legs) may have different stance names.
    return {key: next(iter(names)) for key, names in candidates.items() if len(names) == 1}


def phase_text(phase, record=None):
    return phase_labels(record).get(phase) or PHASES.get(phase, phase or '')


def failure_text(message, code=None, *, record=None, details=None):
    """Translate recognized downstream failures without inferring a physical cause."""
    raw = str(message or code or '')
    chinese = REASONS.get(raw)
    order = re.fullmatch(r'(\w+) navigation must succeed under the same task_id before (\w+)', raw)
    if order:
        targets = {'table2': '2 号桌', 'table_2': '2 号桌', 'relay2': '中转点 2',
                   'relay3': '中转点 3', 'table1': '1 号桌', 'table_1': '1 号桌'}
        targets.update(target_labels(record))
        before, after = (targets.get(name, name) for name in order.groups())
        chinese = f'导航顺序不满足：同一任务内必须先成功到达{before}，才能前往{after}'
    if not chinese:
        chinese = next((text for phrase, text in FAILURE_PHRASES.items() if phrase in raw), None)
    chinese = chinese or REASONS.get(code) or '执行异常，详情见原始错误'
    # Interpret stable fields, never gate evidence on an error name or message.
    sources = [('错误详情回包', details)] if isinstance(details, dict) else []
    if isinstance(details, dict) and isinstance(details.get('terminal'), dict):
        sources.append(('导航终端回包', details['terminal']))
    for source_label, terminal in sources:
        metrics = []
        for key, scale, label, unit in (
                ('goal_distance', 100, '距目标', ' cm'),
                ('final_yaw_error', 180 / math.pi, '最终朝向误差', '°')):
            value = terminal.get(key) if isinstance(terminal, dict) else None
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and (key != 'goal_distance' or value >= 0)):
                converted = abs(value) * scale
                if math.isfinite(converted):
                    metrics.append(f'{label} {converted:.1f}{unit}')
        if metrics:
            chinese += '；' + source_label + '：' + '，'.join(metrics)
        clearance = terminal.get('turn_clearance') if isinstance(terminal, dict) else None
        if isinstance(clearance, dict):
            clearance_metrics = []
            if clearance.get('allowed') is True:
                clearance_metrics.append('允许转向')
            elif clearance.get('allowed') is False:
                clearance_metrics.append('禁止转向')
            reason = clearance.get('reason')
            for key, label in (('nearest_m', '最近障碍'), ('required_m', '要求净空')):
                value = clearance.get(key)
                # Some deployed peers keep nearest_m only in the original reason.
                if key not in clearance and isinstance(reason, str):
                    match = re.search(r'\b' + key + r'=([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?=,|;|\s|$)', reason)
                    value = float(match[1]) if match else None
                if (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and value >= 0 and math.isfinite(value * 100)):
                    clearance_metrics.append(f'{label} {value * 100:.1f} cm')
            if clearance_metrics:
                chinese += '；净空回包（采样快照）：' + '，'.join(clearance_metrics)
            if isinstance(reason, str) and reason:
                chinese += '；净空原文：' + reason
    return f'{chinese}；原始错误：{raw}' + (f'（{code}）' if code and code not in raw else '')


def manipulation_is_simulated(record):
    return (record or {}).get('execution_backend') in {
        'reception_real_sim_manipulation', 'reception_ros_sim_manipulation'}


def scope_manipulation_advice(advice, record):
    """Qualify both saved and newly generated prose without changing its action."""
    navigation_simulated = (record or {}).get('execution_backend') == 'reception_sim_navigation_real'
    if not advice or not (manipulation_is_simulated(record) or navigation_simulated):
        return advice
    result = dict(advice)
    prefix = ('模拟导航说明：导航到达和运送仅为模拟，不能证明机器人实际到位；操控效果按真实回执核验。\n'
              if navigation_simulated else '模拟操控说明：下述开门、持物、抓取和放置仅指模拟状态，不代表真实效果。\n')
    for key in ('summary', 'reason'):
        text = result.get(key)
        if isinstance(text, str) and text and not text.startswith(prefix):
            result[key] = prefix + text
    return result


def task_context(record):
    phase = record.get('phase') or record.get('step_id')
    return phase_text(phase, record)


def presentation_labels():
    return {'states': STATES, 'phases': PHASES, 'reasons': REASONS, 'actions': ACTIONS,
            'backends': BACKENDS, 'failure_phrases': FAILURE_PHRASES}


def format_plan(steps):
    parts = []
    for index, step in enumerate(steps or [], 1):
        if isinstance(step, str):
            step = {'step_id': step}
        if not isinstance(step, dict):
            continue
        step_id = str(step.get('step_id') or '')
        name = step.get('display_name') or PHASES.get(step_id, step_id or '未命名步骤')
        detail = [step_id] if step_id else []
        kind = step.get('kind')
        if kind:
            detail.append(SKILLS.get(kind, str(kind)))
        if step.get('key'):
            detail.append('目标 ' + str(step.get('target_name') or step['key']))
        if step.get('target_area'):
            detail.append('区域 ' + str(step['target_area']))
        if step.get('requires_gate'):
            detail.append('需开启真机执行')
        if step.get('optional'):
            detail.append('可跳过')
        label = f'{index}.{name}'
        if detail:
            label += '（' + '，'.join(detail) + '）'
        parts.append(label)
    return ' '.join(parts)


def readable_call_fields(fields):
    """Deduplicate the reading view without changing the archived call receipt."""
    view = dict(fields)
    repeated = view.pop('error_already_logged', False)
    error = view.get('error')
    if repeated:
        for key in ('error', 'reason', 'result'):
            view.pop(key, None)
    elif isinstance(error, dict):
        message = error.get('message')
        if message and view.get('reason') == message:
            view.pop('reason', None)
        result = view.get('result')
        if message and isinstance(result, dict) and result.get('message') == message:
            view['result'] = {key: value for key, value in result.items() if key != 'message'}
    return view


def format_process_event(record):
    """One bilingual block for the process log. Empty means archive only."""
    kind, event = record.get('kind'), record.get('event')
    if record.get('_human_suppressed'):
        return ''
    if kind == 'CALL':
        record = readable_call_fields(record)
    if kind == 'TASK' and event == 'reflection_completed':
        task = str(record.get('task_id') or record.get('id') or '')
        return beijing_block('INFO', '任务复盘完成，详情已保存' +
                             (f' task={task[:8]}' if task else ''), moment=record.get('time'))
    if kind == 'TASK' and event == 'verdict':
        state = record.get('state')
        reason = record.get('blocked_reason')
        conclusion = ('未通过，任务等待处理' if reason == 'fail' or state == 'failed' else
                      '结果待确认，任务等待处理' if reason or state == 'recovery_required' else
                      '通过')
        task = str(record.get('task_id') or record.get('id') or '')
        command = record.get('command_id') or record.get('cmd') or ''
        return beijing_block('INFO' if conclusion == '通过' else 'ERROR',
                             f'大脑核验｜{conclusion} task={task[:8]} cmd={command}',
                             moment=record.get('time'))
    advice = record.get('recovery_advice') or {}
    if kind == 'TASK' and event == 'recovery_advised' and not any((
            advice.get('error'), record.get('error'), record.get('exception'))):
        return ''
    phase = record.get('step_id')
    task = str(record.get('task_id') or record.get('id') or '')
    message, tag = None, '任务'
    if kind == 'CONTROL':
        action = {'continue': '继续', 'skip': '跳过', 'pause': '暂停', 'cancel': '取消任务'}.get(record.get('action'), record.get('action') or '')
        message = {'received': '收到控制请求，等待处理', 'processing': '正在处理控制请求',
                   'accepted': '控制请求已处理；下发与执行结果见后续日志',
                   'rejected': '控制请求未受理'}.get(event, '控制请求') + '：' + action
    elif kind == 'ADVICE':
        message = {'started': '后台开始分析异常建议，不阻塞任务推进',
                   'queued': '异常建议后台繁忙，保留最新待分析状态；任务推进不等待',
                   'completed': '异常建议已更新',
                   'discarded': '任务状态已变化，丢弃迟到异常建议'}.get(event, '后台异常建议')
    elif kind == 'TASK' and event in EVENTS:
        message = EVENTS[event]
        if event in {'task_opened', 'task_restored'}:
            message += '：' + str(record.get('task_desc') or '任务')
            if event == 'task_restored' and phase:
                message += '；当前步骤：' + phase_text(phase, record)
                if phase in PHASES:
                    message += f'（{phase}）'
        elif event == 'execution_selected':
            backend = record.get('backend')
            message += '：' + BACKENDS.get(backend, backend or '未知')
        elif event == 'plan_ready':
            steps = record.get('plan_steps') or []
            count = record.get('step_count') or len(steps)
            if count:
                message += f'，共 {count} 步'
            rendered = format_plan(steps)
            if rendered:
                message += '：' + rendered
        elif phase:
            message += '：' + phase_text(phase, record)
            if phase in PHASES:
                message += f'（{phase}）'
        if event == 'step_manually_skipped' and record.get('next_step_id'):
            following = record['next_step_id']
            message += '；下一步：' + phase_text(following, record)
        if event == 'handoff_observed' and record.get('handoff_reason'):
            outcome = record['handoff_reason']
            message += '；' + ('已确认' if record.get('handoff_confirmed') else '未确认')
            message += '，' + HANDOFFS.get(outcome, outcome) + f'（{outcome}）'
            skipped = record.get('skipped_steps') or []
            if skipped:
                message += '；跨越：' + '、'.join(phase_text(step, record) for step in skipped)
        if event == 'gate':
            tag = '等待'
        elif event == 'recovery_advised':
            tag = '建议'
        elif phase and event not in {'task_opened', 'task_restored', 'execution_selected', 'plan_ready', 'task_succeeded'}:
            tag = '步骤'
        reason = record.get('blocked_reason')
        if reason:
            message += '；' + reason_text(reason)
        state = record.get('state')
        if state in {'waiting_human', 'recovery_required', 'cancelling', 'paused', 'failed'} or (event == 'task_restored' and state in STATES):
            message += '；' + STATES[state] + f'（{state}）'
        if event == 'recovery_advised':
            message = '生成处理建议时出错'
    elif kind == 'PREFLIGHT' and event == 'boot':
        return ''
    elif kind == 'PREFLIGHT' and event == 'http':
        tag = '预检'
        message = ('无需额外预检（downstream not required）' if record.get('required') is False else
                   '预检通过（preflight passed）' if record.get('ready') is True else
                   '预检未通过（preflight failed）')
        if record.get('blockers'):
            message += '：' + '；'.join(map(str, record['blockers']))
    elif kind == 'INBOUND':
        tag = '请求'
        message = {'publish': '收到任务指令', 'preflight': '收到预检请求', 'chat': '收到闲聊消息'}.get(event)
        if message:
            source_en = record.get('source') or ''
            source = {'web': '网页', 'deploy': '网页', 'feishu': '飞书', 'voice': '语音'}.get(source_en, source_en)
            message += f"：{record.get('text', '')}"
            if source:
                message += f'（来源：{source}' + (f' {source_en}' if source_en else '') + '）'
            route = ' '.join(str(record[key]) for key in ('method', 'path') if record.get(key))
            if route:
                message += ' ' + route
    elif kind == 'CALL':
        peer_en = record.get('peer') or 'downstream'
        peer = {'dream': '导航', 'vla': '操控'}.get(peer_en, peer_en or '下游')
        state = record.get('state')
        error = record.get('error')
        error_body = error if isinstance(error, dict) else {}
        if state == 'failed':
            outcome = '失败'
            explanation = failure_text(error_body.get('message'), error_body.get('code')).split('；原始错误：', 1)[0]
            if error_body.get('code') == 'NATIVE_SAFETY_LATCHED':
                explanation = '对端控制器保护已锁定'
        elif state == 'cancelled':
            outcome, explanation = '已取消', '对端报告命令已取消'
        elif state == 'succeeded' and record.get('ok') is not False:
            outcome, explanation = '成功', '对端报告命令成功，待大脑核验'
        elif event == 'end' and record.get('ok') is not True:
            failure = str(record.get('exception') or record.get('reason') or error_body.get('message') or '')
            if re.search(r'超时|\b(?:timed out|timeout(?:error)?)\b', failure, re.IGNORECASE):
                outcome, explanation = '超时', '请求或等待执行结果超过时限'
            else:
                outcome, explanation = '调用异常', '未取得有效回包，详情见原始错误'
        else:
            outcome = {'start': '开始', 'accepted': '已受理'}.get(event, STATES.get(state, state or '处理中'))
            explanation = '等待执行结果' if event == 'accepted' else ''
        message = f'{peer}调用更新｜{outcome}' + (f'｜{explanation}' if explanation else '')
        if record.get('duration_s') is not None:
            message += f"｜耗时 {record['duration_s']:.3f} 秒"
        meta = ' '.join(f'{key}={record[key]}' for key in ('task_id', 'cmd', 'command_id', 'target', 'goal') if record.get(key))
        lines = [message + (' ' + meta if meta else '')]
        if error:
            lines.append('error=' + _raw_value(error))
        exception = record.get('exception')
        if exception and not (error_body.get('message') and str(error_body['message']) in str(exception)):
            lines.append('exception=' + _exception_summary(exception))
        elif not exception and not error and record.get('reason'):
            lines.append('reason=' + str(record['reason']))
        return beijing_block('ERROR' if outcome in {'失败', '超时', '调用异常'} else 'INFO',
                             '\n'.join(lines), moment=record.get('time'))
    if message is None:
        message = ' '.join(part for part in (str(kind or ''), str(event or '')) if part).strip()
    if not message:
        return None
    diagnostic = record.get('diagnostic') or {}
    failed = bool(record.get('blocked_reason') or record.get('error') or record.get('exception')
                  or diagnostic.get('error') or advice.get('error')
                  or record.get('ok') is False or record.get('state') == 'failed'
                  or (kind == 'PREFLIGHT' and record.get('ready') is False))
    level = 'ERROR' if failed else 'INFO'
    shown_task = task if event in {'task_opened', 'task_restored'} else task[:8]
    meta = []
    if shown_task:
        meta.append(f'task={shown_task}')
    if event:
        meta.append(f'event={event}')
    if record.get('state'):
        meta.append(f"state={record['state']}")
    if kind == 'INBOUND' and record.get('source'):
        meta.append(f"source={record['source']}")
    if record.get('from'):
        meta.append(f"caller={record['from']}")
    if record.get('via'):
        meta.append(f"via={record['via']}")
    command = record.get('command_id') or record.get('cmd')
    if command:
        meta.append(f'cmd={command}')
    detail = ['  ' + ' '.join(meta)] if meta else []
    for error in (diagnostic.get('error'), advice.get('error')):
        if error:
            detail.append('  error=' + _raw_value(error))
    if diagnostic.get('error') and record.get('attempt_id'):
        detail.append(f"  attempt_id={record['attempt_id']}")
    # Preserve additional business results, errors and details without repeating identifiers.
    used = {'time', 'kind', 'event', 'task_id', 'id', 'source', 'from', 'via', 'component',
            'state', 'backend', 'revision', 'environment_revision', 'release_id', 'step_id',
            'command_id', 'cmd', 'attempt_id', 'blocked_reason', 'advice_action', 'task_desc', 'step_count',
            'diagnostic', 'recovery_advice', 'method', 'path', 'plan_steps', 'phase_labels',
            'next_step_id', 'handoff_reason', 'handoff_confirmed', 'skipped_steps'}
    if kind == 'INBOUND':
        used.update({'text', 'intent', 'risk'})
        for flag in ('force_new', 'resume'):
            if not record.get(flag):
                used.add(flag)
    if kind == 'CALL':
        used.update({'peer', 'ok'})
    if kind == 'PREFLIGHT':
        used.update({'ready', 'required', 'blockers', 'note', 'text'})
    if event == 'reflection_completed':
        used.add('result')
    exception = record.get('exception')
    if exception:
        used.add('exception')
        summary = _exception_summary(exception)
        if summary and summary not in message:
            detail.append('  exception=' + summary)
    extra = []
    for key, value in record.items():
        if key not in used and value is not None and value != '':
            extra.append(f'{key}={_raw_value(value)}')
    if extra:
        detail.append('  ' + '；'.join(extra))
    body = message if not detail else message + '\n' + '\n'.join(detail)
    return beijing_block(level, body, moment=record.get('time'))
