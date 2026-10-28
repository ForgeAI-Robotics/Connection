"""VLA gateway evidence validation used by the existing body authority.

No worker, ledger, permit issuer or NAV client lives here.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math

from contracts.body_control import ControlError, VERSION, REVISION, PERMIT_MODE, GATEWAY_BINDING, fresh

EXTENSION = 'vla-gateway-v1-candidate1'
WORKSITE_SCHEMA = 'fq/vla-worksite-evidence/v1'


def require(ok, code):
    if not ok:
        raise ControlError(code, code)


def worksite(proof, body, robot, controller, session):
    """Bind an already observed business prerequisite; never refresh its lifetime."""
    require(isinstance(proof, dict), 'WORKSITE_EVIDENCE_REQUIRED')
    proof = deepcopy(proof)
    require(proof.get('schema') == WORKSITE_SCHEMA
            and proof.get('kind') in {'operator_ready', 'navigation_arrival'}, 'WORKSITE_EVIDENCE_INVALID')
    for key in ('task_id', 'command_id', 'operation', 'object_id', 'target_area'):
        require(proof.get(key) == body.get(key) and body.get(key) is not None, 'WORKSITE_IDENTITY_MISMATCH')
    require(proof.get('robot_serial') == robot and proof.get('controller_instance_id') == controller,
            'WORKSITE_CONTROLLER_MISMATCH')
    require(proof.get('control_session_id') in (None, session), 'WORKSITE_SESSION_MISMATCH')
    proof['control_session_id'] = session
    require(proof.get('ready') is True and proof.get('pose_verified') is True, 'WORKSITE_NOT_READY')
    fields = ['reference', 'pose_reference']
    if proof['kind'] == 'navigation_arrival':
        fields += ['navigation_command_id', 'navigation_result_reference']
    require(all(isinstance(proof.get(k), str) and 0 < len(proof[k].strip()) <= 1024 for k in fields),
            'WORKSITE_REFERENCE_REQUIRED')
    try:
        observed = datetime.fromisoformat(proof['observed_at'].replace('Z', '+00:00'))
        until = datetime.fromisoformat(proof['valid_until'].replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        valid = (observed.tzinfo is not None and until.tzinfo is not None
                 and observed <= now < until and 0 < (until-observed).total_seconds() <= 300)
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    require(valid, 'WORKSITE_EVIDENCE_EXPIRED_OR_INVALID')
    return proof


def capabilities(status):
    require(EXTENSION in status.get('features', []) and 'vla' in status.get('supported_sources', [])
            and {'hold', 'execute'} <= set(status.get('supported_hand_modes', [])),
            'GATEWAY_VLA_EXTENSION_REQUIRED')


def hands(status, binding, phase=None, *, exiting=False, never_bound=False):
    h = status.get('o6') or {}
    require(h.get('receiver_instance_id') == binding.get('o6_instance_id')
            and bool(binding.get('o6_instance_id')), 'O6_BINDING_MISMATCH')
    if never_bound:
        require(exiting and 'vla-prepare-installs-o6-v1' in status.get('features', []), 'O6_EXIT_UNCONFIRMED')
        permission = h.get('permission')
        if permission is not None:
            require(isinstance(permission, dict), 'O6_EXIT_UNCONFIRMED')
            old = (permission.get('control_authorization') or {}).get('ownership_epoch')
            require(type(old) is int and old < binding['control_authorization']['ownership_epoch']
                    and permission.get('control_session_id') != binding['control_session_id'], 'O6_EXIT_UNCONFIRMED')
    else:
        require(h.get('control_session_id') == binding['control_session_id']
                and h.get('input_session_id') == binding['input_session_id'], 'O6_BINDING_MISMATCH')
    require(h.get('hold_verified') is True, 'O6_HOLD_UNCONFIRMED')
    elapsed = status.get('_request_elapsed_ms', 0) / 1000
    for side in ('left', 'right'):
        measured = (h.get('measured') or {}).get(side) or {}
        age = measured.get('age_sec')
        require(measured.get('fresh') is True and type(age) in (int, float)
                and math.isfinite(age) and 0 <= age and age + elapsed <= .25, 'O6_FEEDBACK_STALE')
    if exiting:
        permission = h.get('permission') or {}
        require(h.get('receiver_output_fenced') is True
                and (never_bound or all(permission.get(k) == binding[k] for k in
                        ('control_authorization', 'controller_instance_id', 'gateway_instance_id'))),
                'O6_EXIT_UNCONFIRMED')
    else:
        permission = h.get('permission') or {}
        require(h.get('hold_reference_id') == binding['hold_reference_id']
                and h.get('preparation_received') is True, 'O6_PREPARATION_UNCONFIRMED')
        require(h.get('hand_mode') == ('execute' if phase == 'execute' else 'hold')
                and h.get('receiver_output_fenced') is (phase == 'prepare')
                and permission.get('phase') == phase
                and all(permission.get(k) == binding[k] for k in
                        ('control_authorization', 'controller_instance_id', 'gateway_instance_id')),
                'O6_PERMISSION_UNCONFIRMED')
    return h


def receipt(value, payload):
    require(isinstance(value, dict), 'INVALID_RECEIPT')
    view = deepcopy(value)
    require(all(view.get(k) == expected for k, expected in
                (('contract_version', VERSION), ('coordination_revision', REVISION),
                 ('permit_mode', PERMIT_MODE), ('extension', EXTENSION),
                 ('request_id', payload['request_id']), ('request', payload))), 'VLA_RECEIPT_MISMATCH')
    registration = view.get('registration') or {}
    business = payload.get('business_request')
    control = business['control'] if business else payload
    require(all(registration.get(k) == control.get(k) for k in ('control_session_id', 'control_authorization')),
            'VLA_SESSION_MISMATCH')
    require(registration.get('source_id') == 'vla' and all(
        registration.get(k) for k in (*GATEWAY_BINDING, 'o6_instance_id', 'hold_reference_id')),
        'VLA_REGISTRATION_INVALID')
    identity = dict(registration, task_id=view.get('task_id'), command_id=view.get('command_id'))
    require(view.get('identity') == identity, 'VLA_RECEIPT_MISMATCH')
    if business:
        require(all(view.get(k) == business[k] for k in ('task_id', 'command_id')), 'VLA_RECEIPT_MISMATCH')
    if view.get('state') == 'completed':
        require(view.get('accepted') is True and view.get('historical') is False
                and bool(view.get('vla_instance_id'))
                and view['vla_instance_id'] == view.get('query_vla_instance_id'), 'STALE_PARTICIPANT')
        require(fresh(view.get('checked_at')), 'STALE_EVIDENCE')
        if business and view.get('current_prepare_valid') is not True:
            raise ControlError('STATIC_ACTIVATION_UNCONFIRMED', '操控准备未通过当前状态检查，动作尚未下发',
                details={'current_prepare_blockers': deepcopy(view.get('current_prepare_blockers')),
                         'original_receipt': deepcopy(value)})
    return view
