"""Synthetic VLA runtime replies; never access a robot."""
from datetime import datetime, timezone


def status(*, running=True, managed=True, can_stop=True, ready=True):
    return {'schema': 'fq/vla-runtime-status/v1',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'http': {'state': 'running' if running else 'stopped', 'ready': running,
                     'managed': managed, 'can_stop': running and can_stop,
                     'reason': '' if can_stop else 'active task'},
            'camera': {'state': 'running', 'ready': ready if running else None,
                       'frames_verified': running,
                       'streams': {key: {'ready': ready, 'fps': 29.9, 'age_ms': 20}
                                   for key in ['ego_view', 'left_wrist', 'right_wrist']} if running else {}},
            'o6': {'state': 'running', 'ready': True, 'reason': ''}}


def action_result(action='start', **changes):
    value = {'schema': 'fq/vla-runtime-action/v1', 'action': action,
             'ok': True, 'confirmed': True,
             'message': 'HTTP 已停止；相机与 O6 保留' if action == 'stop' else 'VLA 已启动并核验',
             'status': status(running=action != 'stop')}
    value.update(changes)
    return value
