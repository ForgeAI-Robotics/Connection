"""Master command journal: one business fact per line, plus jsonl.

HTTP access and identical poll snapshots stay out of the human view.
Waiting loops keep polling; lines are emitted only when the observed
state actually changes, and once more when the call or task ends.
"""

from __future__ import annotations

from collections import OrderedDict
from contextvars import ContextVar
import json
import logging
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from shared.log_setup import compact_log_text, log_root
from shared.diagnostic_io import DiagnosticWriter, append_rotating, bound_record, clip_text, process_metadata, redact

SOURCE_HEADER = "X-FQ-Source"
CLIENT_HEADER = "X-FQ-Client"
OPERATOR_HEADER = "X-FQ-Operator"
VIA_HEADER = "X-FQ-Via"

BRAIN_LOG = "brain.log"
BRAIN_JSONL = "brain.jsonl"
HTTP_ACCESS_LOG = "http-access.log"
PIN_FILE = "brain.pin"


_FIELD_ORDER = (
    "source",
    "from",
    "via",
    "operator",
    "text",
    "intent",
    "risk",
    "force_new",
    "ready",
    "required",
    "blockers",
    "type",
    "id",
    "order",
    "phase",
    "peer",
    "op",
    "cmd",
    "target",
    "object",
    "goal",
    "path_m",
    "mode",
    "method",
    "path",
    "state",
    "ok",
    "duration_s",
    "reached",
    "holding",
    "reason",
    "error",
    "detail",
    "note",
)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _stamp(moment: datetime | None = None) -> datetime:
    return moment or datetime.now().astimezone()


def _json_ready(value):
    return bound_record(value) if isinstance(value, dict) else redact(value)


def _fmt_value(value, *, compact=True):
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return text
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, (int, float)) for item in value):
            inner = ",".join(
                f"{float(item):.2f}".rstrip("0").rstrip(".") for item in value
            )
            return f"({inner})"
        value = "；".join(str(item) for item in value if str(item).strip())
    return compact_log_text(value, 240) if compact else str(value)


def format_human(kind: str, event: str | None, fields: dict, *, moment=None, compact=True) -> str:
    if fields.get('_human_suppressed'):
        return ''
    if kind == 'CALL':
        from shared.task_text import readable_call_fields
        fields = readable_call_fields(fields)
    if kind == 'TASK' and event == 'reflection_completed':
        # Keep the full result and original failure in JSONL, not in the reading view.
        fields = {key: fields[key] for key in ('id', 'task_id') if key in fields}
    if kind == 'TASK' and event == 'verdict':
        fields = {key: fields[key] for key in ('id', 'task_id', 'state', 'cmd', 'command_id', 'step_id') if key in fields}
    stamp = _stamp(moment)
    clock = stamp.strftime("%Y-%m-%d %H:%M:%S.") + f"{stamp.microsecond // 1000:03d}"
    parts = [clock, f"{kind:<9}", event or "-"]
    seen = set()
    for key in _FIELD_ORDER:
        if key not in fields:
            continue
        seen.add(key)
        rendered = _fmt_value(fields[key], compact=compact)
        if rendered is None:
            continue
        parts.append(f"{key}={rendered}")
    for key, value in fields.items():
        if key in seen or key.startswith("_"):
            continue
        rendered = _fmt_value(value, compact=compact)
        if rendered is None:
            continue
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


