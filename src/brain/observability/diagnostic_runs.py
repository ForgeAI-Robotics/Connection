"""Non-authoritative, bounded transport/cache for remote diagnostic recordings.

The Runtime emits saved snapshots. This coordinator sends small metadata only;
it never changes task ledgers, robot controls, recording files or business gates.
"""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
import threading
import time
import uuid

from brain.workers import WorkerPool
from contracts.diagnostic_reports import DiagnosticReportError, timestamp
from shared.diagnostic_context import diagnostic_context, diagnostic_event
from shared.diagnostic_io import redact


PHYSICAL_BACKENDS = frozenset({'body_real', 'reception_real',
    'reception_real_sim_manipulation', 'reception_sim_navigation_real'})
TERMINAL_STATES = frozenset({'succeeded', 'failed', 'cancelled'})
# NAV permits 60 s queue + 30 s archive build + coalescing. These bounded
# diagnostic offsets leave a final read after that producer budget, without
# changing command deadlines or extending a window on repeated snapshots.
PEER_SETTLEMENT_SEC = 120.
PEER_READ_OFFSETS = (0., 10., 40., 105.)
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_REQUEST_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


def run_id_for_task(task_id):
    if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        raise ValueError('diagnostic task_id must be an original valid task identity')
    return 'brain-' + uuid.uuid5(uuid.NAMESPACE_URL, 'connection-diagnostics:' + task_id).hex


def _now():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def _bounded(value, default, low, high, integer=False):
    try:
        number = int(value) if integer else float(value)
        return number if low <= number <= high else default
    except (TypeError, ValueError, OverflowError):
        return default


def _ids(record, key, limit=64):
    """Only authoritative attempt identities; peer payloads are not an ID index."""
    if key != 'command_id':
        return []
    result = set()
    if isinstance(record.get('open_command_id'), str):
        result.add(record['open_command_id'])
    count = 0
    for bucket in (record.get('steps') or {}).values():
        for attempt in (bucket or {}).get('attempts') or []:
            count += 1
            if count > 4096 or len(result) >= limit:
                break
            value = attempt.get('command_id')
            if (attempt.get('task_id', record.get('task_id')) == record.get('task_id')
                    and isinstance(value, str) and _ID.fullmatch(value)):
                result.add(value)
    return sorted(value for value in result if _ID.fullmatch(value))[:limit]


@dataclass
class _Run:
    task_id: str
    run_id: str
    query_only: bool = False
    restored: bool = False
    backend: str | None = None
    last_task_state: str | None = None
    opened_at: str | None = None
    begin: dict | None = None
    begin_done: bool = False
    begin_rejected: bool = False
    begin_send_attempted: bool = False
    begin_abandoned: bool = False
    finish: dict | None = None
    finish_done: bool = False
    events: OrderedDict = field(default_factory=OrderedDict)
    seen_events: set = field(default_factory=set)
    command_ids: set = field(default_factory=set)
    request_ids: set = field(default_factory=set)
    indexed_requests: set = field(default_factory=set)
    peer_signatures: dict = field(default_factory=dict)
    peer_targets: dict = field(default_factory=dict)
    pending_peers: OrderedDict = field(default_factory=OrderedDict)
    peer_windows: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)
    inflight: object = None
    inflight_action: str | None = None
    inflight_event_id: str | None = None
    next_at: float = 0.
    capture_deadline: float | None = None
    start_wait_deadline: float | None = None
    refresh_requested: bool = False
    report_polls: int = 0
    report: dict | None = None
    error: dict | None = None
    tracking: str = 'query_only'
    incomplete_reasons: list = field(default_factory=list)
    dropped_events: int = 0
    updated_at: str | None = None


