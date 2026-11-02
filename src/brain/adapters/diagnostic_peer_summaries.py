"""Read a small final index from an original command; never control an executor."""
from copy import deepcopy
import json
from urllib.parse import quote, urlsplit

from brain.adapters.http_client import HttpClient, HttpContractError
from contracts.diagnostic_reports import DiagnosticReportError, identifier
from shared.diagnostic_context import diagnostic_context
from shared.diagnostic_io import redact


class DiagnosticPeerSummaryReader:
    def __init__(self, config, *, http_factory=HttpClient):
        self.config = config
        self.http_factory = http_factory

    def __call__(self, peer, task_id, command_id, expected_target):
        identifier(task_id, 'task_id'); identifier(command_id, 'command_id')
        if peer not in {'nav', 'vla'}:
            raise DiagnosticReportError('PEER_INVALID', 'Unknown diagnostic peer')
        real = self.config.get('reception_real') or {}
        key = 'dream_base_url' if peer == 'nav' else 'vla_base_url'
        target = real.get(key)
        if (not isinstance(expected_target, str) or not isinstance(target, str)
                or target.rstrip('/') != expected_target.rstrip('/')):
            raise DiagnosticReportError('PEER_TARGET_CHANGED', 'Original peer target is unavailable or changed')
        parts = urlsplit(target)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise DiagnosticReportError('PEER_TARGET_INVALID', 'Original peer target is invalid')
        settings = real.get('body_control') or {}
        token_env = settings.get('nav_token_env' if peer == 'nav' else 'vla_token_env') or settings.get('token_env') or 'BRAIN_CONTROL_TOKEN'
        client = self.http_factory(target, timeout_sec=2.,
            contract_version=real.get('contract_version') or 'fq/reception-lan/v1', error_body_in_message=False)
        client.auth_token_env = token_env
        path = ('/v1/commands/' if peer == 'nav' else '/v1/vla/tasks/') + quote(command_id, safe='')
        try:
            with diagnostic_context(replace=True, task_id=task_id, command_id=command_id):
                raw, _, _ = client.request_json('GET', path, timeout_sec=2., max_response_bytes=512 * 1024)
        except HttpContractError as exc:
            raise DiagnosticReportError('PEER_SUMMARY_UNAVAILABLE',
                'Original peer command summary is unavailable', status_code=exc.status_code) from None
        except (TimeoutError, ConnectionError, OSError):
            raise DiagnosticReportError('PEER_SUMMARY_UNAVAILABLE', 'Original peer command summary is unavailable') from None
        small = raw.get('diagnostic_summary') if isinstance(raw, dict) else None
        if (not isinstance(small, dict) or raw.get('task_id') != task_id or raw.get('command_id') != command_id
                or small.get('schema') != 'fq/peer-diagnostic-summary/v1' or small.get('state_authority') is not False
                or small.get('task_id') != task_id or small.get('command_id') != command_id or small.get('peer') != peer):
            raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Original peer summary identity or schema is invalid')
        # The full GET is transient only; no runtime receipt, raw arrays, images,
        # internal controls or credential-bearing execution context enter cache.
        allowed = {'schema', 'task_id', 'command_id', 'peer', 'state_authority', 'status', 'readiness',
                   'collection_state', 'archive_state', 'generated_at', 'quality', 'findings', 'evidence_refs',
                   'service_instance_id', 'code_revision', 'window'}
        result = {name: deepcopy(value) for name, value in small.items() if name in allowed}
        try:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic index is not finite JSON') from None
        if len(encoded) > 16384:
            raise DiagnosticReportError('PEER_SUMMARY_TOO_LARGE', 'Peer diagnostic index exceeds 16 KiB')
        for name, maximum in (('findings', 32), ('evidence_refs', 64)):
            values = result.get(name, [])
            if not isinstance(values, list) or len(values) > maximum:
                raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic index list is invalid')
        quality = result.get('quality')
        if not isinstance(quality, dict):
            raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic quality is invalid')
        result['quality'] = {key: quality[key] for key in
            ('status', 'missing', 'missing_evidence', 'truncation', 'partial', 'readiness') if key in quality}
        for key in ('missing', 'missing_evidence'):
            items = result['quality'].get(key)
            if items is not None and (not isinstance(items, list) or len(items) > 64
                                     or any(not isinstance(item, str) or len(item) > 256 for item in items)):
                raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic missing-evidence list is invalid')
        for name, fields in (
                ('findings', {'code', 'classification', 'assessment', 'confidence', 'message', 'scope', 'meaning', 'signals'}),
                ('evidence_refs', {'ref_id', 'root', 'relative_path', 'sha256', 'hash_scope', 'bytes',
                                   'reference', 'ref', 'path', 'id', 'available', 'scope', 'time_window'})):
            projected = []
            for item in result.get(name, []):
                if isinstance(item, str) and name == 'evidence_refs':
                    if len(item) > 512:
                        raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer evidence reference is too large')
                    projected.append(item); continue
                if not isinstance(item, dict):
                    raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic index entry is invalid')
                entry = {key: item[key] for key in fields if key in item}
                if name == 'findings':
                    classifications = [entry[key] for key in ('assessment', 'classification', 'confidence') if key in entry]
                    if (not classifications or any(not isinstance(value, str) or value not in {'confirmed', 'suspected', 'unknown'} for value in classifications)
                            or len(set(classifications)) != 1):
                        raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic finding classification is invalid or conflicting')
                for key, value in entry.items():
                    if key == 'signals':
                        if (not isinstance(value, list) or len(value) > 64
                                or any(not isinstance(signal, str) or len(signal) > 256 for signal in value)):
                            raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic signals are invalid')
                    elif key == 'time_window':
                        if not isinstance(value, dict):
                            raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer evidence window is invalid')
                        entry[key] = {field: value[field] for field in ('first_command_event', 'last_command_event', 'snapshot_at')
                                      if field in value and (value[field] is None or isinstance(value[field], str))}
                    elif value is not None and type(value) not in {str, int, float, bool}:
                        raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic index entry must be scalar')
                projected.append(entry)
            result[name] = projected
        if 'window' in result:
            window = result['window']
            if not isinstance(window, dict):
                raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic window is invalid')
            result['window'] = {key: window[key] for key in ('started_at', 'ended_at',
                'first_host_monotonic_s', 'first_host_wall_s', 'last_host_monotonic_s', 'last_host_wall_s')
                if key in window and (window[key] is None or type(window[key]) in {str, int, float, bool})}
        for key, value in result.items():
            if key not in {'window', 'quality', 'findings', 'evidence_refs'} and value is not None:
                if type(value) not in {str, int, float, bool} or (isinstance(value, str) and len(value) > 256):
                    raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Peer diagnostic metadata is not a bounded scalar')
        result = redact(result)
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 16384:
            raise DiagnosticReportError('PEER_SUMMARY_TOO_LARGE', 'Redacted peer diagnostic index exceeds 16 KiB')
        return result
