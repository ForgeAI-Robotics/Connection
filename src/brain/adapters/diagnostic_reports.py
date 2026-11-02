"""Gateway diagnostic reports over the existing trusted HTTPS connection."""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from brain.adapters.http_client import HttpClient, HttpContractError
from contracts.diagnostic_reports import (DiagnosticReportError, MAX_RESPONSE_BYTES,
    identifier, timestamp, validate_report, validate_request)


class DiagnosticReportsClient:
    """No retries, latest-report fallback, path dereferences or business mutations."""
    def __init__(self, settings, *, http=None, timeout_sec=2.0):
        if not isinstance(settings, dict):
            raise DiagnosticReportError('CONFIGURATION_ERROR', 'Gateway diagnostic settings are missing')
        try:
            base_url = settings['gateway_base_url']
            parts = urlsplit(base_url)
            ca_file = settings['gateway_ca_file']
            valid = (parts.scheme == 'https' and bool(parts.hostname) and not parts.username
                     and not parts.password and not parts.query and not parts.fragment
                     and (parts.port is None or 0 < parts.port <= 65535)
                     and isinstance(ca_file, str) and bool(ca_file.strip()))
            timeout = float(timeout_sec)
            if not valid or not 0 < timeout <= 2.0:
                raise ValueError('invalid diagnostic configuration')
            token_env = settings.get('gateway_token_env') or 'BRAIN_GATEWAY_TOKEN'
            if not isinstance(token_env, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', token_env):
                raise ValueError('invalid credential source')
            self.http = http if http is not None else HttpClient(base_url,
                ca_file=ca_file, timeout_sec=timeout, error_body_in_message=False)
            self.http.auth_token_env = token_env
            self.http.error_body_in_message = False
            self.timeout_sec = timeout
        except (KeyError, TypeError, ValueError, OSError):
            raise DiagnosticReportError('CONFIGURATION_ERROR',
                'Gateway diagnostics require the existing HTTPS URL, trusted CA and credential source') from None

    @staticmethod
    def _run_path(run_id):
        identifier(run_id, 'run_id')
        if run_id in {'.', '..'}:
            raise DiagnosticReportError('INVALID_IDENTITY', 'Diagnostic run identity cannot be a URL dot segment')
        return '/v1/diagnostics/runs/' + quote(run_id, safe='')

    def _request(self, method, path, *, run_id, task_id, payload=None, event_id=None):
        identifier(run_id, 'run_id')
        identifier(task_id, 'task_id')
        accepted = (200, 202) if method == 'POST' else (200,)
        try:
            body, status, _ = self.http.request_json(method, path, payload,
                accepted_statuses=accepted,
                timeout_sec=self.timeout_sec, max_response_bytes=MAX_RESPONSE_BYTES)
        except HttpContractError as exc:
            status = exc.status_code
            if getattr(exc, 'diagnostic_code', None) == 'RESPONSE_TOO_LARGE':
                code, message = 'RESPONSE_TOO_LARGE', 'Gateway diagnostic response exceeds 64 KiB'
            elif status == 404:
                code, message = 'NOT_FOUND', 'The original diagnostic run is not available in the gateway query scope'
            elif status in (401, 403):
                code, message = 'PERMISSION_DENIED', 'Gateway diagnostic authorization was refused'
            elif (status == 409 and isinstance(exc.payload, dict)
                  and isinstance(exc.payload.get('error'), dict)
                  and exc.payload['error'].get('code') == 'DIAGNOSTIC_BUSY'):
                code, message = 'BUSY', 'Gateway diagnostic recorder is busy; retain the original request identity'
            elif status == 409:
                code, message = 'CONFLICT', 'Gateway diagnostic identity or idempotence conflict; original request retained'
            elif status == 400:
                code, message = 'INVALID_REQUEST', 'Gateway rejected the diagnostic request contract'
            elif status is None or status == 503:
                code, message = 'UNAVAILABLE', 'Gateway diagnostic response is unavailable or unconfirmed'
            else:
                code, message = 'INVALID_RESPONSE', 'Gateway did not return a valid diagnostic response'
            raise DiagnosticReportError(code, message, status_code=status) from None
        except (TimeoutError, ConnectionError, OSError):
            raise DiagnosticReportError('UNAVAILABLE', 'Gateway diagnostic response is unavailable or unconfirmed') from None
        if status not in accepted:
            raise DiagnosticReportError('INVALID_RESPONSE', 'Gateway diagnostic HTTP status is not valid for this operation',
                                        status_code=status)
        return validate_report(body, run_id=run_id, task_id=task_id, event_id=event_id)

    def begin_run(self, payload):
        body = validate_request('begin', payload)
        self._run_path(body['run_id'])
        result = self._request('POST', '/v1/diagnostics/runs', run_id=body['run_id'],
                               task_id=body['task_id'], payload=body)
        if timestamp(result['window']['requested_start']) != timestamp(body['started_at']):
            raise DiagnosticReportError('IDENTITY_MISMATCH', 'Gateway diagnostic start window differs from the original request')
        return result

    def add_event(self, run_id, payload):
        body = validate_request('event', payload, run_id=run_id)
        return self._request('POST', self._run_path(run_id) + '/events',
            run_id=run_id, task_id=body['task_id'], payload=body, event_id=body['event_id'])

    def finish_run(self, run_id, payload):
        body = validate_request('finish', payload, run_id=run_id)
        result = self._request('POST', self._run_path(run_id) + '/finish',
                               run_id=run_id, task_id=body['task_id'], payload=body)
        ended = result['window']['requested_end']
        if ended is not None and timestamp(ended) != timestamp(body['ended_at']):
            raise DiagnosticReportError('IDENTITY_MISMATCH', 'Gateway diagnostic end window differs from the original request')
        return result

    def get_report(self, run_id, *, task_id):
        identifier(run_id, 'run_id')
        identifier(task_id, 'task_id')
        return self._request('GET', self._run_path(run_id) + '/report',
                             run_id=run_id, task_id=task_id)
