"""Business-only direct-v1 requests and executor-reported stop facts."""
from copy import deepcopy
from contracts.tasks import CONTRACT_VERSION
from contracts.vla import NATIVE_OPERATIONS, machine_id, visual_arm_grasp

REVISION = '20261028-direct-v1'
FINISH_REVISION = '20261029-direct-running-finish-v1'
FIELDS = ('contract_version', 'task_id', 'command_id', 'operation', 'object_id', 'target_area')


def o6_feedback_fault(raw, *, task_id, command_id):
    """Project optional first-fault diagnostics; never use them as execution proof."""
    if not isinstance(raw, dict) or not task_id or not command_id:
        return None
    if raw.get('task_id') != task_id or raw.get('command_id') != command_id:
        return None
    def mapping(value):
        return value if isinstance(value, dict) else {}
    result, error = mapping(raw.get('result')), mapping(raw.get('error'))
    candidates = [value for value in (result.get('feedback_fault'),
        mapping(error.get('details')).get('feedback_fault')) if value is not None]
    if not candidates:
        # Older/partial envelopes may expose the same diagnostic only in cleanup.
        candidates = [value for value in (
            mapping(mapping(raw.get('exit_state')).get('runtime')).get('feedback_fault'),
            mapping(mapping(result.get('exit_state')).get('runtime')).get('feedback_fault'),
            mapping(mapping(raw.get('handoff')).get('o6_execution_failure')).get('feedback_fault'))
            if value is not None]
    if not candidates or any(value != candidates[0] for value in candidates[1:]):
        return None
    fault = candidates[0]
    if (not isinstance(fault, dict) or fault.get('schema') != 'fq/o6-feedback-fault/v1'
            or fault.get('task_id') != task_id or fault.get('command_id') != command_id):
        return None
    execution = mapping(mapping(raw.get('handoff')).get('execution'))
    identity = mapping(execution.get('identity'))
    for key in ('receiver_instance_id', 'input_session_id', 'robot_serial'):
        expected = [value[key] for value in (raw, execution, identity) if value.get(key)]
        if any(fault.get(key) != value for value in expected):
            return None
    return deepcopy(fault)


def effect_window(raw, evidence):
    """Use the executor's original observation window, never move the observation time."""
    completion = raw.get('completion') or {}
    if not isinstance(completion, dict):
        raise ValueError('VLA 完成确认结构无效')
    if completion.get('requested_from') != 'running':
        return raw.get('verification_ready_at')
    if (completion.get('evidence') != evidence or not raw.get('execution_ready_at')
            or completion.get('evidence_window_start') != raw.get('execution_ready_at')
            or (raw.get('state') == 'succeeded' and completion.get('status') != 'completed')):
        raise ValueError('运行中确认的原始证据或时间窗口不匹配')
    return raw['execution_ready_at']


def business_request(body):
    result = {key: deepcopy(body.get(key)) for key in FIELDS}
    if (result['contract_version'] != CONTRACT_VERSION or result['operation'] not in NATIVE_OPERATIONS
            or not all(machine_id(result[k]) for k in ('task_id', 'command_id', 'object_id', 'target_area'))):
        raise ValueError('VLA direct-v1 业务请求无效')
    return result


def stop_facts(raw):
    """Consume VLA's results, never query SONIC/O6 or infer physical stationarity."""
    result = raw.get('result') if isinstance(raw.get('result'), dict) else {}
    if raw.get('execution_mode') != 'direct' or result.get('execution_mode') != 'direct':
        return False, False
    exits = [v for v in (raw.get('exit_state'), result.get('exit_state')) if isinstance(v, dict)]
    levels = [result, *exits]
    def confirmed(field):
        values = [value[field] for value in levels if field in value]
        return bool(values) and all(value is True for value in values)
    if raw.get('operation') == 'grasp_cola' and visual_arm_grasp(result.get('evidence')):
        # The authenticated wrist/arm effect is complete; NAV needs the body
        # publishing path released, not another O6 object-retention decision.
        stopped = all(confirmed(k) for k in ('policy_stopped', 'publisher_stopped', 'action_port_released'))
        return stopped, stopped
    not_started = (result.get('action_started') is False
                   and all(value.get('action_started', False) is False for value in exits)
                   and raw.get('state') in {'failed', 'cancelled'})
    # No issued action is different from a measured body stop. Keep raw nulls;
    # only the command execution slot can be cleared in this branch.
    stopped = (confirmed('policy_stopped') and confirmed('publisher_stopped')
               and ((not_started and confirmed('output_fenced')) or
                    all(confirmed(k) for k in ('body_stopped', 'hands_stopped', 'hold_verified'))))
    return stopped, stopped
