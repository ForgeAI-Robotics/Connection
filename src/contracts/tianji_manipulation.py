"""Tianji object effects on the G1 direct-v1 envelope, without G1 hardware rules."""
from copy import deepcopy
from datetime import datetime, timezone
import math
import re

from contracts.tasks import CONTRACT_VERSION
from contracts.vla import machine_id
from contracts.vla_direct import FIELDS as DIRECT_FIELDS

OPERATIONS = frozenset({'grasp_trash', 'dispose_trash'})
FIELDS = frozenset(DIRECT_FIELDS) | {'arm'}
OPTIONAL_FIELDS = frozenset({'prompt'})
IDENTITY_FIELDS = tuple(DIRECT_FIELDS) + ('arm',)


def validate_request(body):
    if not isinstance(body, dict) or not FIELDS <= set(body) <= FIELDS | OPTIONAL_FIELDS:
        raise ValueError('天机操控需要 G1 六字段加 arm，可增加 prompt，不能传路径或控制器参数')
    if body['contract_version'] != CONTRACT_VERSION or body['operation'] not in OPERATIONS:
        raise ValueError('天机操控合同版本或 operation 无效')
    if not all(machine_id(body[k]) for k in ('task_id', 'command_id', 'object_id', 'target_area')):
        raise ValueError('任务、命令、物体及作业区需要 1～128 位机器标识')
    if body['arm'] not in ('left', 'right'):
        raise ValueError('当前单臂合同需要明确 left 或 right；双臂聚合待另行定义')
    if 'prompt' in body:
        prompt = body['prompt']
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4096:
            raise ValueError('prompt 须为1～4096字符的非空文本')
        # Recognize an explicit leading execution-arm instruction. Later
        # mentions such as "keep the right arm still" do not change that arm.
        leading = re.match(r'^\s*(?:please\s+)?(?:use|using)\s+(?:the\s+)?(left|right)\s+(?:arm|hand)\b', prompt, re.I)
        chinese = re.match(r'^\s*(?:请)?(?:使用|用)?(左|右)[臂手]', prompt)
        requested = leading.group(1).lower() if leading else ({'左': 'left', '右': 'right'}[chinese.group(1)] if chinese else None)
        if requested is not None and requested != body['arm']:
            raise ValueError('prompt 首段执行臂与 arm 不一致')
    return body


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else None


def validate_effect(raw, body):
    """An event is not task completion; transport readiness is not an object effect."""
    result = raw.get('result') if isinstance(raw.get('result'), dict) else {}
    proof = result.get('evidence')
    if not isinstance(proof, dict) or proof.get('source') not in {'visual_verifier', 'operator_observation'}:
        return False
    if any(proof.get(k) != body[k] for k in IDENTITY_FIELDS):
        return False
    if 'prompt' in body and any(item.get('prompt') != body['prompt'] for item in (raw, proof)):
        return False
    if (not isinstance(proof.get('reference'), str) or not 1 <= len(proof['reference'].strip()) <= 1024
            or not machine_id(raw.get('run_id')) or proof.get('run_id') != raw['run_id']
            or not isinstance(proof.get('event_id'), str) or not proof['event_id']
            or proof.get('event_id') != raw.get('event_id')
            or proof.get('target_identity_verified') is not True):
        return False
    observed, started, completed = (timestamp(proof.get('observed_at')),
                                   timestamp(raw.get('started_at')), timestamp(raw.get('completed_at')))
    if not all((observed, started, completed)) or not started <= observed <= completed <= datetime.now(timezone.utc):
        return False
    if result.get('success') is not True:
        return False
    if body['operation'] == 'grasp_trash':
        return (result.get('object_grasped') is True and result.get('holding') == body['object_id']
                and result.get('holding_at_event') is True)
    return (result.get('arms_clear') is True and result.get('released') is True and result.get('holding') is None
            and 'holding' in result and result.get('bin_verified') is True
            and result.get('object_at_target') is True)


def observe_legacy_event(raw, *, task_id, run_id, arm, receipt_ttl_sec=5, now=None):
    """Read the downloaded cleanup draft as an event, never as a completed skill."""
    if (not machine_id(task_id) or not machine_id(run_id) or arm not in ('left', 'right')
            or type(receipt_ttl_sec) not in (float, int)
            or not math.isfinite(receipt_ttl_sec) or receipt_ttl_sec <= 0):
        raise ValueError('需要合法 task_id/run_id/arm 及有限正数接收 TTL')
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    if type(current) not in (int, float) or not math.isfinite(current):
        raise ValueError('接收时间必须为有限数')
    matched = (isinstance(raw, dict) and raw.get('schema_version') == 'cleanup.v0.1-draft'
               and raw.get('task_id') == task_id and raw.get('run_id') == run_id
               and raw.get('arm') == arm)
    event_at = raw.get('event_timestamp') if matched else None
    fresh = (type(event_at) in (int, float) and math.isfinite(event_at)
             and 0 <= current - event_at <= receipt_ttl_sec)
    received = (matched and fresh and raw.get('state') == 'GRASP_CONFIRMED_HOLDING_AT_EVENT'
                and raw.get('event_id') == run_id + ':grasp_confirmed'
                and all(raw.get(k) is True for k in
                        ('grasp_success', 'policy_stopped_at_event', 'holding_at_event')))
    return dict(scope='legacy_completion_event_only', event_received=bool(received),
        task_completed=False, navigation_allowed=False, deduplication_required=True,
        event_id=raw.get('event_id') if received else None,
        reason='persist_event_id_in_original_attempt_before_consumption' if received
               else 'event_unconfirmed_or_expired', raw=deepcopy(raw))
