"""Explicit native VLA operations and externally observed effect evidence."""
from datetime import datetime, timedelta, timezone
import re

NATIVE_OPERATIONS = frozenset({'open_fridge', 'grasp_cola'})
MANIPULATION_OPERATIONS = NATIVE_OPERATIONS | {'pick', 'place'}
HOLDING_SCHEMA = 'g1/o6-holding-evidence/v1'
VISUAL_ARM_GRASP_CONTRACT = 'fq/vla-grasp-cola-wrist-return-evidence/v2'


def visual_arm_grasp(evidence):
    """Identify the wrist/arm contract; validate_evidence verifies its conditions."""
    return (isinstance(evidence, dict) and evidence.get('source') == 'visual_verifier'
            and evidence.get('visual_contract') == VISUAL_ARM_GRASP_CONTRACT)


def manual_holding_assumed(request):
    """Runtime's task-bound human disposition, never a VLA/sensor receipt."""
    assumption = request.get('operator_assumption')
    return (isinstance(assumption, dict) and assumption.get('source') == 'human_skip'
            and bool(assumption.get('task_id'))
            and assumption.get('task_id') == request.get('body', {}).get('task_id')
            and bool(assumption.get('step_id')) and bool(assumption.get('at'))
            and bool(request.get('required_holding'))
            and assumption.get('holding') == request['required_holding'])


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
        raise ValueError('证据时间须带时区，且处于本次执行／暂停确认的有效观察窗口内')
    if operation == 'open_fridge':
        if evidence.get('visual_contract') == 'fq/vla-open-fridge-head-arm-evidence/v2':
            arm = evidence.get('arm_return') or {}
            valid = (evidence.get('source') == 'visual_verifier' and evidence.get('door_open') is True
                     and evidence.get('door_closed') is False and evidence.get('arms_returned') is True
                     and isinstance(arm, dict) and arm.get('schema') == 'fq/vla-arm-return/v1'
                     and arm.get('valid') is True and isinstance(arm.get('event_id'), str)
                     and bool(arm['event_id']) and arm.get('return_dwell_s') == .8)
        else:  # Historical/manual completion evidence retains its original meaning.
            valid = (all(evidence.get(k) is True for k in ('door_open', 'door_stable_open', 'hands_clear_of_door'))
                     and evidence.get('door_closed') is not True)
    elif operation == 'grasp_cola':
        if evidence.get('visual_contract') == VISUAL_ARM_GRASP_CONTRACT:
            arm=evidence.get('arm_return') or {}
            valid=(evidence.get('source') == 'visual_verifier' and evidence.get('object_grasped') is True
                   and evidence.get('left_grasped') is True and evidence.get('right_grasped') is True
                   and evidence.get('holding') == object_id and evidence.get('arms_returned') is True
                   and isinstance(arm,dict) and arm.get('schema') == 'fq/vla-arm-return/v1'
                   and arm.get('valid') is True and isinstance(arm.get('event_id'),str)
                   and bool(arm['event_id']) and arm.get('return_dwell_s') == .8)
        elif evidence.get('visual_contract') == 'fq/vla-grasp-cola-wrist-arm-evidence/v1':
            arm = evidence.get('arm_hold') or {}
            valid = (evidence.get('source') == 'visual_verifier' and evidence.get('object_grasped') is True
                     and evidence.get('holding') == object_id and evidence.get('arms_held') is True
                     and isinstance(arm,dict) and arm.get('schema') == 'fq/vla-arm-hold/v1'
                     and arm.get('valid') is True and isinstance(arm.get('event_id'),str)
                     and bool(arm['event_id']) and arm.get('hold_dwell_s') == .8)
        else:
            valid = (evidence.get('object_grasped') is True and evidence.get('holding') == object_id
                     and evidence.get('clear_of_fridge') is True)
    else:
        valid = False
    if not valid:
        raise ValueError('效果证据不满足本动作的完成条件')
    return evidence


