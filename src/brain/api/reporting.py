"""Deterministic summaries of Runtime status; no new task state or diagnosis."""
from shared.task_text import phase_text, BACKENDS, STATES


def reception_report(status):
    counts = status.get('attempt_summary') or {}
    state = status.get('state')
    failures = status.get('failure_history') or []
    unresolved = []
    if status.get('command_unknown'):
        unresolved.append({'step_id': status.get('control_step_id'),
                           'command_id': status.get('control_command_id'), 'reason': '原命令执行结果未知，停止未据此确认'})
    elif status.get('active'):
        unresolved.append({'step_id': status.get('control_step_id'),
                           'command_id': status.get('control_command_id'), 'reason': '本轮尚未结束'})
    if state == 'cancelled':
        unresolved.append({'reason': '大脑任务已取消；不代表下游命令已结束或机器人停止'})
    lines = [f"任务 ID：{status.get('task_id')}",
             f"任务环境：{BACKENDS.get(status.get('execution_backend'), status.get('execution_backend') or '未选择')}",
             f"本轮结果：{STATES.get(state, state or '未知')}，完成 {status.get('completed', 0)}/{status.get('total', 0)}",
             f"历史失败 {counts.get('failure_count', 0)} 次，重试 {counts.get('retry_count', 0)} 次，人工跳过 {len(status.get('manual_skips') or [])} 次"]
    if status.get('flow_finished') and state != 'succeeded':
        lines.append('流程已结束，含人工跳过，未全部成功。')
    kinds = {'rejected': '提交被拒绝', 'cancelled': '命令取消', 'execution_failed': '执行失败',
             'unknown': '执行结果未知'}
    for f in failures:
        lines.append(f"{phase_text(f.get('step_id'), status)}：{kinds.get(f.get('kind'), '失败记录')}；命令 {f.get('command_id')}")
        lines.append(f"失败说明：{f.get('display_message') or f.get('message') or f.get('code') or '未提供'}")
    for item in unresolved:
        lines.append('未确认事项：' + item['reason'] + ('；命令 ' + item['command_id'] if item.get('command_id') else ''))
    return {'task_id': status.get('task_id'), 'verdict': state == 'succeeded', 'state': state,
            'execution_backend': status.get('execution_backend'), 'attempt_summary': counts,
            'failure_history': failures, 'unresolved': unresolved,
            'checks': [{'name': item.get('display_name') or item['subtask'], 'pass': item['status'] == 'success', 'detail': item['result']}
                       for item in status.get('subtask_list', [])],
            'advice': status.get('blocked_reason') or '', 'spoken': state,
            'summary_text': '\n'.join(lines)}