class DiagnosticRuns:
    def __init__(self, config, *, client=None, pool=None, clock=time.monotonic,
                 now=_now, control_records=None, peer_summary_reader=None):
        settings = (config.get('brain') or {}).get('diagnostics') or {}
        settings = settings if isinstance(settings, dict) else {}
        self.enabled = settings.get('enabled') is True
        self.clock, self.now = clock, now
        self.retry_sec = _bounded(settings.get('retry_interval_sec'), 1., .05, 60.)
        self.poll_sec = _bounded(settings.get('report_poll_interval_sec'), 2., .05, 60.)
        self.max_attempts = _bounded(settings.get('max_attempts'), 3, 1, 10, True)
        self.max_polls = _bounded(settings.get('max_report_polls'), 30, 1, 120, True)
        self.max_runs = _bounded(settings.get('max_cached_runs'), 16, 2, 64, True)
        self.max_events = _bounded(settings.get('event_queue_limit'), 64, 1, 256, True)
        self.capture_sec = _bounded(settings.get('max_capture_duration_sec'), 900., 10., 3600.)
        self.start_wait_sec = _bounded(settings.get('start_wait_timeout_sec'), 60., 1., 300.)
        self.pool = pool or WorkerPool('brain-diagnostic-runs', 1, capacity=8)
        self.client = client
        self.control_records = control_records
        self.peer_summary_reader = peer_summary_reader
        self._lock = threading.RLock()
        self._runs = OrderedDict()
        self._current_task = None
        self._closing = False
        self._ticking = False
        self._dropped_runs = 0
        self._queue_rejections = 0
        self._configuration_error = None
        self._last_rejected_task = None
        if self.enabled and self.client is None:
            try:
                from brain.adapters.diagnostic_reports import DiagnosticReportsClient
                self.client = DiagnosticReportsClient(
                    (config.get('reception_real') or {}).get('body_control') or {},
                    timeout_sec=_bounded(settings.get('request_timeout_sec'), 2., .1, 2.))
            except Exception as exc:
                self._configuration_error = {'code': 'DIAGNOSTIC_CONFIG_INVALID',
                                             'message': str(redact(str(exc)))}

    def _get(self, task_id, *, query_only=True):
        run_id = run_id_for_task(task_id)
        run = self._runs.get(task_id)
        if run is not None:
            return run
        if len(self._runs) >= self.max_runs:
            victim = next((key for key, value in self._runs.items()
                           if key != self._current_task and value.inflight is None
                           and (value.finish_done or value.query_only or value.begin is None
                                or value.tracking == 'failed')), None)
            if victim is None:
                self._dropped_runs += 1
                self._last_rejected_task = task_id
                return None
            self._runs.pop(victim)
        run = _Run(task_id, run_id, query_only=query_only)
        self._runs[task_id] = run
        return run

    def _incomplete(self, run, reason):
        if reason not in run.incomplete_reasons and len(run.incomplete_reasons) < 16:
            run.incomplete_reasons.append(reason)

    def _begin_payload(self, run, record):
        # Wire field mapping is centralized so no retry ever regenerates times/IDs.
        return {'run_id': run.run_id, 'task_id': run.task_id,
                'started_at': run.opened_at or record.get('updated_at') or self.now(),
                'profile': 'navigation', 'command_ids': sorted(run.command_ids)[:64],
                'request_ids': sorted(run.request_ids)[:64]}

    def _finish_payload(self, run, reason, ended_at):
        return {'task_id': run.task_id, 'ended_at': ended_at, 'reason': reason,
                'command_ids': sorted(run.command_ids)[:64], 'request_ids': sorted(run.request_ids)[:64]}

    def _finish(self, run, reason, ended_at):
        if run.query_only or run.begin is None or run.finish is not None:
            return
        run.finish = self._finish_payload(run, reason, ended_at)
        run.refresh_requested = True
        run.report_polls = 0
        if reason in {'superseded', 'capture_timeout'}:
            self._incomplete(run, reason)
        if not run.begin_send_attempted:
            self._abandon_begin(run, 'capture_ended_before_begin_delivery')
        if reason == 'superseded':
            run.tracking = 'superseded'
            # Close the original recorder before attempting a new one. Superseded
            # unsent metadata is explicitly lost, not silently attached to a new run.
            run.dropped_events += len(run.events)
            run.events.clear()
            if run.dropped_events:
                self._incomplete(run, 'superseded_unsent_events')

    def _abandon_begin(self, run, reason):
        """Only a local pre-send fact permits abandoning a queued begin."""
        if run.begin_send_attempted or run.begin_done:
            return
        run.begin_abandoned = True
        run.finish_done = run.finish is not None
        run.refresh_requested = False
        run.dropped_events += len(run.events)
        run.events.clear()
        self._incomplete(run, reason)
        if run.tracking != 'superseded':
            run.tracking = 'not_recorded'
        future = run.inflight if run.inflight_action == 'begin' else None
        if future is not None:
            # A worker may already be running but not yet have entered its send
            # boundary. _perform repeats the same check before invoking client.
            future.cancel()

    def event(self, event, record):
        """Consume saved Runtime facts without waiting for network or a recorder."""
        if not self.enabled or self._closing or not isinstance(record, dict):
            return
        task_id = record.get('task_id')
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            return
        try:
            with self._lock:
                if event == 'task_restored':
                    if record.get('execution_backend') not in PHYSICAL_BACKENDS:
                        return
                    run = self._get(task_id, query_only=True)
                    self._current_task = task_id
                    if run is not None:
                        run.restored = True
                        run.last_task_state = record.get('state')
                        run.refresh_requested = True
                    # A cold restart queries an existing deterministic run only.
                elif event == 'task_opened':
                    known = self._runs.get(task_id)
                    if known is not None and self._current_task not in {None, task_id}:
                        self._incomplete(known, 'late_task_opened_ignored')
                        return
                    old = self._runs.get(self._current_task)
                    if old is not None and old.task_id != task_id:
                        self._finish(old, 'superseded', record.get('updated_at') or self.now())
                    self._current_task = task_id
                    run = self._get(task_id, query_only=False)
                    if run is not None:
                        if not run.restored:
                            run.query_only = False
                            run.refresh_requested = False
                            run.error = None
                            run.next_at = self.clock()
                            run.report_polls = 0
                        run.opened_at = record.get('updated_at') or self.now()
                        run.tracking = 'not_started'
                        run.last_task_state = record.get('state')
                else:
                    run = self._runs.get(task_id)
                    if event == 'execution_selected' and record.get('execution_backend') in PHYSICAL_BACKENDS:
                        if self._current_task is not None and self._current_task != task_id:
                            if run is not None:
                                self._incomplete(run, 'late_execution_selection_ignored')
                            return
                        if run is None:
                            # Selection is an explicit new-task event, unlike a status read.
                            run = self._get(task_id, query_only=False)
                            self._current_task = task_id
                        if run is not None and not run.query_only and run.begin is None:
                            run.backend = record.get('execution_backend')
                            target = record.get('execution_target') or {}
                            run.peer_targets = {key: target[key] for key in ('dream', 'vla')
                                                if isinstance(target.get(key), str)}
                            run.begin = self._begin_payload(run, record)
                            run.capture_deadline = self.clock() + self.capture_sec
                            run.start_wait_deadline = self.clock() + self.start_wait_sec
                            run.tracking = 'queued'
                    if run is None or run.query_only or run.begin is None:
                        return
                    run.last_task_state = record.get('state')
                    self._remember_ids(run, _ids(record, 'command_id'), _ids(record, 'request_id'))
                    revision = record.get('revision')
                    at = record.get('updated_at') or self.now()
                    key = hashlib.sha256(f'{task_id}:{revision}:{event}:{at}'.encode()).hexdigest()
                    if key not in run.seen_events and run.finish is None:
                        body = {'event_id': key, 'task_id': task_id, 'at': at, 'source': 'brain',
                            'kind': 'failure' if record.get('state') in {'failed', 'recovery_required'} else 'task',
                            'summary': self._summary_fields({'event': event, 'task_state': record.get('state'),
                                'step_id': record.get('phase'), 'execution_backend': record.get('execution_backend'),
                                'blocked_reason': record.get('blocked_reason'), 'revision': revision,
                                **self._physical_scope(record)})}
                        command = record.get('open_command_id')
                        if isinstance(command, str) and _ID.fullmatch(command):
                            body['command_id'] = command
                        self._enqueue_event(run, body)
                        self._peer_evidence_events(run, record, key, at)
                    if record.get('state') in TERMINAL_STATES:
                        reason = {'succeeded': 'completed', 'cancelled': 'cancelled', 'failed': 'failed'}[record['state']]
                        self._finish(run, reason, record.get('updated_at') or self.now())
                        for peer_key in tuple(run.pending_peers):
                            self._arm_peer(run, peer_key)
            self.tick()
        except Exception as exc:
            diagnostic_event('DIAGNOSTIC_RUN', 'event_dropped', task_id=task_id,
                             error=str(redact(str(exc))), level='WARNING')

    @staticmethod
    def _summary_fields(values):
        return {key: str(redact(value))[:256] if isinstance(value, str) else value
                for key, value in values.items() if value is not None and isinstance(value, (str, bool, int, float))}

    @staticmethod
    def _physical_scope(record):
        backend = record.get('execution_backend')
        scopes = {'reception_real': (True, True),
                  'reception_real_sim_manipulation': (True, False),
                  'reception_sim_navigation_real': (False, True)}
        if backend in scopes:
            nav, vla = scopes[backend]
            return {'navigation_physical': nav, 'manipulation_physical': vla,
                    'scope': 'configured_route_not_motion_or_success_evidence'}
        specs = record.get('phase_specs') or []
        if backend == 'body_real' and specs:
            return {'navigation_physical': any(s.get('kind') == 'navigate' for s in specs),
                    'manipulation_physical': any(s.get('kind') in {'open_fridge', 'grasp_cola', 'pick', 'place'} for s in specs),
                    'scope': 'planned_physical_skills_not_motion_or_success_evidence'}
        return {'backend_physical': backend in PHYSICAL_BACKENDS,
                'scope': 'step_applicability_pending'}

    def _remember_ids(self, run, commands, requests):
        for destination, incoming in ((run.command_ids, commands), (run.request_ids, requests)):
            for value in incoming:
                if value not in destination and len(destination) >= 64:
                    self._incomplete(run, 'identity_limit')
                    break
                destination.add(value)

    def _enqueue_event(self, run, body):
        key = body['event_id']
        if key in run.seen_events:
            return
        if len(run.events) >= self.max_events:
            dropped = next((candidate for candidate in run.events if candidate != run.inflight_event_id), None)
            run.dropped_events += 1
            self._incomplete(run, 'event_queue_overflow')
            if dropped is None:
                return
            run.events.pop(dropped, None)
            run.attempts.pop('event:' + dropped, None)
        run.events[key] = body
        run.seen_events.add(key)
        if len(run.seen_events) > self.max_events * 4:
            run.seen_events = set(run.events)

    def _peer_evidence_events(self, run, record, parent_key, at):
        emitted = 0
        for bucket in (record.get('steps') or {}).values():
            for attempt in (bucket or {}).get('attempts') or []:
                if emitted >= 16:
                    self._incomplete(run, 'peer_summary_event_limit'); return
                command = attempt.get('command_id')
                raw = ((attempt.get('progress') or {}).get('evidence') or {}).get('downstream') or {}
                small = raw.get('diagnostic_summary') if isinstance(raw, dict) else None
                if (not isinstance(small, dict) or small.get('schema') != 'fq/peer-diagnostic-summary/v1'
                        or not isinstance(command, str) or not _ID.fullmatch(command)):
                    continue
                if (small.get('state_authority') is not False
                        or small.get('task_id') != run.task_id or small.get('command_id') != command
                        or raw.get('task_id') != run.task_id or raw.get('command_id') != command):
                    self._incomplete(run, 'peer_summary_identity_mismatch')
                    continue
                skill = (attempt.get('request') or {}).get('skill')
                source = ('nav' if skill == 'navigate' else 'vla' if skill in {'open_fridge', 'grasp_cola', 'pick', 'place'}
                          else 'vla' if raw.get('operation') else 'nav')
                explicit_source = small.get('peer')
                if explicit_source != source:
                    self._incomplete(run, 'peer_summary_source_mismatch')
                    continue
                progress = attempt.get('progress') or {}
                terminal = any(isinstance(value, str) and value in TERMINAL_STATES for value in
                               (progress.get('terminal'), attempt.get('outcome'), raw.get('state'), record.get('state')))
                if not self._track_peer(run, source, command, small, terminal=terminal):
                    continue
                quality = small.get('quality') if isinstance(small.get('quality'), dict) else {}
                missing = quality.get('missing_evidence', quality.get('missing'))
                fields = self._summary_fields({'peer_schema': small.get('schema'), 'peer_status': small.get('status'),
                    'peer_archive_state': small.get('archive_state'),
                    'quality_status': quality.get('status'), 'missing_count': len(missing) if isinstance(missing, list) else None,
                    'peer_generated_at': small.get('generated_at'), 'peer_readiness': small.get('readiness'),
                    'peer_partial': small.get('partial', quality.get('partial')),
                    'peer_quality_truncated': quality.get('truncation'),
                    'failure_code': (raw.get('error') or {}).get('code') if isinstance(raw.get('error'), dict) else None})
                signature = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
                if run.peer_signatures.get(command) != signature and (command in run.peer_signatures or len(run.peer_signatures) < 128):
                    run.peer_signatures[command] = signature
                    key = hashlib.sha256(f'{parent_key}:{command}:{signature}'.encode()).hexdigest()
                    self._enqueue_event(run, {'event_id': key, 'task_id': run.task_id, 'at': at,
                        'source': source, 'kind': 'evidence', 'command_id': command, 'summary': fields})
                    emitted += 1
                findings = small.get('findings')
                if isinstance(findings, list) and len(findings) > 4:
                    self._incomplete(run, 'peer_finding_projection_limit')
                for finding in (findings if isinstance(findings, list) else [])[:4]:
                    if emitted >= 16:
                        self._incomplete(run, 'peer_summary_event_limit'); return
                    if not isinstance(finding, dict):
                        continue
                    code = finding.get('code')
                    classifications = [finding[name] for name in ('assessment', 'classification', 'confidence') if name in finding]
                    if not classifications or any(not isinstance(value, str) or value not in {'confirmed', 'suspected', 'unknown'}
                                                  for value in classifications):
                        continue
                    if len(set(classifications)) != 1:
                        self._incomplete(run, 'peer_finding_classification_conflict')
                        continue
                    assessment = classifications[0]
                    if not isinstance(code, str) or assessment not in {'confirmed', 'suspected', 'unknown'}:
                        continue
                    finding_fields = self._summary_fields({'finding_code': code, 'assessment': assessment,
                        'message': finding.get('message', finding.get('scope', finding.get('meaning'))),
                        'scope': 'peer_finding_not_business_verdict',
                        'signal_count': len(finding['signals']) if isinstance(finding.get('signals'), list) else None})
                    finding_sig = hashlib.sha256(json.dumps([command, finding_fields], sort_keys=True).encode()).hexdigest()
                    if finding_sig in run.peer_signatures:
                        continue
                    if len(run.peer_signatures) >= 128:
                        self._incomplete(run, 'peer_summary_dedup_limit'); continue
                    run.peer_signatures[finding_sig] = True
                    self._enqueue_event(run, {'event_id': hashlib.sha256(f'{parent_key}:{finding_sig}'.encode()).hexdigest(),
                        'task_id': run.task_id, 'at': at, 'source': source, 'kind': 'evidence',
                        'command_id': command, 'summary': finding_fields})
                    emitted += 1
                refs = small.get('evidence_refs')
                if isinstance(refs, list) and len(refs) > 8:
                    self._incomplete(run, 'peer_reference_projection_limit')
                for ref in (refs if isinstance(refs, list) else [])[:8]:
                    if emitted >= 16:
                        self._incomplete(run, 'peer_summary_event_limit'); return
                    value, metadata = self._peer_reference(ref, peer=source)
                    if not isinstance(value, str) or not value or len(value) > 512:
                        self._incomplete(run, 'peer_evidence_reference_unavailable')
                        continue
                    safe_ref = str(redact(value))
                    ref_sig = hashlib.sha256(json.dumps([command, safe_ref, metadata], sort_keys=True).encode()).hexdigest()
                    if ref_sig in run.peer_signatures:
                        continue
                    if len(run.peer_signatures) >= 128:
                        self._incomplete(run, 'peer_summary_dedup_limit'); continue
                    run.peer_signatures[ref_sig] = True
                    ref_key = hashlib.sha256(f'{parent_key}:{ref_sig}'.encode()).hexdigest()
                    self._enqueue_event(run, {'event_id': ref_key, 'task_id': run.task_id, 'at': at,
                        'source': source, 'kind': 'evidence', 'command_id': command, 'evidence_ref': safe_ref,
                        'summary': {'scope': 'source_declared_not_fetched', **metadata}})
                    emitted += 1

    @staticmethod
    def _peer_reference(ref, *, peer=None):
        if isinstance(ref, str):
            return ref, {}
        if not isinstance(ref, dict):
            return None, {}
        root, relative = ref.get('root'), ref.get('relative_path')
        if root is not None or relative is not None:
            if (not isinstance(relative, str) or not relative or relative.startswith(('/', '\\'))
                    or '..' in relative.replace('\\', '/').split('/')):
                return None, {}
            if root is None and peer == 'nav':
                reference = ref.get('ref_id')
                if not isinstance(reference, str) or not reference.startswith('nav:'):
                    return None, {}
            elif isinstance(root, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', root):
                reference = root + ':' + relative
            else:
                return None, {}
        else:
            reference = ref.get('reference') or ref.get('ref') or ref.get('path') or ref.get('id')
        metadata = {key: ref[key] for key in ('ref_id', 'sha256', 'hash_scope', 'bytes', 'relative_path') if key in ref}
        if isinstance(relative, str) and len(relative) > 256:
            metadata['relative_path_truncated'] = True
        if 'scope' in ref:
            metadata['peer_ref_scope'] = ref['scope']
        window = ref.get('time_window')
        if isinstance(window, dict):
            metadata.update({key: window[key] for key in ('first_command_event', 'last_command_event', 'snapshot_at') if key in window})
        return reference, DiagnosticRuns._summary_fields(metadata)

    @staticmethod
    def _peer_pending(small):
        return (small.get('status') in {'pending', 'collecting', 'finalizing', 'starting', 'running'}
                or small.get('collection_state') in {'pending', 'open', 'collecting', 'finalizing'}
                or small.get('archive_state') in {'pending', 'queued', 'building', 'running', 'finalizing'})

    def _track_peer(self, run, peer, command_id, small, *, terminal=False):
        key = peer + ':' + command_id
        if not self._peer_pending(small):
            self._close_peer(run, key, 'ready')
            return True
        window = run.peer_windows.get(key)
        if window is not None and window.get('closed'):
            # A repeated old Runtime snapshot cannot reopen an exhausted window
            # or overwrite a sealed final index with its stale pending metadata.
            return False
        if key in run.pending_peers:
            if terminal:
                self._arm_peer(run, key)
            return True
        if len(run.pending_peers) >= 4:
            self._incomplete(run, 'peer_final_index_tracking_limit')
            return True
        target = run.peer_targets.get('dream' if peer == 'nav' else 'vla')
        if not callable(self.peer_summary_reader) or target is None:
            self._incomplete(run, 'peer_final_index_reader_unavailable')
            return True
        run.pending_peers[key] = {'peer': peer, 'command_id': command_id,
                                  'target': target, 'next_at': self.clock()}
        if terminal:
            self._arm_peer(run, key)
        return True

    def _sync_peer_pending_reason(self, run):
        armed = any(pending.get('deadline') is not None for pending in run.pending_peers.values())
        if armed:
            self._incomplete(run, 'peer_final_index_pending')
        elif 'peer_final_index_pending' in run.incomplete_reasons:
            run.incomplete_reasons.remove('peer_final_index_pending')

    def _arm_peer(self, run, key):
        pending = run.pending_peers.get(key)
        if pending is None:
            return
        window = run.peer_windows.get(key)
        if window is None:
            if len(run.peer_windows) >= 64:
                run.pending_peers.pop(key, None)
                self._incomplete(run, 'peer_final_index_window_limit')
                return
            window = {'started_at': self.clock(), 'deadline': self.clock() + PEER_SETTLEMENT_SEC, 'closed': False}
            run.peer_windows[key] = window
        if window['closed']:
            run.pending_peers.pop(key, None)
            self._sync_peer_pending_reason(run)
            return
        if self.clock() >= window['deadline']:
            self._close_peer(run, key, 'timeout')
            return
        pending['deadline'] = window['deadline']
        count = run.attempts.get('peer:' + key, 0)
        pending['next_at'] = window['started_at'] + PEER_READ_OFFSETS[min(count, len(PEER_READ_OFFSETS) - 1)]
        self._sync_peer_pending_reason(run)

    def _close_peer(self, run, key, reason):
        run.pending_peers.pop(key, None)
        window = run.peer_windows.get(key)
        if window is None and reason == 'ready' and len(run.peer_windows) < 64:
            window = {'deadline': self.clock(), 'closed': True}
            run.peer_windows[key] = window
        if window is not None:
            window.update(closed=True, reason=reason)
        if reason == 'timeout':
            self._incomplete(run, 'peer_final_index_settlement_timeout:' + key)
        elif reason == 'unavailable':
            self._incomplete(run, 'peer_final_index_unavailable:' + key)
        self._sync_peer_pending_reason(run)

    def _peer_completed(self, run, key, value):
        pending = run.pending_peers.get(key)
        if pending is None:
            return
        if pending.get('deadline') is None or self.clock() >= pending['deadline']:
            self._close_peer(run, key, 'timeout')
            return
        peer, command_id = pending['peer'], pending['command_id']
        if (not isinstance(value, dict) or value.get('schema') != 'fq/peer-diagnostic-summary/v1'
                or value.get('state_authority') is not False or value.get('task_id') != run.task_id
                or value.get('command_id') != command_id or value.get('peer') != peer):
            raise DiagnosticReportError('PEER_SUMMARY_INVALID', 'Late peer summary identity is invalid')
        at = self.now()
        raw = {'task_id': run.task_id, 'command_id': command_id, 'diagnostic_summary': value}
        if peer == 'vla':
            raw['operation'] = 'diagnostic_read_only'
        snapshot = {'steps': {'original': {'attempts': [{'command_id': command_id,
            'request': {'skill': 'navigate' if peer == 'nav' else 'open_fridge'},
            'progress': {'evidence': {'downstream': raw}}}]}}}
        self._peer_evidence_events(run, snapshot,
            hashlib.sha256(f'{run.run_id}:{key}:{at}'.encode()).hexdigest(), at)
        if key in run.pending_peers:
            if run.attempts.get('peer:' + key, 0) >= len(PEER_READ_OFFSETS):
                self._close_peer(run, key, 'unavailable')
            else:
                count = run.attempts.get('peer:' + key, 0)
                run.pending_peers[key]['next_at'] = run.peer_windows[key]['started_at'] + PEER_READ_OFFSETS[count]
        self._sync_peer_pending_reason(run)

    def _saved_requests(self, task_id):
        if not callable(self.control_records):
            return []
        try:
            state = self.control_records() or {}
            gate = state.get('source_gate', state)
            requests = []
            operations = gate.get('operations') or {}
            for number, key in enumerate(reversed(operations)):
                if number >= 4096:
                    with self._lock:
                        run = self._runs.get(task_id)
                        if run is not None:
                            self._incomplete(run, 'control_identity_scan_limit')
                    break
                value = operations[key]
                if (value.get('diagnostic_identity') or {}).get('task_id') == task_id:
                    rid = (value.get('request') or {}).get('request_id')
                    if isinstance(rid, str) and _REQUEST_ID.fullmatch(rid):
                        requests.append(rid)
                        if len(requests) >= 64:
                            break
            return requests
        except Exception:
            with self._lock:
                run = self._runs.get(task_id)
                if run is not None:
                    self._incomplete(run, 'control_identity_index_unavailable')
            return []

    def _perform(self, run, action, payload):
        with diagnostic_context(replace=True, task_id=run.task_id):
            if action == 'begin':
                with self._lock:
                    if self._closing or run.finish is not None or run.begin_abandoned:
                        self._abandon_begin(run, 'begin_abandoned_before_send')
                        return None
                    # This is the send-attempt linearization point, not queue
                    # admission. After it, a lost reply cannot prove nonexecution.
                    run.begin_send_attempted = True
                return self.client.begin_run(payload)
            if action == 'event':
                # Local control-store read happens off the Runtime thread. Never
                # mutate an already-enqueued event body to add newly found IDs.
                requests = self._saved_requests(run.task_id)
                with self._lock:
                    if self._runs.get(run.task_id) is run:
                        self._remember_ids(run, [], requests)
                        for rid in requests:
                            if rid in run.indexed_requests:
                                continue
                            run.indexed_requests.add(rid)
                            key = hashlib.sha256(f'{run.task_id}:request:{rid}'.encode()).hexdigest()
                            self._enqueue_event(run, {'event_id': key, 'task_id': run.task_id,
                                'at': payload['at'], 'source': 'brain', 'kind': 'request', 'request_id': rid,
                                'summary': {'scope': 'saved_source_gate_operation'}})
            if action == 'event':
                return self.client.add_event(run.run_id, payload)
            if action == 'finish':
                return self.client.finish_run(run.run_id, payload)
            if action == 'peer':
                with self._lock:
                    key = payload['peer'] + ':' + payload['command_id']
                    pending = run.pending_peers.get(key)
                    if pending is None:
                        return None
                    if pending.get('deadline') is None or self.clock() >= pending['deadline']:
                        self._close_peer(run, key, 'timeout')
                        return None
                return self.peer_summary_reader(payload['peer'], run.task_id,
                                                payload['command_id'], payload['target'])
            return self.client.get_report(run.run_id, task_id=run.task_id)

    def _dispatch(self, run, action, payload=None, event_id=None):
        if run.inflight is not None or self._closing or self.client is None:
            return False
        if action == 'begin' and (run.begin_abandoned or run.finish is not None):
            return False
        if action == 'finish' and not (run.begin_send_attempted or run.begin_done):
            return False
        attempt_key = action + (':' + event_id if event_id else '')
        limit = len(PEER_READ_OFFSETS) if action == 'peer' else self.max_attempts
        if action != 'report' and run.attempts.get(attempt_key, 0) >= limit:
            return False
        if action == 'finish' and run.attempts.get(attempt_key, 0) == 0:
            # Freeze the final identity index at first delivery, not at an earlier
            # terminal callback; its ended_at/reason have already been frozen.
            run.finish.update(command_ids=sorted(run.command_ids)[:64], request_ids=sorted(run.request_ids)[:64])
            payload = run.finish
        copied = deepcopy(payload)
        try:
            with diagnostic_context(replace=True, task_id=run.task_id,
                                    command_id=(payload or {}).get('command_id')):
                future = self.pool.submit(self._perform, run, action, copied)
        except Exception as exc:
            self._queue_rejections += 1
            run.next_at = self.clock() + self.retry_sec
            run.error = {'code': 'DIAGNOSTIC_QUEUE_UNAVAILABLE', 'message': str(redact(str(exc)))}
            self._incomplete(run, 'diagnostic_queue_unavailable')
            return False
        run.inflight, run.inflight_action, run.inflight_event_id = future, action, event_id
        run.attempts[attempt_key] = run.attempts.get(attempt_key, 0) + 1
        if action == 'report':
            run.refresh_requested = False
            run.report_polls += 1
        future.add_done_callback(lambda value: self._completed(run, action, attempt_key, event_id, value))
        return True

    @staticmethod
    def _report_ready(report):
        analysis = ((report.get('quality') or {}).get('analysis') or {}).get('state')
        return (report.get('status') in {'complete', 'partial', 'interrupted', 'recording_disabled'}
                and analysis in {'finished', 'failed'})

    @staticmethod
    def _capture_released(report):
        quality = report.get('quality') or {}
        return (quality.get('recorder_released') is True
                or (report.get('status') == 'recording_disabled' and quality.get('recording_enabled') is False))

    def _completed(self, run, action, attempt_key, event_id, future):
        with self._lock:
            if self._runs.get(run.task_id) is not run:
                return
            run.inflight = None; run.inflight_action = None; run.inflight_event_id = None
            if action == 'begin' and run.begin_abandoned and not run.begin_send_attempted:
                run.updated_at = self.now()
                return
            try:
                value = future.result()
                if action == 'peer':
                    self._peer_completed(run, event_id, value)
                    run.updated_at = self.now()
                    run.next_at = self.clock()
                    return
                if not isinstance(value, dict) or value.get('task_id') != run.task_id or value.get('run_id') != run.run_id:
                    raise DiagnosticReportError('IDENTITY_MISMATCH', 'diagnostic response identity mismatch')
            except Exception as exc:
                run.error = {'code': getattr(exc, 'code', 'DIAGNOSTIC_TRANSPORT_FAILED'),
                             'message': str(redact(str(exc)))}
                run.next_at = self.clock() + self.retry_sec
                busy = action == 'begin' and getattr(exc, 'code', None) == 'BUSY'
                permanent = (getattr(exc, 'status_code', None) in {400, 401, 403, 404, 409, 422}
                             or getattr(exc, 'code', None) in {'IDENTITY_MISMATCH', 'INVALID_REPORT',
                                 'PEER_SUMMARY_INVALID', 'PEER_TARGET_CHANGED', 'PEER_TARGET_INVALID'}) and not busy
                if action == 'peer':
                    pending = run.pending_peers.get(event_id)
                    if pending is not None:
                        if permanent or run.attempts.get(attempt_key, 0) >= len(PEER_READ_OFFSETS):
                            self._close_peer(run, event_id, 'unavailable')
                        else:
                            count = run.attempts.get(attempt_key, 0)
                            pending['next_at'] = run.peer_windows[event_id]['started_at'] + PEER_READ_OFFSETS[count]
                    self._sync_peer_pending_reason(run)
                    run.updated_at = self.now()
                    return
                if busy and self.clock() < (run.start_wait_deadline or 0.):
                    run.attempts[attempt_key] -= 1
                    run.next_at = self.clock() + self.poll_sec
                    self._incomplete(run, 'delayed_start')
                elif busy:
                    run.attempts[attempt_key] = self.max_attempts
                    self._incomplete(run, 'recorder_busy_timeout')
                if permanent:
                    run.attempts[attempt_key] = self.max_attempts
                if action == 'report':
                    run.refresh_requested = not permanent and run.report_polls < self.max_polls
                elif run.attempts.get(attempt_key, 0) >= self.max_attempts:
                    self._incomplete(run, action + '_delivery_unconfirmed')
                    if action == 'event':
                        run.events.pop(event_id, None); run.attempts.pop(attempt_key, None); run.dropped_events += 1
                    elif action == 'begin':
                        run.tracking = 'failed'
                        # Invalid/mismatched response data is not proof that the
                        # sent begin was rejected. Keep original-ID query/finish.
                        run.begin_rejected = permanent and getattr(exc, 'status_code', None) in {400, 401, 403, 409, 422}
                        run.refresh_requested = not run.begin_rejected
                    elif action == 'finish':
                        run.refresh_requested = True
                run.updated_at = self.now()
            else:
                run.error = None
                run.report = deepcopy(value)
                run.updated_at = self.now()
                run.next_at = self.clock()
                if action == 'begin':
                    run.begin_done = True
                    if run.tracking != 'superseded':
                        run.tracking = 'active'
                    run.refresh_requested = True
                elif action == 'event':
                    run.events.pop(event_id, None)
                    run.attempts.pop(attempt_key, None)
                elif action == 'finish':
                    run.finish_done = True
                    if run.tracking != 'superseded':
                        run.tracking = 'finished' if self._report_ready(value) else 'finishing'
                    run.refresh_requested = True
                elif action == 'report':
                    if (not run.query_only and not run.begin_rejected and not run.begin_abandoned and not run.begin_done and run.begin is not None
                            and timestamp((value.get('window') or {}).get('requested_start'), nullable=True)
                            == timestamp(run.begin['started_at'])):
                        run.begin_done = True
                        if run.tracking != 'superseded':
                            run.tracking = 'finishing' if run.finish is not None else 'active'
                        if 'begin_delivery_unconfirmed' in run.incomplete_reasons:
                            run.incomplete_reasons.remove('begin_delivery_unconfirmed')
                    if run.finish_done and run.tracking != 'superseded' and self._report_ready(value):
                        run.tracking = 'finished'
                    if not self._report_ready(value) and (run.finish is not None or run.query_only):
                        run.refresh_requested = run.report_polls < self.max_polls
                        run.next_at = self.clock() + self.poll_sec
                    if run.report_polls >= self.max_polls and not self._report_ready(value):
                        self._incomplete(run, 'report_poll_limit')
        # Continue an already-authorized diagnostic chain without waiting for a
        # business task to finish; delayed retries still use the existing tick.
        if not self._closing and action != 'report':
            self.tick()

    def tick(self):
        """Bounded nonblocking progress, called from existing Runtime idle/events."""
        with self._lock:
            if self._ticking:
                return
            self._ticking = True
            try:
                self._advance()
            finally:
                self._ticking = False

    def _advance(self):
        if not self.enabled or self._closing or self.client is None:
            return
        with self._lock:
            now = self.clock()
            # One lifecycle operation across runs preserves old-finish/new-begin
            # ordering without blocking Runtime. Read-only refreshes remain separate.
            if any(run.inflight is not None for run in self._runs.values()):
                return
            runs = tuple(self._runs.values())
            for run in runs:
                if run.capture_deadline is not None and now >= run.capture_deadline and run.finish is None:
                    self._finish(run, 'capture_timeout', self.now())
                for key, pending in tuple(run.pending_peers.items()):
                    if pending.get('deadline') is not None and now >= pending['deadline']:
                        self._close_peer(run, key, 'timeout')
            # Finish metadata never sits behind a backlog of event uploads. The
            # gateway accepts those original-ID events after stopping/analysis.
            for run in runs:
                if (not run.query_only and not run.begin_rejected and now >= run.next_at
                        and run.finish is not None and not run.finish_done
                        and (run.begin_done or run.begin_send_attempted)):
                    if self._dispatch(run, 'finish', run.finish):
                        return
            # A new capture takes precedence over old late metadata, but only
            # after the original recorder release is explicit or bounded BUSY handling.
            for run in runs:
                if (run.query_only or run.begin is None or run.finish is not None or run.begin_abandoned
                        or now < run.next_at or run.begin_rejected):
                    continue
                if not run.begin_done and run.tracking != 'superseded':
                    blocking = next((old for old in runs
                        if old is not run and old.begin_done and old.finish is not None
                        and not self._capture_released(old.report or {})), None)
                    if blocking is not None:
                        self._incomplete(run, 'delayed_start')
                        if now >= (run.start_wait_deadline or now):
                            run.attempts['begin'] = self.max_attempts
                            run.tracking = 'failed'
                            run.error = {'code': 'BUSY', 'message': 'Previous diagnostic capture did not finish within the start wait budget'}
                            self._incomplete(run, 'recorder_busy_timeout')
                        else:
                            run.next_at = now + self.poll_sec
                            if now >= blocking.next_at and blocking.report_polls < self.max_polls:
                                blocking.refresh_requested = True
                                if self._dispatch(blocking, 'report'):
                                    return
                        continue
                    if self._dispatch(run, 'begin', run.begin):
                        return
            # A saved original-command terminal opens its own bounded window,
            # including while Runtime remains in recovery_required and capture
            # continues. This does not finish the recorder or alter the task.
            for run in runs:
                if not run.query_only and run.begin_done and now >= run.next_at:
                    for key, pending in tuple(run.pending_peers.items()):
                        if (pending.get('deadline') is not None and now < pending['deadline']
                                and now >= pending['next_at'] and self._dispatch(run, 'peer', pending, key)):
                            return
            for run in runs:
                if now < run.next_at or run.query_only:
                    continue
                if run.events and run.begin_done and not run.begin_rejected:
                    event_id, body = next(iter(run.events.items()))
                    if self._dispatch(run, 'event', body, event_id):
                        return
            for run in runs:
                if now >= run.next_at and run.refresh_requested and run.report_polls < self.max_polls:
                    if self._dispatch(run, 'report'):
                        return

    def summary(self, task_id):
        run_id = run_id_for_task(task_id)
        with self._lock:
            run = self._runs.get(task_id)
            result = {'enabled': self.enabled, 'task_id': task_id, 'run_id': run_id,
                'tracking': 'disabled' if not self.enabled else 'not_tracked',
                'refreshing': False, 'report': None, 'error': deepcopy(self._configuration_error),
                'incomplete_reasons': [], 'dropped_events': 0, 'updated_at': None}
            if run is not None:
                result.update(tracking=run.tracking, refreshing=run.inflight_action == 'report' or run.refresh_requested,
                    report=deepcopy(run.report), error=deepcopy(run.error or self._configuration_error),
                    incomplete_reasons=list(run.incomplete_reasons), dropped_events=run.dropped_events,
                    updated_at=run.updated_at)
                result['pending_peer_indices'] = len(run.pending_peers)
            elif task_id == self._last_rejected_task:
                result.update(error={'code': 'DIAGNOSTIC_CACHE_FULL', 'message': 'Diagnostic run cache is full'},
                              incomplete_reasons=['diagnostic_cache_capacity'])
            return result

    def refresh(self, task_id):
        """Schedule GET only. This endpoint must never begin or finish a run."""
        run_id_for_task(task_id)
        if self.enabled and not self._closing and self.client is not None:
            with self._lock:
                run = self._get(task_id, query_only=True)
                if run is not None:
                    if not run.refresh_requested and run.inflight_action != 'report':
                        run.report_polls = 0
                        run.refresh_requested = True
                    if run.inflight is None:
                        self._dispatch(run, 'report')
        return self.summary(task_id)

    def health(self):
        with self._lock:
            return {'enabled': self.enabled, 'configured': self.client is not None,
                'closing': self._closing, 'cached_runs': len(self._runs),
                'inflight': sum(run.inflight is not None for run in self._runs.values()),
                'pending_events': sum(len(run.events) for run in self._runs.values()),
                'dropped_events': sum(run.dropped_events for run in self._runs.values()),
                'pending_peer_indices': sum(len(run.pending_peers) for run in self._runs.values()),
                'dropped_runs': self._dropped_runs, 'queue_rejections': self._queue_rejections,
                'configuration_error': deepcopy(self._configuration_error), 'worker': self.pool.health()}

    def close(self, timeout=1.):
        with self._lock:
            self._closing = True
            for run in self._runs.values():
                if run.begin is not None and not run.begin_send_attempted:
                    self._abandon_begin(run, 'brain_shutdown_before_begin_delivery')
                if run.begin is not None and not run.finish_done:
                    self._incomplete(run, 'brain_shutdown_coverage_unconfirmed')
        # Closing Brain never selects sources, stops a foreign run or starts a bag.
        return self.pool.close(max(0., timeout))