def holding_expiry(evidence, *, now=None):
    """Validate submission/current validity, never invalidate historical effects."""
    now = now or datetime.now(timezone.utc)
    try:
        observed = datetime.fromisoformat(evidence['observed_at'].replace('Z', '+00:00'))
        until = evidence.get('valid_until')
        expires = (datetime.fromisoformat(until.replace('Z', '+00:00')) if until is not None
                   else observed + timedelta(seconds=60))
        valid = (observed.tzinfo is not None and expires.tzinfo is not None
                 and observed <= now < expires and 0 < (expires - observed).total_seconds() <= 300)
    except (KeyError, AttributeError, ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        raise ValueError('持物证据已过期、时间无效或超过 300 秒上限；须真实重新确认')
    return expires


def validate_current_holding(status, object_id, *, robot_serial, task_id, source_association):
    """Consume O6-verified evidence; brain never signs or renews object proofs."""
    from contracts.body_control import digest
    proof = status.get('holding_evidence')
    if not isinstance(proof, dict):
        raise ValueError('缺少当前 holding_evidence；历史 holding 不能代替持物证明')
    if (proof.get('schema') != HOLDING_SCHEMA or proof.get('valid') is not True
            or proof.get('signature_verified') is not True or proof.get('invalid_reason')):
        raise ValueError('当前持物证明无效：' + str(proof.get('invalid_reason') or 'schema/valid/signature_verified'))
    if (status.get('current_holding') != object_id or proof.get('holding') != object_id
            or proof.get('robot_serial') != robot_serial or proof.get('task_id') != task_id
            or proof.get('command_id') != source_association.get('command_id')):
        raise ValueError('持物证明的机器人、原任务／抓取命令或物体不匹配')
    holding_expiry(proof)
    anchor = proof.get('anchor') or {}
    facts = status.get('execution_facts') or {}
    if not isinstance(anchor, dict) or not isinstance(facts, dict):
        raise ValueError('持物证明或当前执行事实格式无效')
    hands = facts.get('o6') or {}
    if not isinstance(hands, dict):
        raise ValueError('当前 O6 回执格式无效')
    if (not isinstance(hands.get('capabilities'), list)
            or 'authenticated-holding-evidence-v1' not in hands['capabilities']
            or hands.get('robot_serial') != robot_serial
            or not anchor.get('receiver_instance_id') or not anchor.get('controller_instance_id')
            or anchor.get('receiver_instance_id') != hands.get('receiver_instance_id')
            or anchor.get('controller_instance_id') != facts.get('controller_instance_id')
            or anchor.get('output_instance_id') != hands.get('output_instance_id')
            or not anchor.get('output_instance_id')
            or digest(anchor.get('association')) != digest(source_association)
            or digest(hands.get('association')) != digest(source_association)
            or hands.get('holding') != object_id or hands.get('holding_evidence') != proof
            or hands.get('hold_verified') is not True or hands.get('hands_stopped') is not True
            or hands.get('receiver_output_fenced') is not True):
        raise ValueError('持物证明与当前控制器、O6 实例、原授权或保持回执不匹配')
    for key, current_key in (('target_generation', 'command_writes'), ('feedback_generation', 'feedback_generation')):
        if (type(anchor.get(key)) is not int or anchor[key] < 0
                or type(hands.get(current_key)) is not int or anchor[key] != hands[current_key]):
            raise ValueError('持物证明的目标或反馈代次已经变化')
    targets = hands.get('targets')
    if (not isinstance(targets, dict) or set(targets) != {'left', 'right'}
            or any(not isinstance(v, list) or len(v) != 6
                   or any(type(x) is not int or not 0 <= x <= 255 for x in v) for v in targets.values())
            or anchor.get('targets_sha256') != digest(targets)):
        raise ValueError('持物证明的双手目标不匹配')
    evidence = proof.get('evidence') or {}
    if (not isinstance(evidence, dict)
            or evidence.get('source') not in {'operator_observation', 'visual_verifier'}
            or not isinstance(evidence.get('reference'), str) or not evidence['reference'].strip()
            or evidence.get('observed_at') != proof.get('observed_at')
            or evidence.get('holding') != object_id or evidence.get('object_grasped') is not True
            or holding_expiry(evidence) != holding_expiry(proof)):
        raise ValueError('持物证明与原认证观察不一致')
    payload = {key: proof.get(key) for key in ('schema', 'robot_serial', 'task_id', 'command_id',
                                             'holding', 'anchor', 'evidence', 'observed_at', 'valid_until')}
    if proof.get('proof_id') != digest(payload):
        raise ValueError('持物 proof_id 与原证明内容不一致')
    return proof
