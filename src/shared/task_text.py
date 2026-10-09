"""Deterministic Chinese presentation; never changes task state or permissions."""
import json
import re
from datetime import datetime


def _log_stamp(moment=None):
    """RoboAgent clock: ``2026-09-30 17:29:39,461``."""
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
    'INITIALIZING': '初始化接待任务', 'FETCHING_WORLD': '读取现场信息',
    'NAVIGATING_TO_TABLE2': '导航到 2 号桌', 'DREAM_INSPECTING_TABLE2': '检查 2 号桌上的物品',
    'VLA_PICKING': '抓取饮料', 'VERIFYING_GRASP': '确认饮料已抓稳',
    'NAVIGATING_TO_RELAY2': '导航到中转点 2', 'LATERAL_TO_RELAY3': '横移到中转点 3',
    'NAVIGATING_TO_TABLE1': '导航到 1 号桌', 'VLA_PLACING': '放置饮料',
    'VERIFYING_PLACE': '确认饮料已放好',
}
REASONS = {
    'MOTOR_HEALTH_NOT_READY': '电机保护或降温确认中，指令未受理',
    'SAFETY_NOT_READY': '导航执行条件未就绪，指令未受理',
    'submission_rejected': '指令被拒绝，未开始执行',
    'INVALID_LEG_ORDER': '导航步骤顺序不满足要求，具体前置步骤见原始错误',
    'NAVIGATION_NO_PATH': '导航无法规划可通行路径',
    'NAVIGATION_NO_PROGRESS': '导航持续无进展',
    'NAVIGATION_TERMINAL_UNREACHABLE': '导航末端未满足到达条件',
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
    'real_execution_disabled': '真机动作许可未开启',
    'navigation_gate': '导航执行条件尚未满足',
    'command_unknown': '原动作指令的执行状态尚未确认',
    'handoff_blocked': '导航与操控之间的控制权交接尚未确认',
    'Task accepted': '任务已接收', 'Task was not accepted': '任务未被接受',
}
HANDOFFS = {
    'manual_skip': '因人工跳过由大脑补齐，未向下游核验',
    'transport_ready_without_receipt': '下游未提供控制器凭证，按通路空闲放行',
    'controller_confirmed': '控制器凭证核验通过',
    'transport_not_ready': '导航或操控仍在占用动作通路',
    'source_stop_unconfirmed': '上一动作的停止或资源归还未确认',
    'source_command_unavailable': '找不到上一动作的原指令',
    'control_status_unavailable': '无法读取导航或操控状态',
    'controller_receipt_invalid': '控制器凭证无效',
}
ACTIONS = {'query_original': '查询原指令的执行状态', 'request_help': '请现场负责人确认执行条件',
           'wait': '等待执行条件就绪', 'cancel': '通过原任务入口取消任务',
           'continue': '确认执行条件后，通过原任务入口继续'}
BACKENDS = {'reception_real': '真机接待', 'reception_protocol': '接待协议模拟',
            'reception_simple': 'SIMPLE 物理仿真接待',
            'unselected': '尚未选择', 'desk': '桌面仿真'}
SKILLS = {'local': '本地', 'navigate': '导航', 'inspect': '检查', 'pick': '抓取',
          'place': '放置', 'verify': '核验', 'describe': '观察', 'desk_check': '桌面检查',
          'mock': '模拟'}
