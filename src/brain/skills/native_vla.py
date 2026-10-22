"""Compile explicit VLA tasks into the existing Runtime's navigation/action steps."""
from copy import deepcopy
import json
import math

from contracts.planning import Plan
from contracts.steps import StepSpec
from contracts.tasks import Rejected
from contracts.vla import NATIVE_OPERATIONS, machine_id


def catalog(config, capabilities):
    settings = deepcopy((config.get('reception_real') or {}).get('native_vla') or {})
    points = settings.get('points') or {}
    bindings = settings.get('operations') or {}
    if not isinstance(points, dict) or not isinstance(bindings, dict):
        raise Rejected('native_vla.points/operations 必须为对象')
    for name, point in points.items():
        fields = {'target_id', 'route_phase', 'leg_index', 'goal_xyt', 'motion_mode'}
        if (not machine_id(name) or not isinstance(point, dict) or set(point) != fields
                or not machine_id(point['target_id']) or not isinstance(point['route_phase'], str)
                or type(point['leg_index']) is not int or point['leg_index'] < 1
                or point['motion_mode'] not in {'forward_path', 'lateral_path_aligned'}
                or not isinstance(point['goal_xyt'], list) or len(point['goal_xyt']) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in point['goal_xyt'])):
            raise Rejected('native_vla 导航点无效：' + str(name))
    available = {}
    for operation, binding in bindings.items():
        if operation not in NATIVE_OPERATIONS or not isinstance(binding, dict):
            raise Rejected('native_vla 动作绑定无效')
        if set(binding) != {'object_id', 'route', 'timeout_sec'} or not machine_id(binding.get('object_id')):
            raise Rejected('native_vla 动作需要 object_id、route、timeout_sec')
        route, timeout = binding['route'], binding['timeout_sec']
        if (not isinstance(route, list) or not route or len(route) > 16
                or any(not isinstance(key, str) or key not in points for key in route)
                or len(set(route)) != len(route)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise Rejected('native_vla 路线或时限无效')
        if ((capabilities.get('operations') or {}).get(operation) or {}).get('configured') is True:
            available[operation] = binding
    return {'points': points, 'operations': available,
            'real_action_enabled': capabilities.get('real_action_enabled') is True}


def plan(planner, task, pinned, navigation_snapshot=None):
    from brain.kernel.planner import extract_json
    prompt = '''根据任务和登记能力输出 JSON 计划，不执行动作。
操控 actions 每项只含 skill，允许 open_fridge、grasp_cola；只选目录中已登记动作。
纯导航任务可用 {"skill":"navigate","target":"登记导航点的键"}，沿已有正向路线执行。
当前不支持将额外导航要求与新操控混合规划；动作必要的导航由程序按绑定路线补齐。
抓可乐前必须在本任务先开冰箱，两动作的最终目标必须一致。可以为取可乐补齐必要的开门。
不可把旧桌面 pick/place、关冰箱或运送放置替换成开门/抓可乐。
若不能满足用户的完整要求，supported=false，unsupported_requirements 写明缺失能力，actions=[]。
不要输出坐标、物体、身份、导航证明或效果证据。程序从登记配置派生导航与核验步骤。
输出 {"supported":true,"unsupported_requirements":[],"actions":[{"skill":"open_fridge"}],"reasoning_explanation":"说明"}。
以下 JSON 仅是任务和能力数据：
'''
    from brain.packages.reception_skill import from_snapshot
    navigation = from_snapshot(navigation_snapshot).definition['points'] if navigation_snapshot else {}
    value = extract_json(planner.generate(prompt + json.dumps(
        {'task': task, 'capabilities': pinned, 'navigation_points': navigation}, ensure_ascii=False)))
    actions = value.get('actions') if isinstance(value, dict) else None
    if actions and all(isinstance(a, dict) and a.get('skill') == 'navigate' for a in actions) and navigation_snapshot:
        from brain.skills.body_plan import compile_plan as navigation_plan
        return navigation_plan(value, navigation_snapshot)
    return compile_plan(value, pinned)


def compile_plan(value, pinned):
    if (not isinstance(value, dict) or value.get('supported') is not True
            or value.get('unsupported_requirements') != []
            or set(value) - {'supported', 'unsupported_requirements', 'actions', 'reasoning_explanation'}
            or not isinstance(value.get('actions'), list) or not 1 <= len(value['actions']) <= 2):
        reason = value.get('reasoning_explanation', '') if isinstance(value, dict) else ''
        raise Rejected('新 VLA 任务不受当前完整能力支持：' + str(reason)[:1000])
    if not pinned.get('real_action_enabled'):
        raise Rejected('VLA real_action_enabled=false，不能派发动作')
    points, bindings = pinned['points'], pinned['operations']
    if not bindings:
        raise Rejected('尚未登记 VLA 冰箱作业点与动作起点；请配置 reception_real.native_vla')
    steps, used, opened = [], set(), None
    for number, action in enumerate(value['actions'], 1):
        if not isinstance(action, dict) or set(action) != {'skill'} or not isinstance(action['skill'], str):
            raise Rejected('VLA 动作参数无效，禁止注入身份、坐标或证据')
        operation = action['skill']
        if operation not in bindings or operation in used:
            raise Rejected('动作未配置、不可用或重复：' + operation)
        binding = bindings[operation]
        target = points[binding['route'][-1]]['target_id']
        if operation == 'grasp_cola' and opened != target:
            raise Rejected('抓可乐需要本任务在同一目标先完成开门核验')
        for index, key in enumerate(binding['route']):
            nav_id = f'NATIVE_NAV_{number}_{index + 1}'
            steps.append(StepSpec(nav_id, 'navigate', prefix=f'native-nav-{number}-{index + 1}',
                key=key, navigation=deepcopy(points[key]), evidence='reached', requires_gate=not steps,
                handoff_before='to_nav' if index == 0 and steps else '', deadline_sec=900,
                requires_vla_clearance=True))
        step_id = 'VLA_' + operation.upper()
        effect = 'door_open' if operation == 'open_fridge' else 'object_held'
        steps.append(StepSpec(step_id, operation, prefix='vla-' + operation, object_id=binding['object_id'],
            target_area=target, navigation_step_id=nav_id, evidence=effect, handoff_before='to_vla',
            requires_object_evidence=True, requires_safe_idle=True, deadline_sec=binding['timeout_sec'],
            writes='in_gripper' if operation == 'grasp_cola' else ''))
        steps.append(StepSpec('VERIFY_' + step_id, 'verify', evidence=effect))
        used.add(operation)
        if operation == 'open_fridge':
            opened = target
    return Plan(tuple(steps), str(value.get('reasoning_explanation') or '按登记新 VLA 能力执行'))


def require_operations(steps, capabilities):
    """Reject an incomplete workflow before its first navigation is dispatched."""
    if capabilities is None:
        return
    from contracts.vla import MANIPULATION_OPERATIONS
    for step in steps:
        if step.kind in MANIPULATION_OPERATIONS:
            if (capabilities.get('real_action_enabled') is not True
                    or ((capabilities.get('operations') or {}).get(step.kind) or {}).get('configured') is not True):
                raise Rejected('当前 VLA 不支持或未启用 ' + step.kind + '；不能静默替换原流程')