def critical_evidence(payload):
    """Keep meaningful proof changes separate from poll clocks and telemetry arrays."""
    if not isinstance(payload, dict):
        return {}
    result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
    exit_state = payload.get('exit_state') if isinstance(payload.get('exit_state'), dict) else {}
    result_exit = result.get('exit_state') if isinstance(result.get('exit_state'), dict) else {}
    keys = ('navigation_stopped', 'policy_stopped', 'publisher_stopped', 'body_stopped',
            'hands_stopped', 'hold_verified', 'resources_released', 'action_port_released',
            'output_fenced', 'action_started', 'action_state_uncertain', 'business_outcome',
            'holding', 'holding_known', 'holding_unknown', 'object_grasped', 'released',
            'success', 'reached', 'effect_verified', 'effect', 'supports', 'identity_ok', 'time_ok')
    evidence = {}
    for prefix, item in (('response', payload), ('result', result), ('exit_state', exit_state),
                         ('result.exit_state', result_exit)):
        if isinstance(item, dict):
            for key in keys:
                if key in item:
                    evidence[prefix + '.' + key] = item[key]
            if isinstance(item.get('runtime'), dict):
                for key in keys:
                    if key in item['runtime']:
                        evidence[prefix + '.runtime.' + key] = item['runtime'][key]
    completion = payload.get('completion')
    if isinstance(completion, dict):
        for key in ('status', 'evidence', 'error', 'verified', 'requested'):
            if key in completion:
                evidence['completion.' + key] = completion[key]
    for key in ('finish_requested', 'pause_requested', 'stop_requested', 'receiver_instance_id',
                'controller_instance_id', 'input_session_id', 'robot_serial', 'holding_evidence'):
        if key in payload:
            evidence[key] = payload[key]
    handoff = payload.get('handoff') if isinstance(payload.get('handoff'), dict) else {}
    for key in ('original_execution_failure', 'o6_execution_failure'):
        if key in handoff:
            evidence['handoff.' + key] = handoff[key]
    execution = handoff.get('execution') if isinstance(handoff.get('execution'), dict) else {}
    for key in ('receiver_instance_id', 'source_gate_receiver_instance_id',
                'controller_instance_id', 'input_session_id', 'robot_serial'):
        if key in execution:
            evidence['handoff.execution.' + key] = execution[key]
    for prefix, item in (('result', result), ('exit_state', exit_state), ('result.exit_state', result_exit)):
        for key in ('feedback_fault', 'holding_evidence'):
            if key in item:
                evidence[prefix + '.' + key] = item[key]
            if isinstance(item.get('runtime'), dict) and key in item['runtime']:
                evidence[prefix + '.runtime.' + key] = item['runtime'][key]
    return evidence


def fingerprint(payload) -> tuple:
    """Stable identity of a remote command. Timestamps and motor arrays ignored."""

    if not isinstance(payload, dict):
        return ("raw", compact_log_text(payload, 180))
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    return (
        str(payload.get("state") or "").lower(),
        str(payload.get("wait_reason") or ""),
        str(error.get("code") or ""),
        str(error.get("message") or result.get("message") or ""),
        str(progress.get("message") or ""),
        str(result.get("success")),
        str(result.get("reached")),
        str(result.get("navigation_stopped")),
        str(result.get("holding")),
        str(result.get("object_grasped")),
        str(result.get("released")),
        str(plan.get("path_length_m") or ""),
        json.dumps(redact(critical_evidence(payload)), sort_keys=True, ensure_ascii=False),
        str((payload.get("dispatch") or {}).get("goal_dispatched")
            if isinstance(payload.get("dispatch"), dict) else ""),
    )


def call_reason(payload, error=None) -> str | None:
    if error is not None:
        text = str(error).strip()
        if text:
            return text
    if not isinstance(payload, dict):
        return str(payload) if payload else None
    error_body = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for candidate in (
        error_body.get("message"),
        result.get("message"),
        payload.get("wait_reason"),
        (payload.get("progress") or {}).get("message")
        if isinstance(payload.get("progress"), dict) else None,
    ):
        text = str(candidate) if candidate else ""
        if text:
            return text
    return None


