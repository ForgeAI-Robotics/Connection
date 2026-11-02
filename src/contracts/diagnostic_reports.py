"""Validation boundary for robot diagnostic summaries, never execution evidence."""
from __future__ import annotations

from datetime import datetime
import json
import re

from shared.diagnostic_io import redact


REPORT_SCHEMA = 'fq/robot-diagnostic-report/v1'
MAX_RESPONSE_BYTES = 65536
MAX_REQUEST_BYTES = 16384
MAX_FINDINGS = 32
MAX_EVIDENCE_REFS = 32
_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_BUSINESS_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
STATUSES = frozenset({'registered', 'recording_disabled', 'starting', 'recording',
                      'finishing', 'complete', 'partial', 'interrupted'})
SOURCES = frozenset({'brain', 'nav', 'vla', 'sonic', 'gateway'})
EVENT_KINDS = frozenset({'task', 'command', 'request', 'evidence', 'failure'})
FINISH_REASONS = frozenset({'completed', 'cancelled', 'failed', 'timeout',
                            'interrupted', 'superseded', 'capture_timeout'})


class DiagnosticReportError(RuntimeError):
    """Contains only locally selected diagnostic messages, not remote raw payloads."""
    def __init__(self, code, message, *, status_code=None):
        super().__init__(message)
        self.code = str(code)
        self.status_code = status_code


def identifier(value, name='identifier'):
    pattern = _BUSINESS_ID if name in {'task_id', 'command_id', 'command_ids'} else _ID
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DiagnosticReportError('INVALID_IDENTITY', f'Invalid diagnostic {name}')
    return value


