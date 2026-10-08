"""Read-only brain health and current task overview."""
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.request import ProxyHandler, build_opener
from shared.task_text import reason_text, task_context


def _get(path):
    with build_opener(ProxyHandler({})).open('http://127.0.0.1:5000' + path, timeout=2) as response:
        value = json.load(response)
        if not isinstance(value, dict):
            raise ValueError('大脑接口返回格式无效')
        return value


def current_alert(health, task, errors):
    # Alerts describe live facts. Old log lines are never an authority for motion.
    if 'health' in errors or 'task' in errors:
        return {'level': 'ERROR', 'message': '无法确认大脑健康或当前任务状态，请查看大脑日志。'}
    if health.get('ready') is not True or health.get('state') != 'healthy':
        return {'level': 'ERROR', 'message': '大脑尚未就绪：' + str(health.get('state') or '未知状态')}
    state = task.get('state')
    unresolved = (task.get('command_unknown') is True or task.get('resources_cleared') is False
                  or task.get('blocks_new_motion') is True)
    if unresolved or state in {'recovery_required', 'cancelling', 'waiting_human', 'paused'}:
        reason = task.get('blocked_reason') or {
            'recovery_required': '任务需要恢复处理', 'cancelling': '正在等待取消与停止确认',
            'waiting_human': '任务等待人工处理', 'paused': '任务已暂停',
        }.get(state, '原命令或资源状态尚未核清')
        return {'level': 'ERROR' if unresolved or state == 'recovery_required' else 'WARNING',
                'message': (task_context(task) + '：' if task_context(task) else '') + reason_text(reason),
                'task_id': task.get('task_id'), 'state': state}
    return None


def overview():
    paths = {'health': '/health', 'task': '/api/task_status', 'profile': '/api/execution_profile'}
    result, errors = {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = {name: pool.submit(_get, path) for name, path in paths.items()}
        for name, future in pending.items():
            try:
                result[name] = future.result()
            except Exception as exc:
                result[name] = {}
                errors[name] = str(exc)
    task = result['task']
    # No media, credentials or entire task history in the panel summary.
    result['task'] = {key: task.get(key) for key in (
        'task_id', 'task_desc', 'state', 'active', 'phase', 'blocked_reason',
        'command_unknown', 'resources_cleared', 'blocks_new_motion')}
    result['errors'] = errors
    result['alert'] = current_alert(result['health'], result['task'], errors)
    return result
