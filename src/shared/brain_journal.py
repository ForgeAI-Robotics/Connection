"""Master command journal: one business fact per line, plus jsonl.

HTTP access and identical poll snapshots stay out of the human view.
Waiting loops keep polling; lines are emitted only when the observed
state actually changes, and once more when the call or task ends.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from shared.log_setup import compact_log_text, log_root

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
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return compact_log_text(value, 240)


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
    def __init__(self, *, service: str = "master", enabled: bool = True, echo_process=False):
        self.service = service
        self.enabled = enabled
        self.echo_process = echo_process
        self._lock = threading.RLock()
        self._current_task_id = None
        self._day = None
        self._dir = None
        self._pin = None

    def set_current_task(self, task_id):
        with self._lock:
            self._current_task_id = str(task_id or "") or None

    def current_task_id(self):
        with self._lock:
            return self._current_task_id

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
        # inherit_task=False for inbound requests that carry no task identity of
        # their own (standalone preflight, chat). Without it they inherit the
        # previous task's id and pollute per-task greps.
        if not self.enabled:
            return None
        moment = _stamp()
        carried = self.current_task_id() if inherit_task else None
        known_task = str(task_id or fields.get("id") or carried or "")
        payload = {
            key: _json_ready(value)
            for key, value in fields.items()
            if value is not None and value != ""
        }
        if known_task:
            payload.setdefault("id", known_task)
        record = {
            "time": now_iso(),
            "kind": str(kind).upper(),
            "event": event,
            "task_id": known_task or None,
            **payload,
        }
        line = format_human(record["kind"], event, payload, moment=moment)
        with self._lock:
            paths = self.paths(moment=moment)
            with paths["brain"].open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            with paths["jsonl"].open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
            kind_u = record["kind"]
            event_l = str(event or "").lower()
            if kind_u == "TASK" and (event_l in {"start", "task_opened"}
                                    or payload.get('state') in {'succeeded', 'cancelled'}):
                self._pin = None
                if paths["pin"].exists():
                    paths["pin"].unlink()
            elif kind_u in {"TASK", "CALL", "STEP"} and payload.get('state') != 'cancelled' and (
                payload.get("ok") is False or event_l in {"fail", "failure"}
            ):
                self._pin = line
                paths["pin"].write_text(line + "\n", encoding="utf-8")
            if self.echo_process and kind_u != 'DIAGNOSTIC':
                # Ordinary logging and uncaught exceptions already reach stderr.
                # Business events also belong in the same readable process stream.
                from shared.task_text import format_process_event
                rendered = format_process_event(record)
                if rendered is None:
                    rendered = format_human(kind_u, event, payload, moment=moment, compact=False)
                if rendered:
                    print(rendered, flush=True)
        return record

    def append_http_access(self, line: str):
        if not self.enabled:
            return
        text = str(line).rstrip("\n")
        if not text:
            return
        with self._lock:
            path = self.paths()["http"]
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
                handle.flush()

    def pin_text(self) -> str | None:
        with self._lock:
            if self._pin:
                return self._pin
            try:
                path = self.paths()["pin"]
            except Exception:
                return None
            if not path.exists():
                return None
            text = path.read_text(encoding="utf-8").strip()
            return text or None


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
                         exception=logging.Formatter().formatException((kind, value, tb)))
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
    journal = BrainJournal(service="master", enabled=True, echo_process=True)
    _JOURNAL = journal
    _redirect_werkzeug(journal)
    root = logging.getLogger()
    root.handlers = [handler for handler in root.handlers if not isinstance(handler, _JournalDiagnosticHandler)]
    root.addHandler(_JournalDiagnosticHandler(journal))
    _install_exception_hooks(journal)
    _ATTACHED = True
    journal.paths()
    journal.emit("PREFLIGHT", event="boot", note="Master指挥日志已就绪")
    return journal


def reset_journal_for_tests(journal=None):
    global _JOURNAL, _ATTACHED
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
        self._started = time.monotonic()
        self._fp = None
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
            payload.update(extra)
        return payload

    def _emit(self, event, **extra):
        emit(
            "CALL",
            event=event,
            task_id=self.task_id,
            **self._public(extra),
        )

    def accepted(self, payload=None):
        extra = summarize_remote(payload) if payload is not None else {}
        if payload is not None:
            self._fp = fingerprint(payload)
            if extra.get("path_m") is not None:
                self._fields["path_m"] = extra["path_m"]
        if extra.get("state") is None and isinstance(payload, dict):
            extra["state"] = payload.get("state")
        if not self._ended:
            self._emit("accepted", **extra)

    def update(self, payload):
        if self._ended or payload is None:
            return False
        fp = fingerprint(payload)
        if fp == self._fp:
            return False
        self._fp = fp
        extra = summarize_remote(payload)
        extra["state"] = extra.get("state") or (
            payload.get("state") if isinstance(payload, dict) else None
        )
        if extra.get("path_m") is not None:
            self._fields["path_m"] = extra["path_m"]
        self._emit("state", **extra)
        return True

    def end(self, ok: bool, payload=None, error=None):
        if self._ended:
            return
        self._ended = True
        extra = summarize_remote(payload)
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
        self._emit("end", **extra)


class CallBook:
    def __init__(self, peer: str):
        self.peer = peer
        self._calls: dict[str, OutboundCall] = {}
        self._lock = threading.Lock()

    def start(self, op: str, *, command_id=None, task_id=None, **fields) -> OutboundCall:
        fields.pop("op", None)
        key = str(command_id or "")
        call = OutboundCall(
            self.peer, op, command_id=command_id, task_id=task_id, **fields
        )
        if key:
            with self._lock:
                self._calls[key] = call
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
