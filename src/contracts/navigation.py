"""Validate the existing NAV wire body without imposing a reception route."""
import math
import re

from contracts.tasks import CONTRACT_VERSION


NAV_FIELDS = frozenset({
    'contract_version', 'task_id', 'command_id', 'target_id', 'route_phase',
    'leg_index', 'frame_id', 'goal_xyt', 'motion_mode', 'require_final_orientation',
})


def validate_navigation_body(body):
    if not isinstance(body, dict) or set(body) != NAV_FIELDS:
        raise ValueError('导航请求字段缺失或包含未约定字段')
    if body['contract_version'] != CONTRACT_VERSION:
        raise ValueError('导航合同版本不匹配')
    for key in ('task_id', 'command_id'):
        if not isinstance(body[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', body[key]):
            raise ValueError(key + ' 必须为 1～160 字符的命令标识')
    if not isinstance(body['target_id'], str) or not body['target_id'].strip():
        raise ValueError('target_id 必须为非空字符串')
    if not isinstance(body['route_phase'], str):
        raise ValueError('route_phase 必须为字符串')
    if type(body['leg_index']) is not int or body['leg_index'] <= 0:
        raise ValueError('leg_index 必须为正整数')
    goal = body['goal_xyt']
    if (not isinstance(goal, list) or len(goal) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in goal)):
        raise ValueError('goal_xyt 必须为三个有限数，单位米／米／弧度')
    if body['frame_id'] != 'map':
        raise ValueError('导航仅接受 map 坐标系')
    if body['motion_mode'] not in ('forward_path', 'lateral_path_aligned'):
        raise ValueError('导航运动模式不支持')
    if body['require_final_orientation'] is not True:
        raise ValueError('当前大脑导航合同要求最终朝向')
    return body
