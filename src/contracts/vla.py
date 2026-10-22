"""Explicit native VLA operations and externally observed effect evidence."""
from datetime import datetime, timezone
import re

NATIVE_OPERATIONS = frozenset({'open_fridge', 'grasp_cola'})
MANIPULATION_OPERATIONS = NATIVE_OPERATIONS | {'pick', 'place'}


def machine_id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def validate_request(body):
    from contracts.tasks import CONTRACT_VERSION
    keys = {'contract_version', 'task_id', 'command_id', 'operation', 'object_id', 'target_area', 'navigation_proof'}
    if (not isinstance(body, dict) or set(body) != keys or body['contract_version'] != CONTRACT_VERSION
            or body['operation'] not in NATIVE_OPERATIONS):
        raise ValueError('新 VLA 动作合同无效')
    if not all(machine_id(body[k]) for k in ('task_id', 'command_id', 'object_id', 'target_area')):
        raise ValueError('新 VLA 动作需要 1–128 位机器标识')
    proof = body['navigation_proof']
    if (not isinstance(proof, dict) or set(proof) != {'dream_command_id', 'target_id', 'state'}
            or not machine_id(proof['dream_command_id']) or proof['target_id'] != body['target_area']
            or proof['state'] != 'succeeded'):
        raise ValueError('新 VLA 动作的导航证明无效')


def validate_evidence(operation, object_id, evidence, ready_at):
    if not isinstance(evidence, dict) or evidence.get('source') not in {'operator_observation', 'visual_verifier'}:
        raise ValueError('需要现场观察或视觉核验的真实效果证据')
    reference = evidence.get('reference')
    if not isinstance(reference, str) or not 1 <= len(reference.strip()) <= 1024:
        raise ValueError('需要可追溯的证据 reference')
    try:
        observed = datetime.fromisoformat(str(evidence.get('observed_at', '')).replace('Z', '+00:00'))
        ready = datetime.fromisoformat(str(ready_at).replace('Z', '+00:00'))
        valid = observed.tzinfo is not None and ready.tzinfo is not None and ready <= observed <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('证据时间须带时区，且处于暂停确认之后、当前时间之前')
    if operation == 'open_fridge':
        valid = (all(evidence.get(k) is True for k in ('door_open', 'door_stable_open', 'hands_clear_of_door'))
                 and evidence.get('door_closed') is not True)
    elif operation == 'grasp_cola':
        valid = (evidence.get('object_grasped') is True and evidence.get('holding') == object_id
                 and evidence.get('clear_of_fridge') is True)
    else:
        valid = False
    if not valid:
        raise ValueError('效果证据不满足本动作的完成条件')
    return evidence
