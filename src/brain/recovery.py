"""Bounded recovery proposals. Pure validation; Runtime alone applies changes."""
from copy import deepcopy
from datetime import datetime, timezone

from contracts.tasks import Rejected

OBSERVATION_TTL_SEC = 60
SUPPORTED_BACKENDS = {'desk', 'simple_o7', 'body_real', 'body_simple', 'reception_real', 'reception_protocol',
                      'reception_simple', 'reception_real_sim_manipulation', 'reception_ros_sim_manipulation', 'reception_sim_navigation_real'}


def bind_policy(policy, package, backend, port, config):
    settings = (config.get('brain') or {}).get('reasoning') or {}
    mode = getattr(policy, 'recovery_mode', lambda _: 'disabled')(package)
    if backend in {'body_real', 'body_simple'}:
        mode = 'preserve_order'
    if settings.get('recovery_plans', True) is False or backend not in SUPPORTED_BACKENDS:
        mode = 'disabled'
    limit = settings.get('max_recovery_revisions', 2)
    if type(limit) is not int or not 0 <= limit <= 5:
        raise Rejected('max_recovery_revisions 必须是 0～5 的整数')
    return {'mode': mode, 'observe': mode != 'disabled' and callable(getattr(port, 'observe_recovery', None)),
            # SIMPLE generic is one episode per task; its scene API is a template,
            # not a live state or permission to reset a failed physical episode.
            'revise': mode != 'disabled' and backend != 'simple_o7',
            'max_revisions': limit}


def remaining(record):
    return deepcopy((record.get('phase_specs') or [])[int(record.get('cursor') or 0):])


def latest_attempt(record):
    specs = remaining(record)
    if not specs:
        return {}
    return ((record.get('steps', {}).get(specs[0]['step_id']) or {}).get('attempts') or [{}])[-1]


def recovery_progress(record):
    attempt = latest_attempt(record)
    query = record.get('advice_query') or {}
    if (query.get('command_id') and query.get('command_id') == attempt.get('command_id')
            and query.get('attempt_id') == attempt.get('attempt_id')):
        return query
    return attempt.get('progress') or {}


def refuses_retry(progress, backend=None):
    evidence = progress.get('evidence') or {}
    if not isinstance(evidence, dict):
        return True
    raw = evidence.get('downstream') or progress.get('raw') or {}
    if not isinstance(raw, dict):
        return True
    result, error = raw.get('result') or {}, raw.get('error') or {}
    details = error.get('details') or {} if isinstance(error, dict) else {}
    if not isinstance(result, dict) or not isinstance(details, dict):
        return True
    simulation = result.get('simulation') or {}
    if not isinstance(simulation, dict):
        return True
    # SIMPLE keeps retryable=false on terminal commands (no automatic replay),
    # but explicitly permits a new same-episode navigation attempt when stopped
    # and recoverable. This exception is limited to the pinned simulation route.
    simulation_retry = (backend in {'body_simple', 'reception_simple', 'reception_ros_sim_manipulation'}
                        and simulation.get('retry_allowed') is True)
    return (result.get('failure_scope') == 'safety' or details.get('failure_scope') == 'safety'
            or result.get('next_command_allowed') is False or details.get('next_command_allowed') is False
            or simulation.get('retry_allowed') is False
            or (isinstance(error, dict) and error.get('retryable') is False and not simulation_retry)
            or evidence.get('submission_rejected') is not None)


def fresh_observation(record, *, now=None):
    observation = record.get('recovery_observation') or {}
    attempt = latest_attempt(record)
    if (observation.get('task_id') != record.get('task_id')
            or observation.get('command_id') != attempt.get('command_id')
            or observation.get('plan_revision') != record.get('plan_revision', 0)):
        return False
    try:
        stamp = datetime.fromisoformat(observation['observed_at'])
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        return stamp.tzinfo is not None and 0 <= age <= OBSERVATION_TTL_SEC
    except (KeyError, ValueError, TypeError):
        return False


def can_revise(record):
    policy = record.get('recovery_policy') or {}
    attempt = latest_attempt(record)
    progress = recovery_progress(record)
    evidence = progress.get('evidence') or {}
    return (policy.get('mode') in {'preserve_order', 'reorder_remaining'} and policy.get('revise') is True
            and record.get('state') == 'recovery_required'
            and record.get('control_request') in (None, '')
            and not record.get('command_unknown')
            and record.get('stopped_confirmed') is True and record.get('resources_cleared') is True
            and attempt.get('command_id') == record.get('open_command_id')
            and progress.get('terminal') == 'failed'
            and evidence.get('identity_ok') is True and evidence.get('time_ok') is True
            and not refuses_retry(progress, record.get('execution_backend')) and fresh_observation(record)
            and len(record.get('plan_revisions') or []) < policy.get('max_revisions', 0))


