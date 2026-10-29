"""Business-only direct-v1 requests and executor-reported stop facts."""
from copy import deepcopy
from contracts.tasks import CONTRACT_VERSION
from contracts.vla import NATIVE_OPERATIONS, machine_id

REVISION = '20261028-direct-v1'
FIELDS = ('contract_version', 'task_id', 'command_id', 'operation', 'object_id', 'target_area')


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
    not_started = (result.get('action_started') is False
                   and all(value.get('action_started', False) is False for value in exits)
                   and raw.get('state') in {'failed', 'cancelled'})
    # No issued action is different from a measured body stop. Keep raw nulls;
    # only the command execution slot can be cleared in this branch.
    stopped = (confirmed('policy_stopped') and confirmed('publisher_stopped')
               and ((not_started and confirmed('output_fenced')) or
                    all(confirmed(k) for k in ('body_stopped', 'hands_stopped', 'hold_verified'))))
    return stopped, stopped
