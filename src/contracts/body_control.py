"""Brain-owned handoff wire identity. No robot operations or task state."""
import hashlib
import json
import re
from datetime import datetime, timezone

VERSION = 'fq/vla-handoff/v1'
REVISION = '20261023-brain-owned-v1'
IDENTITY = ('contract_version', 'coordination_revision', 'robot_serial', 'action_endpoint',
            'task_id', 'command_id', 'lease_id')


class ControlError(RuntimeError):
    def __init__(self, code, message, status=409, *, details=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.details = details

    def response(self):
        error = {'code': self.code, 'message': str(self)}
        if self.details is not None:
            error['details'] = self.details
        return {'contract_version': VERSION, 'coordination_revision': REVISION,
                'authority': 'brain', 'accepted': False,
                'error': error}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def lease_identity(robot, task, command):
    return hashlib.sha256('\0'.join((robot, task, command)).encode('utf-8')).hexdigest()


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def fresh(value, maximum_age=10):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.tzinfo is not None and -2 <= (datetime.now(timezone.utc) - parsed).total_seconds() <= maximum_age
    except (ValueError, TypeError):
        return False


def nav_prepare_refused(wire, post):
    """Only NAV's known pre-admission refusals prove no new preparation.

    The original authenticated HTTP reply is tied to this exact first POST.
    REQUEST_CONFLICT, generic errors and query-only 404s prove no such fact.
    """
    if not post or wire['service'] != 'nav' or wire['payload'].get('action') != 'prepare':
        return False
    if post.get('method') != 'POST' or post.get('path') != wire['path']:
        return False
    value = post.get('payload')
    if (not isinstance(value, dict) or value.get('accepted') is not False
            or value.get('contract_version') != VERSION or value.get('coordination_revision') != REVISION):
        return False
    expected = {'INVALID_REQUEST':400, 'INVALID_AUTHORIZATION':400, 'UNSUPPORTED_CONTRACT_VERSION':400,
                'HANDOFF_BUSY':409, 'NAV_NOT_YIELDED':409, 'STALE_HANDOFF':409}
    error = value.get('error')
    code = error.get('code') if isinstance(error, dict) else None
    if code not in expected or post.get('status_code') != expected[code]:
        return False
    # Optional echoed identity must not contradict the actual request.
    if any(key in value and encode(value[key]) != encode(expected_value)
           for key, expected_value in wire['payload'].items()):
        return False
    if 'request' in value and encode(value['request']) != encode(wire['payload']):
        return False
    if any(value.get(key) is True for key in ('action_started', 'effects_started', 'execution_started')):
        return False
    return value.get('state') not in {'pending','completed','succeeded','unknown'}