def available_actions(record):
    policy = record.get('recovery_policy') or {}
    actions = []
    if (policy.get('observe') and remaining(record)
            and record.get('state') in {'recovery_required', 'waiting_human', 'paused'}):
        actions.append('observe_scene')
    if can_revise(record):
        actions.append('revise_plan')
    return actions


def planning_context(record):
    return {'policy': deepcopy(record.get('recovery_policy') or {}),
            'plan_revision': record.get('plan_revision', 0),
            'remaining_steps': [{key: s.get(key) for key in ('step_id', 'kind', 'key', 'object_id', 'target_area')}
                                for s in remaining(record)],
            'observation': deepcopy(record.get('recovery_observation')),
            'observation_fresh': fresh_observation(record)}


def validate_proposal_shape(context, plan):
    if not isinstance(plan, dict) or set(plan) != {'step_order', 'observation_id'}:
        raise Rejected('恢复方案结构无效')
    order = plan['step_order']
    ids = [s['step_id'] for s in context['remaining_steps']]
    if (not isinstance(order, list) or any(not isinstance(i, str) for i in order)
            or len(order) != len(ids) or len(set(order)) != len(order) or set(order) != set(ids)):
        raise Rejected('恢复方案必须完整引用尚未完成的步骤')
    if plan['observation_id'] != (context.get('observation') or {}).get('observation_id'):
        raise Rejected('恢复方案未引用当前现场证据')
    if context['policy'].get('mode') == 'preserve_order' and order != ids:
        raise Rejected('恢复方案不允许改写 SOP 顺序')
    return deepcopy(plan)


def validate_plan(record, plan):
    """Retain every obligation and exact step payload; only independent work can move."""
    if not can_revise(record):
        raise Rejected('原动作、现场证据或恢复次数不满足计划调整条件')
    if not isinstance(plan, dict) or set(plan) != {'step_order', 'observation_id'}:
        raise Rejected('恢复方案只接受 step_order 和 observation_id')
    if plan['observation_id'] != record['recovery_observation']['observation_id']:
        raise Rejected('恢复方案引用的现场证据已变化')
    specs = remaining(record)
    ids = [s['step_id'] for s in specs]
    order = plan['step_order']
    if (not isinstance(order, list) or any(not isinstance(i, str) for i in order)
            or len(order) != len(ids) or len(set(order)) != len(order) or set(order) != set(ids)):
        raise Rejected('恢复方案必须保留全部未完成步骤，不能增加、删除或重复动作')
    if record['recovery_policy']['mode'] == 'preserve_order' and order != ids:
        raise Rejected('SOP 恢复不得改变路线、抓放或核验顺序')
    if order != ids:
        # Only independent generic pick/place groups may move. Keep object-local
        # dependencies, final checks, and exclusive holding semantics intact.
        from brain.adapters.execution import parse_sim_action
        actions = {}
        for s in specs:
            if s.get('kind') != 'sim':
                raise Rejected('此计划含不可重排的动作或核验步骤')
            action = parse_sim_action(s.get('key'))
            if not action or action[0] not in {'grasp', 'place'}:
                raise Rejected('当前仅允许调整独立物体抓放步骤的顺序')
            actions[s['step_id']] = action
        for obj in {a[1] for a in actions.values()}:
            old = [i for i in ids if actions[i][1] == obj]
            if [i for i in order if actions[i][1] == obj] != old:
                raise Rejected('同一物体的抓放依赖不能改变')
            # Placing onto another moved object introduces a cross-object dependency.
            if any(a[0] == 'place' and a[2] in {v[1] for v in actions.values()} for a in actions.values()):
                raise Rejected('物体间存在放置依赖，不能重排')
        holding = record.get('holding')
        # Generic steps have no SOP writes field. Recover established holding
        # from the completed prefix, rather than assuming the initial None.
        for s in (record.get('phase_specs') or [])[:int(record.get('cursor') or 0)]:
            if (record.get('steps', {}).get(s['step_id']) or {}).get('status') != 'done':
                continue
            a = parse_sim_action(s.get('key'))
            if a and a[0] == 'grasp':
                holding = a[1]
            elif a and a[0] == 'place':
                holding = None
        for i in order:
            action = actions[i]
            if action[0] == 'grasp':
                if holding:
                    raise Rejected('恢复方案会在持物时重复抓取')
                holding = action[1]
            else:
                if holding != action[1]:
                    raise Rejected('恢复方案缺少对应抓取前提')
                holding = None
    buckets = record.get('steps') or {}
    for i in ids:
        bucket = buckets.get(i) or {}
        if bucket.get('status') in {'done', 'skipped'} or any(a.get('verdict') == 'PASS' for a in bucket.get('attempts', [])):
            raise Rejected('已完成或已跳过步骤不能纳入恢复重排')
    by_id = {s['step_id']: s for s in specs}
    return [by_id[i] for i in order]