def timestamp(value, name='timestamp', *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise DiagnosticReportError('INVALID_REPORT', f'Invalid diagnostic {name}')
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('timezone missing')
    except (ValueError, OverflowError):
        raise DiagnosticReportError('INVALID_REPORT', f'Invalid diagnostic {name}') from None
    return parsed


def json_copy(value, *, limit=MAX_RESPONSE_BYTES):
    """Validate bounded finite JSON and detach without changing request identity."""
    try:
        wire = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic value is not finite JSON') from None
    if len(wire) > limit:
        raise DiagnosticReportError('RESPONSE_TOO_LARGE', 'Diagnostic data exceeds its byte limit')
    return json.loads(wire)


def _text(value, name, *, limit=1024, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise DiagnosticReportError('INVALID_REPORT', f'Invalid diagnostic {name}')
    return redact(value)


def _identity(value, run_id, task_id):
    if not isinstance(value, dict):
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic response must be an object')
    if value.get('run_id') != identifier(run_id, 'run_id') or value.get('task_id') != identifier(task_id, 'task_id'):
        raise DiagnosticReportError('IDENTITY_MISMATCH', 'Diagnostic response does not belong to the requested run and task')


def _enum(value, options, name):
    if not isinstance(value, str) or value not in options:
        raise DiagnosticReportError('INVALID_REPORT', f'Unknown diagnostic {name}')
    return value


def _list(value, name, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise DiagnosticReportError('INVALID_REPORT', f'Invalid diagnostic {name}')
    return value


def _strings(value, name, *, count=64, length=256):
    return [_text(item, name, limit=length) for item in _list(value, name, count)]


def _ids(value, name):
    return [identifier(item, name) for item in _list(value, name, 64)]


def _scalar_object(value, name, *, count=32, length=512):
    if not isinstance(value, dict) or len(value) > count:
        raise DiagnosticReportError('INVALID_REPORT', f'Invalid diagnostic {name}')
    for key, item in value.items():
        _text(key, name + ' key', limit=128)
        if item is not None and type(item) not in (str, int, float, bool):
            raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {name} must contain summary scalars')
        if isinstance(item, str):
            _text(item, name, limit=length)
    return redact(value)


def _quality_summary(value, name, *, depth=0):
    """Bounded summary JSON allows count/budget/reason maps, never huge scans."""
    if depth > 4:
        raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {name} exceeds summary nesting')
    if isinstance(value, dict):
        if len(value) > 64:
            raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {name} has too many fields')
        output = {}
        for key, child in value.items():
            _text(key, name + ' key', limit=128)
            # These fields are not report summaries, even if their serialized
            # payload happens to fit the overall response cap.
            if key.lower() in {'raw', 'raw_data', 'raw_sensor_data', 'point_cloud', 'frames', 'images'} or (
                    key.lower() == 'messages' and isinstance(child, (dict, list, str))):
                raise DiagnosticReportError('INVALID_REPORT', 'Raw sensor payload is not diagnostic summary data')
            output[key] = _quality_summary(child, name, depth=depth + 1)
        return redact(output)
    if isinstance(value, list):
        if len(value) > 64:
            raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {name} summary list is too large')
        return [_quality_summary(child, name, depth=depth + 1) for child in value]
    if value is None or type(value) in (bool, int, float):
        return value  # json_copy already rejected non-finite numbers.
    return _text(value, name, limit=1024)


def _topic_summary(value):
    result = _scalar_object(value, 'topic summary')
    counters = {'messages_observed', 'first_record_ns', 'last_record_ns', 'header_samples_decoded',
                'consecutive_duplicate_header_stamps', 'reversed_header_stamps', 'reversed_record_times'}
    numbers = {'hz', 'max_gap_ms', 'p95_gap_ms', 'first_offset_sec', 'last_offset_sec',
               'sample_span_ratio', 'max_header_record_age_ms'}
    signed = {'first_offset_sec', 'last_offset_sec', 'max_header_record_age_ms'}
    for key, item in result.items():
        if key in counters and item is not None and (type(item) is not int or item < 0):
            raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {key} must be a nonnegative count or unknown')
        if key in numbers and item is not None and (type(item) not in (int, float) or (key not in signed and item < 0)):
            raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {key} must be a finite measurement or unknown')
        if key in {'type', 'header_role', 'interval_scope'}:
            _text(item, key, limit=256)
    return result


def validate_request(operation, value, *, run_id=None):
    """Return the exact finite JSON body; never refresh timestamps or generate IDs."""
    body = json_copy(value, limit=MAX_REQUEST_BYTES)
    if not isinstance(body, dict):
        raise DiagnosticReportError('INVALID_REQUEST', 'Diagnostic request must be an object')
    identifier(body.get('task_id'), 'task_id')
    if operation == 'begin':
        required = {'run_id', 'task_id', 'started_at', 'profile', 'command_ids', 'request_ids'}
        allowed = required
        identifier(body.get('run_id'), 'run_id')
        timestamp(body.get('started_at'), 'started_at')
        _enum(body.get('profile'), {'navigation'}, 'profile')
    elif operation == 'event':
        identifier(run_id, 'run_id')
        required = {'event_id', 'task_id', 'at', 'source', 'kind'}
        allowed = required | {'command_id', 'request_id', 'evidence_ref', 'summary', 'source_instance_id'}
        identifier(body.get('event_id'), 'event_id')
        timestamp(body.get('at'), 'at')
        _enum(body.get('source'), SOURCES, 'source')
        _enum(body.get('kind'), EVENT_KINDS, 'kind')
        for key in ('command_id', 'request_id', 'source_instance_id'):
            if key in body:
                identifier(body[key], key)
        if 'evidence_ref' in body:
            body['evidence_ref'] = _text(body['evidence_ref'], 'evidence_ref', limit=512)
        if 'summary' in body:
            body['summary'] = _scalar_object(body['summary'], 'summary', count=16, length=256)
    elif operation == 'finish':
        identifier(run_id, 'run_id')
        required = {'task_id', 'ended_at', 'reason', 'command_ids', 'request_ids'}
        allowed = required
        timestamp(body.get('ended_at'), 'ended_at')
        _enum(body.get('reason'), FINISH_REASONS, 'finish reason')
    else:
        raise DiagnosticReportError('INVALID_REQUEST', 'Unknown diagnostic operation')
    if not required <= body.keys() or set(body) - allowed:
        raise DiagnosticReportError('INVALID_REQUEST', 'Diagnostic request fields do not match the contract')
    for key in ('command_ids', 'request_ids'):
        if key in body:
            _ids(body[key], key)
    return body


def validate_report(value, *, run_id, task_id, event_id=None):
    """Accept a bounded inert summary; never promote it to business/stop evidence."""
    body = json_copy(value)
    _identity(body, run_id, task_id)
    if body.get('schema') != REPORT_SCHEMA or body.get('state_authority') is not False:
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic schema or non-authoritative boundary is invalid')
    status = _enum(body.get('status'), STATUSES, 'status')
    timestamp(body.get('generated_at'), 'generated_at')
    window = body.get('window')
    if not isinstance(window, dict):
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic window is missing')
    windows = {}
    moments = {}
    for key in ('requested_start', 'requested_end', 'recording_start', 'recording_end'):
        if key not in window:
            raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic window field is missing')
        moments[key] = timestamp(window[key], key, nullable=key != 'requested_start')
        windows[key] = window[key]
    for prefix in ('requested', 'recording'):
        start, end = moments[prefix + '_start'], moments[prefix + '_end']
        if start is not None and end is not None and end < start:
            raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic time window is reversed')
    quality = body.get('quality')
    if not isinstance(quality, dict):
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic quality is missing')
    for key in ('recording_enabled', 'recording_ready'):
        if type(quality.get(key)) is not bool:
            raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic quality flags must be boolean')
    q = {key: quality[key] for key in ('recording_enabled', 'recording_ready')}
    q['recorder_state'] = _text(quality.get('recorder_state'), 'recorder_state', limit=64)
    q['loss_visibility'] = _enum(quality.get('loss_visibility'), {'unknown'}, 'loss visibility')
    if 'scope' in quality:
        q['scope'] = _enum(quality['scope'], {'navigation_profile_only'}, 'quality scope')
    if 'whole_robot_complete' in quality:
        if quality['whole_robot_complete'] is not False:
            raise DiagnosticReportError('INVALID_REPORT', 'Navigation diagnostic profile cannot claim whole robot completeness')
        q['whole_robot_complete'] = False
    if 'stage_applicability' in quality:
        q['stage_applicability'] = _quality_summary(quality['stage_applicability'], 'stage applicability')
    if 'applicability' in quality:
        q['applicability'] = _text(quality['applicability'], 'applicability', limit=128)
    for key in ('navigation_physical', 'manipulation_physical'):
        if key in quality:
            if quality[key] is not None and type(quality[key]) is not bool:
                raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {key} must be boolean or unknown')
            q[key] = quality[key]
    for key in ('recorder_released', 'processing_complete', 'required_topics_observed'):
        if key in quality:
            if type(quality[key]) is not bool:
                raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {key} must be boolean')
            q[key] = quality[key]
    if 'finish_reason' in quality:
        q['finish_reason'] = (None if quality['finish_reason'] is None else
                              _enum(quality['finish_reason'], FINISH_REASONS, 'finish reason'))
    if 'indexed_events' in quality:
        if type(quality['indexed_events']) is not int or quality['indexed_events'] < 0:
            raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic indexed event count is invalid')
        q['indexed_events'] = quality['indexed_events']
    if 'external_summaries' in quality:
        summaries = []
        for external in _list(quality['external_summaries'], 'external summaries', 64):
            if not isinstance(external, dict) or external.get('verification') != 'source_declared':
                raise DiagnosticReportError('INVALID_REPORT', 'External diagnostic summary is not independently verified')
            if ('task_id' in external and external['task_id'] != task_id) or ('run_id' in external and external['run_id'] != run_id):
                raise DiagnosticReportError('IDENTITY_MISMATCH', 'External diagnostic summary identity is inconsistent')
            timestamp(external.get('at'), 'external observation time')
            item = {'source': _enum(external.get('source'), SOURCES, 'external source'),
                'event_id': identifier(external.get('event_id'), 'event_id'),
                'at': external['at'], 'verification': 'source_declared',
                'summary': _scalar_object(external.get('summary'), 'external summary', count=16, length=256)}
            for key in ('command_id', 'request_id', 'source_instance_id'):
                if key in external:
                    item[key] = None if external[key] is None else identifier(external[key], key)
            summaries.append(item)
        q['external_summaries'] = summaries
    for key in ('required_topics', 'optional_topics', 'missing_evidence'):
        q[key] = _strings(quality.get(key), key)
    topics = quality.get('topics')
    if not isinstance(topics, dict) or len(topics) > 64:
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic topics must be a bounded object')
    q['topics'] = {_text(topic, 'topic', limit=256): _topic_summary(info)
                   for topic, info in topics.items()}
    for key in ('analysis', 'capture', 'archive'):
        if not isinstance(quality.get(key), dict):
            raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {key} must be a summary object')
        q[key] = _quality_summary(quality[key], key)
    if 'resources' in quality:
        if not isinstance(quality['resources'], dict):
            raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic resources must be a summary object')
        q['resources'] = _quality_summary(quality['resources'], 'resources')
    _enum(q['analysis'].get('state'), {'pending', 'running', 'finished', 'failed'}, 'analysis state')
    if type(q['analysis'].get('truncated')) is not bool:
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic analysis state/truncation is missing')
    for key in ('capture', 'archive'):
        if 'truncated' in q[key] and type(q[key]['truncated']) is not bool:
            raise DiagnosticReportError('INVALID_REPORT', f'Diagnostic {key} truncation must be boolean')
    findings = []
    for finding in _list(body.get('findings'), 'findings', MAX_FINDINGS):
        if not isinstance(finding, dict):
            raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic finding must be an object')
        findings.append({'code': identifier(finding.get('code'), 'finding code'),
            'assessment': _enum(finding.get('assessment'), {'confirmed', 'suspected', 'unknown'}, 'assessment'),
            'message': _text(finding.get('message'), 'finding message', limit=2048),
            'evidence_ids': _ids(finding.get('evidence_ids'), 'evidence_id')})
    refs = []
    for ref in _list(body.get('evidence_refs'), 'evidence_refs', MAX_EVIDENCE_REFS):
        if not isinstance(ref, dict) or type(ref.get('available')) is not bool:
            raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic evidence reference is invalid')
        refs.append({'evidence_id': identifier(ref.get('evidence_id'), 'evidence_id'),
            'kind': _text(ref.get('kind'), 'evidence kind', limit=64),
            'source': _text(ref.get('source'), 'evidence source', limit=128),
            'available': ref['available'], 'reference': _text(ref.get('reference'), 'evidence reference', limit=512)})
    evidence_ids = {ref['evidence_id'] for ref in refs}
    if len(evidence_ids) != len(refs):
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic evidence IDs must be unique')
    if any(set(finding['evidence_ids']) - evidence_ids for finding in findings):
        raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic finding refers to an unlisted evidence ID')
    result = {'schema': REPORT_SCHEMA, 'run_id': run_id, 'task_id': task_id,
        'status': status, 'generated_at': body['generated_at'], 'window': windows,
        'quality': q, 'findings': findings, 'evidence_refs': refs,
        'limitations': _strings(body.get('limitations'), 'limitations', count=32, length=1024),
        'state_authority': False}
    if 'replayed' in body:
        if type(body['replayed']) is not bool:
            raise DiagnosticReportError('INVALID_REPORT', 'Diagnostic replay flag must be boolean')
        result['replayed'] = body['replayed']
    if event_id is not None:
        if body.get('event_id') != identifier(event_id, 'event_id'):
            raise DiagnosticReportError('IDENTITY_MISMATCH', 'Diagnostic event receipt does not match the original event')
        result['event_id'] = event_id
    elif 'event_id' in body:
        result['event_id'] = identifier(body['event_id'], 'event_id')
    return result