def summarize_nav_request(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    goal = payload.get("goal_xyt")
    fields = {
        "cmd": payload.get("command_id"),
        "target": payload.get("target_id"),
        "mode": payload.get("motion_mode"),
        "path": "/v1/navigation/goals",
        "method": "POST",
    }
    if isinstance(goal, (list, tuple)) and len(goal) >= 2:
        fields["goal"] = [round(float(item), 4) for item in goal[:3]]
    return fields


def summarize_remote(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    dispatch = payload.get("dispatch") if isinstance(payload.get("dispatch"), dict) else {}
    path_m = plan.get("path_length_m")
    if path_m is None:
        path_m = dispatch.get("path_length_m")
    fields = {
        "cmd": payload.get("command_id"),
        "state": payload.get("state"),
        "target": payload.get("target_id") or payload.get("operation"),
        "reached": result.get("reached"),
        "holding": result.get("holding"),
    }
    if path_m is not None:
        try:
            fields["path_m"] = round(float(path_m), 3)
        except (TypeError, ValueError):
            fields["path_m"] = path_m
    reason = call_reason(payload)
    if reason:
        fields["reason"] = reason
    # Error codes, nested detail and remote tracebacks must survive the summary.
    if payload.get("error"):
        fields["error"] = payload["error"]
    proof = critical_evidence(payload)
    if proof:
        fields['evidence_changes'] = proof
    if payload.get('state') in {'failed', 'cancelled', 'succeeded'}:
        # Evidence is detached and redacted before journal output; raw business
        # return values are never changed by diagnosis.
        fields['downstream'] = payload
    return {key: value for key, value in fields.items() if value is not None}


def summarize_vla_request(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        "cmd": payload.get("command_id"),
        "object": payload.get("object_id"),
        "target": payload.get("target_area"),
        "method": "POST",
        "path": "/v1/vla/tasks",
    }


class BrainJournal:
    def __init__(self, *, service: str = "master", enabled: bool = True, echo_process=False,
                 asynchronous=False, queue_capacity=512, max_file_bytes=16 * 1024 * 1024):
        self.service = service
        self.enabled = enabled
        self.echo_process = echo_process
        self._lock = threading.RLock()
        self._current_task = ContextVar("brain_journal_task_" + uuid.uuid4().hex, default=None)
        self._writer = DiagnosticWriter(asynchronous=asynchronous, capacity=queue_capacity)
        self.max_file_bytes = max(1024, int(max_file_bytes))
        self._metadata = process_metadata()
        self._day = None
        self._dir = None
        self._pin = None

    def set_current_task(self, task_id):
        with self._lock:
            self._current_task.set(str(task_id or "") or None)

    def current_task_id(self):
        with self._lock:
            return self._current_task.get()

    def _ensure_day(self, moment: datetime):
        day = moment.strftime("%Y-%m-%d")
        if self._day == day and self._dir is not None:
            return
        folder = log_root() / day / self.service
        folder.mkdir(parents=True, exist_ok=True)
        self._day = day
        self._dir = folder

    def paths(self, *, moment: datetime | None = None):
        stamp = _stamp(moment)
        with self._lock:
            self._ensure_day(stamp)
            return {
                "dir": self._dir,
                "brain": self._dir / BRAIN_LOG,
                "jsonl": self._dir / BRAIN_JSONL,
                "http": self._dir / HTTP_ACCESS_LOG,
                "pin": self._dir / PIN_FILE,
            }

    def emit(
        self,
        kind: str,
        *,
        event: str | None = None,
        task_id=None,
        inherit_task: bool = True,
        **fields,
    ):
        if not self.enabled:
            return None
        try:
            from shared.diagnostic_context import context_fields
            context = context_fields()
            explicit = task_id or fields.get('id')
            if explicit and context.get('task_id') != explicit:
                for key in ('task_id', 'step_id', 'attempt_id', 'command_id', 'cmd', 'request_id'):
                    context.pop(key, None)
            carried = self.current_task_id() if inherit_task else None
            known_task = str(explicit or context.get('task_id') or carried or '')
            context.pop('task_id', None)
            payload = {key: value for key, value in {**context, **fields}.items()
                       if value is not None and value != ''}
            if known_task:
                payload['id'] = known_task
            delivery = self._writer.health()
            if delivery['degraded']:
                # The next delivered event preserves the loss notice even after
                # the process exits and its in-memory health counters disappear.
                payload['diagnostic_delivery'] = {key: delivery[key] for key in
                    ('write_errors', 'dropped', 'lost_event_ids', 'last_error')}
            kind_u = str(kind).upper()
            level = payload.pop('level', None) or ('ERROR' if (
                payload.get('ok') is False or payload.get('state') == 'failed'
                or payload.get('error') or payload.get('exception')
                or (payload.get('diagnostic') or {}).get('error')
                or (payload.get('recovery_advice') or {}).get('error')
                or (payload.get('blocked_reason') and payload.get('blocked_reason') != 'vla_effect_confirmation')
                or (kind_u == 'PREFLIGHT' and payload.get('ready') is False)) else 'INFO')
            moment = _stamp()
            record = bound_record({**payload, **self._metadata,
                'time': moment.isoformat(timespec='milliseconds'), 'event_id': uuid.uuid4().hex,
                'service': self.service, 'component': payload.get('component') or self.service,
                'level': level, 'kind': kind_u, 'event': event, 'task_id': known_task or None})
            critical = (level in {'ERROR', 'CRITICAL', 'WARNING', 'WARN'}
                        or record.get('state') in {'failed', 'succeeded', 'cancelled', 'recovery_required'})
            self._writer.submit(self._write_record, record, moment,
                                event_id=record['event_id'], critical=critical)
            return record
        except Exception as exc:
            self._writer.failure(exc)
            return None

    def _write_record(self, record, moment):
        # Record is an immutable-by-convention bounded copy prepared by the emitter.
        payload = {key: value for key, value in record.items() if key not in {
            'time', 'kind', 'event', 'task_id', 'service', 'component', 'level',
            'log_schema_version', 'service_instance_id', 'code_revision', 'pid', 'code_dirty', 'downstream'}}
        line = format_human(record['kind'], record.get('event'), payload, moment=moment)
        failures = []
        with self._lock:
            try:
                paths = self.paths(moment=moment)
            except Exception as exc:
                paths = None
                failures.append(exc)
            if paths:
                outputs = ((paths['jsonl'], json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'),
                           (paths['brain'], clip_text(line, 8192) + '\n' if line else ''))
                for path, text in outputs:
                    try:
                        append_rotating(path, text, max_bytes=self.max_file_bytes)
                    except Exception as exc:
                        failures.append(exc)
                try:
                    kind, event = record['kind'], str(record.get('event') or '').lower()
                    if kind == 'TASK' and (event in {'start', 'task_opened'} or record.get('state') in {'succeeded', 'cancelled'}):
                        self._pin = None
                        paths['pin'].unlink(missing_ok=True)
                    elif kind in {'TASK', 'CALL', 'STEP'} and record.get('state') != 'cancelled' and (record.get('ok') is False or event in {'fail', 'failure'}):
                        if line:
                            self._pin = clip_text(line, 8192)
                            paths['pin'].write_text(self._pin + '\n', encoding='utf-8')
                except Exception as exc:
                    failures.append(exc)
            if self.echo_process and not record.get('_process_already_logged'):
                try:
                    from shared.task_text import format_process_event
                    rendered = format_process_event(record)
                    if rendered is None:
                        rendered = format_human(record['kind'], record.get('event'), payload,
                                                moment=moment, compact=False)
                    if rendered:
                        rendered = clip_text(rendered, 8192)
                        if '[diagnostic truncated:' in rendered:
                            rendered += ' event_id=' + record['event_id'] + ' (structured record in brain.jsonl)'
                        print(rendered, flush=True)
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise OSError('; '.join(str(error) for error in failures))

    def append_http_access(self, line: str):
        if not self.enabled:
            return
        try:
            text = clip_text(redact(str(line).rstrip('\n')), 8192)
            if text:
                self._writer.submit(self._write_http, text, _stamp())
        except Exception as exc:
            self._writer.failure(exc)

    def _write_http(self, text, moment):
        with self._lock:
            append_rotating(self.paths(moment=moment)['http'], text + '\n', max_bytes=self.max_file_bytes)

    def pin_text(self) -> str | None:
        self.flush(.2)
        with self._lock:
            if self._pin:
                return self._pin
            try:
                path = self.paths()['pin']
                return path.read_text(encoding='utf-8').strip() or None if path.exists() else None
            except Exception:
                return None

    def health(self):
        from shared.log_setup import process_log_health
        return {**self._writer.health(), **self._metadata, 'process_output': process_log_health()}

    def flush(self, timeout=1.):
        return self._writer.flush(timeout)

    def close(self, timeout=1.):
        return self._writer.close(timeout)


class NullJournal:
    enabled = False

    def set_current_task(self, task_id):
        return None

    def current_task_id(self):
        return None

    def emit(self, *args, **kwargs):
        return None

    def append_http_access(self, line: str):
        return None

    def pin_text(self):
        return None

    def paths(self, *, moment=None):
        return {}

    def health(self):
        return {"enabled": False, "degraded": False, "queued": 0, "dropped": 0, "write_errors": 0}

    def flush(self, timeout=1.):
        return True

    def close(self, timeout=1.):
        return True


_JOURNAL: BrainJournal | NullJournal = NullJournal()
_ATTACHED = False
_EXCEPTION_HOOKS = None


def _restore_exception_hooks():
    global _EXCEPTION_HOOKS
    if _EXCEPTION_HOOKS:
        old_main, old_thread, main, thread = _EXCEPTION_HOOKS
        if sys.excepthook is main:
            sys.excepthook = old_main
        if threading.excepthook is thread:
            threading.excepthook = old_thread
        _EXCEPTION_HOOKS = None


def _install_exception_hooks(journal):
    global _EXCEPTION_HOOKS
    _restore_exception_hooks()
    old_main, old_thread = sys.excepthook, threading.excepthook

    def report(kind, value, tb, component):
        if issubclass(kind, (KeyboardInterrupt, SystemExit)):
            return
        try:
            journal.emit('DIAGNOSTIC', event='exception', inherit_task=False,
                         component=component, level='CRITICAL', message=str(value),
                         _process_already_logged=True,
                         exception=logging.Formatter().formatException((kind, value, tb)))
            journal.flush(.5)
        except Exception:
            pass

    def main(kind, value, tb):
        report(kind, value, tb, 'brain.process')
        old_main(kind, value, tb)

    def thread(args):
        report(args.exc_type, args.exc_value, args.exc_traceback,
               'brain.thread.' + (args.thread.name if args.thread else 'unknown'))
        old_thread(args)

    sys.excepthook, threading.excepthook = main, thread
    _EXCEPTION_HOOKS = old_main, old_thread, main, thread


def get_journal():
    return _JOURNAL


def emit(
    kind: str,
    *,
    event: str | None = None,
    task_id=None,
    inherit_task: bool = True,
    **fields,
):
    return get_journal().emit(
        kind, event=event, task_id=task_id, inherit_task=inherit_task, **fields
    )


def attach_master_journal() -> BrainJournal:
    """Create today's journal files and capture Werkzeug access logs."""

    global _JOURNAL, _ATTACHED
    journal = BrainJournal(service="master", enabled=True, echo_process=True, asynchronous=True)
    _JOURNAL = journal
    _redirect_werkzeug(journal)
    root = logging.getLogger()
    root.handlers = [handler for handler in root.handlers if not isinstance(handler, _JournalDiagnosticHandler)]
    root.addHandler(_JournalDiagnosticHandler(journal))
    _install_exception_hooks(journal)
    _ATTACHED = True
    journal.emit("PREFLIGHT", event="boot", note="Master指挥日志已就绪")
    return journal


def reset_journal_for_tests(journal=None):
    global _JOURNAL, _ATTACHED
    _JOURNAL.close(1.)
    _JOURNAL = journal if journal is not None else NullJournal()
    _ATTACHED = False
    _restore_exception_hooks()
    root = logging.getLogger()
    root.handlers = [handler for handler in root.handlers if not isinstance(handler, _JournalDiagnosticHandler)]
    logger = logging.getLogger("werkzeug")
    logger.handlers = [
        handler for handler in logger.handlers
        if not isinstance(handler, _JournalHttpHandler)
    ]


class _JournalDiagnosticHandler(logging.Handler):
    """Project warnings and exceptions into the business journal without HTTP polling noise."""
    def __init__(self, journal):
        super().__init__(logging.WARNING)
        self.journal = journal

    def emit(self, record):
        if record.name == 'werkzeug':
            return
        try:
            self.journal.emit('DIAGNOSTIC', event='exception' if record.exc_info else 'log',
                              inherit_task=False, task_id=getattr(record, 'task_id', None),
                              component=record.name, level=record.levelname,
                              _process_already_logged=True,
                              message=record.getMessage(),
                              exception=logging.Formatter().formatException(record.exc_info)
                              if record.exc_info else None)
        except Exception:
            pass  # A diagnostic must never interrupt task execution or recurse into logging.


class _JournalHttpHandler(logging.Handler):
    def __init__(self, journal: BrainJournal):
        super().__init__()
        self.journal = journal

    def emit(self, record):
        try:
            self.journal.append_http_access(self.format(record))
        except Exception:
            pass


def _redirect_werkzeug(journal: BrainJournal):
    logger = logging.getLogger("werkzeug")
    logger.setLevel(logging.INFO)
    logger.propagate = True
    logger.handlers = [
        handler for handler in logger.handlers
        if not isinstance(handler, _JournalHttpHandler)
    ]
    handler = _JournalHttpHandler(journal)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


class OutboundCall:
    """One outbound command. Polls stay silent until fingerprint changes."""

    def __init__(
        self,
        peer: str,
        op: str,
        *,
        command_id=None,
        task_id=None,
        emit_start: bool = True,
        **fields,
    ):
        self.peer = peer
        self.op = op
        self.command_id = command_id
        self.task_id = task_id
        self._fields = dict(fields)
        from shared.diagnostic_context import context_fields
        self._context = context_fields()
        if self.task_id and self._context.get('task_id') not in (None, self.task_id):
            self._context = {}
        self.task_id = self.task_id or self._context.get('task_id')
        self._started = time.monotonic()
        self._last_summary = self._started
        self._unchanged = 0
        self.wait_summary_sec = float(self._fields.pop('wait_summary_sec', 30.))
        self._fp = None
        self._last_error = None
        self._terminal_receipt = None
        self._ended = False
        if emit_start:
            self._emit("start", **self._public())

    def _public(self, extra=None):
        payload = {
            "peer": self.peer,
            "op": self.op,
            "cmd": self.command_id,
            **self._fields,
        }
        if extra:
            observed_command = extra.get('cmd')
            payload.update(extra)
            if observed_command and self.command_id and observed_command != self.command_id:
                payload['observed_command_id'] = observed_command
                payload['identity_match'] = False
        payload['cmd'] = self.command_id
        return payload

    def _emit(self, event, **extra):
        from shared.diagnostic_context import diagnostic_context
        with diagnostic_context(replace=True, **self._context):
            emit('CALL', event=event, task_id=self.task_id, **self._public(extra))

    def _summary(self, payload):
        extra = summarize_remote(payload)
        if isinstance(payload, dict) and payload.get('task_id'):
            if payload['task_id'] != self.task_id:
                extra['observed_task_id'] = payload['task_id']
                extra['identity_match'] = False if self.task_id else None
        return extra

    def accepted(self, payload=None):
        extra = self._summary(payload) if payload is not None else {}
        if payload is not None:
            self._fp = fingerprint(payload)
            if extra.get("path_m") is not None:
                self._fields["path_m"] = extra["path_m"]
        if extra.get("state") is None and isinstance(payload, dict):
            extra["state"] = payload.get("state")
        if not self._ended:
            if extra.get('state') in {'succeeded', 'failed', 'cancelled'}:
                extra['duration_s'] = round(time.monotonic() - self._started, 3)
                self._terminal_receipt = _json_ready(payload)
            self._emit("accepted", **extra)
            self._last_error = _json_ready(extra.get('error'))

    def update(self, payload):
        if self._ended or payload is None:
            return False
        fp = fingerprint(payload)
        extra = self._summary(payload)
        if fp == self._fp and _json_ready(extra.get('error')) == self._last_error:
            self._unchanged += 1
            if time.monotonic() - self._last_summary >= self.wait_summary_sec and extra.get('state') not in {'failed', 'succeeded', 'cancelled'}:
                self._emit('waiting', **extra, unchanged_polls=self._unchanged,
                           duration_s=round(time.monotonic() - self._started, 3))
                self._last_summary = time.monotonic()
                self._unchanged = 0
                return True
            return False
        self._fp = fp
        extra["state"] = extra.get("state") or (
            payload.get("state") if isinstance(payload, dict) else None
        )
        if extra.get("path_m") is not None:
            self._fields["path_m"] = extra["path_m"]
        if extra.get('state') in {'succeeded', 'failed', 'cancelled'}:
            extra['duration_s'] = round(time.monotonic() - self._started, 3)
            self._terminal_receipt = _json_ready(payload)
        extra['unchanged_polls'] = self._unchanged
        self._unchanged = 0
        self._last_summary = time.monotonic()
        self._emit("state", **extra)
        self._last_error = _json_ready(extra.get('error'))
        return True

    def end(self, ok: bool, payload=None, error=None):
        if self._ended:
            return
        self._ended = True
        extra = self._summary(payload)
        extra["ok"] = bool(ok)
        extra["duration_s"] = round(time.monotonic() - self._started, 3)
        reason = call_reason(payload, error=error)
        if reason:
            extra["reason"] = reason
        if error is not None:
            extra['exception'] = str(error)
            if isinstance(error, BaseException) and error.__traceback__ is not None:
                import traceback
                extra['exception'] = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
        if not ok and isinstance(payload, dict) and payload.get('result') is not None:
            extra['result'] = payload['result']
        if extra.get("state") is None and isinstance(payload, dict):
            extra["state"] = payload.get("state")
        if extra.get('error') and _json_ready(extra['error']) == self._last_error:
            extra['error_already_logged'] = True
        if (self._terminal_receipt is not None and _json_ready(payload) == self._terminal_receipt
                and error is None):
            extra['_human_suppressed'] = True
        self._emit("end", **extra)
        if isinstance(payload, dict):
            self._fp = fingerprint(payload)
            self._last_error = _json_ready(payload.get('error'))
            if payload.get('state') in {'failed', 'succeeded', 'cancelled'}:
                self._terminal_receipt = _json_ready(payload)


class CallBook:
    def __init__(self, peer: str):
        self.peer = peer
        self._calls: dict[str, OutboundCall] = OrderedDict()
        self._history: dict[str, OutboundCall] = OrderedDict()
        self._limit = 512
        self._lock = threading.RLock()

    def start(self, op: str, *, command_id=None, task_id=None, **fields) -> OutboundCall:
        fields.pop("op", None)
        key = str(command_id or "")
        call = OutboundCall(
            self.peer, op, command_id=command_id, task_id=task_id, **fields
        )
        if key:
            with self._lock:
                self._calls[key] = call
                while len(self._calls) > self._limit:
                    self._calls.popitem(last=False)
        return call

    def get(self, command_id) -> OutboundCall | None:
        key = str(command_id or "")
        if not key:
            return None
        with self._lock:
            return self._calls.get(key)

    def ensure(self, op: str, command_id, *, task_id=None, **fields) -> OutboundCall:
        existing = self.get(command_id)
        if existing is not None:
            return existing
        return self.start(op, command_id=command_id, task_id=task_id, **fields)

    def complete(self, command_id, ok: bool, payload=None, error=None):
        key = str(command_id or "")
        with self._lock:
            call = self._calls.pop(key, None) if key else None
        if call is None:
            return
        call.end(ok, payload=payload, error=error)
        with self._lock:
            self._history[key] = call
            while len(self._history) > self._limit:
                self._history.popitem(last=False)

    def observe(self, command_id, payload, *, task_id=None, operation='query'):
        """Log reads of an original command, including after restart or completion."""
        key = str(command_id or '')
        if not key:
            return False
        with self._lock:
            call = self._calls.get(key) or self._history.get(key)
            if call is None:
                call = self.start(operation, command_id=key, task_id=task_id, emit_start=False)
            elif task_id and call.task_id and task_id != call.task_id:
                # Do not reuse diagnostic state from a different task, even if a
                # malformed peer reuses a command ID. Keep both associations explicit.
                call = self.start(operation, command_id=key, task_id=task_id, emit_start=False)
            if call._ended:
                fp = fingerprint(payload)
                error = _json_ready(summarize_remote(payload).get('error'))
                if fp == call._fp and error == call._last_error:
                    return False
                call._ended = False
            changed = call.update(payload)
            if isinstance(payload, dict) and payload.get('state') in {'failed', 'cancelled', 'succeeded'}:
                self._history[key] = call
                while len(self._history) > self._limit:
                    self._history.popitem(last=False)
            return changed


def inbound_from_flask(request) -> dict:
    headers = getattr(request, "headers", {}) or {}
    remote = getattr(request, "remote_addr", None)
    return {
        "source": headers.get(SOURCE_HEADER) or "direct",
        "from": headers.get(CLIENT_HEADER) or remote,
        "via": headers.get(VIA_HEADER),
        "operator": headers.get(OPERATOR_HEADER),
    }


def latest_named_log(service: str, filename: str) -> Path | None:
    root = log_root()
    if not root.exists():
        return None
    try:
        days = sorted(
            (path for path in root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]") if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return None
    for day in days:
        candidate = day / service / filename
        if candidate.is_file():
            return candidate
    return None