EVENTS = {
    'task_restored': '已读取原任务状态',
    'task_opened': '已接收任务', 'execution_selected': '已选择执行流程', 'plan_ready': '计划已生成',
    'drive_cursor': '准备执行', 'gate': '执行条件未满足', 'recovery_advised': '已生成处理建议',
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


def failure_text(message, code=None):
    """Translate recognized downstream failures without inferring a physical cause."""
    raw = str(message or code or '')
    phrases = {
        'localization pose is not in inflated known-free space': '当前定位点不在地图膨胀后的已知可通行区域内',
        'goal is not in inflated known-free space': '目标点不在地图膨胀后的已知可通行区域内',
        'no route exists in inflated known-free space': '地图膨胀后的已知可通行区域内不存在连通路径',
        'terminal pose adjustment made no progress for ': '末端位姿调整持续无进展，导航已报告失败',
    }
    chinese = REASONS.get(raw)
    order = re.fullmatch(r'(\w+) navigation must succeed under the same task_id before (\w+)', raw)
    if order:
        targets = {'table2': '2 号桌', 'table_2': '2 号桌', 'relay2': '中转点 2',
                   'relay3': '中转点 3', 'table1': '1 号桌', 'table_1': '1 号桌'}
        before, after = (targets.get(name, name) for name in order.groups())
        chinese = f'导航顺序不满足：同一任务内必须先成功到达{before}，才能前往{after}'
    if not chinese:
        chinese = next((text for phrase, text in phrases.items() if phrase in raw), None)
    chinese = chinese or REASONS.get(code) or '执行异常，详情见原始错误'
    return f'{chinese}；原始错误：{raw}' + (f'（{code}）' if code and code not in raw else '')


def task_context(record):
    phase = record.get('phase') or record.get('step_id')
    return PHASES.get(phase, phase or '')


def presentation_labels():
    return {'states': STATES, 'phases': PHASES, 'reasons': REASONS, 'actions': ACTIONS}


def format_plan(steps):
    parts = []
    for index, step in enumerate(steps or [], 1):
        if isinstance(step, str):
            step = {'step_id': step}
        if not isinstance(step, dict):
            continue
        step_id = str(step.get('step_id') or '')
        name = PHASES.get(step_id, step_id or '未命名步骤')
        detail = [step_id] if step_id else []
        kind = step.get('kind')
        if kind:
            detail.append(SKILLS.get(kind, str(kind)))
        if step.get('key'):
            detail.append('目标 ' + str(step['key']))
        if step.get('target_area'):
            detail.append('区域 ' + str(step['target_area']))
        if step.get('requires_gate'):
            detail.append('需真机许可')
        if step.get('optional'):
            detail.append('可跳过')
        label = f'{index}.{name}'
        if detail:
            label += '（' + '，'.join(detail) + '）'
        parts.append(label)
    return ' '.join(parts)


def format_process_event(record):
    """One bilingual block for the process log. Empty means archive only."""
    kind, event = record.get('kind'), record.get('event')
    advice = record.get('recovery_advice') or {}
    if kind == 'TASK' and event == 'recovery_advised' and not any((
            advice.get('error'), record.get('error'), record.get('exception'))):
        return ''
    phase = record.get('step_id')
    task = str(record.get('task_id') or record.get('id') or '')
    message, tag = None, '任务'
    if kind == 'TASK' and event in EVENTS:
        message = EVENTS[event]
        if event in {'task_opened', 'task_restored'}:
            message += '：' + str(record.get('task_desc') or '任务')
            if event == 'task_restored' and phase:
                message += '；当前步骤：' + PHASES.get(phase, phase)
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
            message += '：' + PHASES.get(phase, phase)
            if phase in PHASES:
                message += f'（{phase}）'
        if event == 'step_manually_skipped' and record.get('next_step_id'):
            following = record['next_step_id']
            message += '；下一步：' + PHASES.get(following, following)
        if event == 'handoff_observed' and record.get('handoff_reason'):
            outcome = record['handoff_reason']
            message += '；' + ('已确认' if record.get('handoff_confirmed') else '未确认')
            message += '，' + HANDOFFS.get(outcome, outcome) + f'（{outcome}）'
            skipped = record.get('skipped_steps') or []
            if skipped:
                message += '；跨越：' + '、'.join(PHASES.get(step, step) for step in skipped)
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
        tag = '调用'
        peer_en = record.get('peer') or 'downstream'
        peer = {'dream': '导航', 'vla': '操控'}.get(peer_en, peer_en or '下游')
        action = {'start': '开始调用', 'accepted': '已受理', 'state': '状态更新', 'end': '调用结束'}.get(event)
        if action:
            state = record.get('state')
            state_text = ''
            if state:
                state_text = f"：{STATES.get(state, state)}"
                if state in STATES:
                    state_text += f'（{state}）'
            message = f'{peer}（{peer_en}）{action}{state_text}'
            route = ' '.join(str(record[key]) for key in ('method', 'path') if record.get(key))
            if route:
                message += ' ' + route
            if record.get('ok') is not None:
                message += '；结果=' + ('成功' if record['ok'] else '未成功')
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
            'diagnostic', 'recovery_advice', 'method', 'path', 'plan_steps',
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
