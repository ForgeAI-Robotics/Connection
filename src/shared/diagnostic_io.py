"""Bounded diagnostic output. Never use this module for business ledger writes."""
from __future__ import annotations

import atexit
from collections import deque
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid


MAX_STRING = 65536
MAX_RECORD_BYTES = 524288
_SECRET = re.compile(r'^(?:authorization|proxy_authorization|cookie|set_cookie|password|passwd|secret|token|api_key|apikey|credential|credentials|access_key|private_key|signature|sig|x_amz_signature|x_amz_credential)$|(?:_token|_secret|_password|_api_key|_credential|_private_key)$', re.I)
_ASSIGNMENT = re.compile(r'''(?ix)(?<![\w-])(["']?(?:authorization|proxy[-_]authorization|cookie|set[-_]cookie|[\w-]{0,48}(?:token|secret|password|passwd|api[-_]?key|credential|signature)|access[-_]key|sig)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;&}\]\n]+)''')
_BEARER = re.compile(r'\b(Bearer|Basic)\s+[^\s,;"\'<>]+', re.I)
_URL_USER = re.compile(r'(https?://)[^/\s:@]+(?::[^/\s@]*)?@', re.I)
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)
_INSTANCE = uuid.uuid4().hex
_REVISION = None
_DIRTY = None
_META_LOCK = threading.Lock()


def clip_text(value, limit=8192):
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 80)] + f' … [diagnostic truncated: {len(text)} characters]'


def _text(value):
    text = _PRIVATE_KEY.sub('[REDACTED PRIVATE KEY]', value)
    text = _BEARER.sub(lambda m: m.group(1) + ' [REDACTED]', text)
    text = _URL_USER.sub(r'\1[REDACTED]@', text)
    text = _ASSIGNMENT.sub(lambda m: m.group(1) + '[REDACTED]', text)
    return clip_text(text, MAX_STRING)


def redact(value, *, max_depth=20, max_items=4096):
    """Pure bounded copy, retaining protocol identities and explicit unknowns."""
    remaining = [max_items]
    active = set()

    def visit(item, depth):
        remaining[0] -= 1
        if remaining[0] < 0:
            return '[diagnostic truncated: item budget]'
        if depth > max_depth:
            return '[diagnostic truncated: nesting]'
        if isinstance(item, float) and not math.isfinite(item):
            return '[diagnostic nonfinite number: ' + str(item) + ']'
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            return _text(item)
        if isinstance(item, Path):
            return _text(str(item))
        if isinstance(item, (dict, list, tuple)):
            if id(item) in active:
                return '[diagnostic truncated: recursive value]'
            active.add(id(item))
            try:
                if isinstance(item, dict):
                    result = {}
                    for key, child in item.items():
                        if remaining[0] <= 0:
                            result['_diagnostic_truncated'] = 'item budget'
                            break
                        remaining[0] -= 1  # Secret values must consume budget too.
                        key = clip_text(str(key), 256)
                        normalized = key.strip().lower().replace('-', '_')
                        result[key] = '[REDACTED]' if _SECRET.search(normalized) else visit(child, depth + 1)
                    return result
                result = []
                for child in item:
                    if remaining[0] <= 0:
                        result.append('[diagnostic truncated: item budget]')
                        break
                    result.append(visit(child, depth + 1))
                return result
            finally:
                active.remove(id(item))
        try:
            return _text(str(item))
        except Exception:
            return '[unserializable diagnostic value]'

    return visit(value, 0)


def bound_record(record, limit=MAX_RECORD_BYTES):
    """Limit serialized output with explicit field names, never fake an attachment."""
    value = redact(record)
    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode('utf-8')) <= limit:
        return value
    protected = {'time', 'event_id', 'log_schema_version', 'service_instance_id', 'service',
                 'component', 'kind', 'event', 'level', 'code_revision', 'task_id', 'step_id',
                 'attempt_id', 'command_id', 'cmd', 'request_id', 'trace_id', 'call_id', 'state'}
    fields = sorted((key for key in value if key not in protected),
                    key=lambda key: len(json.dumps(value[key], ensure_ascii=False)), reverse=True)
    omitted = []
    for key in fields:
        value[key] = '[diagnostic truncated: record byte budget]'
        omitted.append(key)
        value['diagnostic_truncated'] = True
        value['truncated_fields'] = omitted[:]
        if len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= limit:
            return value
    # Maliciously long identity strings are also bounded; a prefix is not a full identity.
    value = {key: clip_text(item, 256) if isinstance(item, str) else item for key, item in value.items()}
    value['diagnostic_truncated'] = True
    value['identity_truncated'] = True
    return value


def process_metadata():
    global _REVISION, _DIRTY
    with _META_LOCK:
        if _REVISION is None:
            try:
                repository = Path(__file__).resolve().parents[2]
                result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repository,
                                        capture_output=True, text=True, timeout=1, check=False)
                revision = result.stdout.strip()
                _REVISION = revision if result.returncode == 0 and re.fullmatch('[0-9a-f]{40,64}', revision) else 'unknown'
                status = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=normal'],
                                        cwd=repository, capture_output=True, text=True, timeout=1, check=False)
                _DIRTY = bool(status.stdout) if status.returncode == 0 else None
            except Exception:
                _REVISION = 'unknown'
        return {'service_instance_id': _INSTANCE, 'code_revision': _REVISION,
                'pid': os.getpid(), 'log_schema_version': 1, 'code_dirty': _DIRTY}


def append_rotating(path, text, *, max_bytes=16 * 1024 * 1024):
    """One writer per output path; rename retained segments, never delete history."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError('Diagnostic destination must not be a symlink')
    size = path.stat().st_size if path.exists() else 0
    if size and size + len(text.encode('utf-8')) > max_bytes:
        stamp = datetime.now().astimezone().strftime('%H%M%S%f')
        path.rename(path.with_name(f'{path.stem}.{stamp}.{uuid.uuid4().hex[:8]}{path.suffix}'))
    with path.open('a', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()


class DiagnosticWriter:
    """Serial, bounded, best-effort queue; errors are observable and never re-raised."""
    def __init__(self, *, asynchronous=False, capacity=512, name='brain-diagnostic'):
        self.asynchronous = asynchronous
        self.queue = queue.Queue(max(1, capacity))
        self._guard = threading.Lock()
        self._closing = False
        self._errors = self._dropped = self._written = 0
        self._last_error = None
        self._last_notice = 0.
        self._lost = deque(maxlen=16)
        self.thread = None
        if asynchronous:
            self.thread = threading.Thread(target=self._run, name=name, daemon=True)
            self.thread.start()
            atexit.register(self.close, 1.)

    def failure(self, error, *, event_id=None, dropped=False):
        try:
            with self._guard:
                if dropped:
                    self._dropped += 1
                else:
                    self._errors += 1
                self._last_error = clip_text(redact(str(error)), 500)
                if event_id:
                    self._lost.append(str(event_id)[:128])
                # A full queue must never make its producer block on stderr.
                # Only the I/O worker may attempt the fallback in async mode.
                may_report = not self.asynchronous or threading.current_thread() is self.thread
                notice = may_report and time.monotonic() - self._last_notice >= 10
                if notice:
                    self._last_notice = time.monotonic()
            if notice:
                # Do not recurse through logging or the tee. No business payload here.
                stream = sys.__stderr__
                if stream is not None:
                    stream.write('Diagnostic output degraded; inspect /health logging counters.\n')
                    stream.flush()
        except Exception:
            pass

    def submit(self, fn, *args, event_id=None, critical=False):
        try:
            with self._guard:
                if self._closing:
                    status = 'closed'
                elif self.asynchronous:
                    # Close and admission share the guard: once accepted, a
                    # record is queued before the worker is allowed to exit.
                    if not critical and self.queue.qsize() >= max(1, self.queue.maxsize * 3 // 4):
                        raise queue.Full
                    self.queue.put_nowait((fn, args, event_id))
                    return True
                else:
                    status = 'sync'
            if status == 'closed':
                self.failure('diagnostic writer closed', event_id=event_id, dropped=True)
                return False
            return self._write(fn, args, event_id)
        except queue.Full:
            self.failure('diagnostic queue full', event_id=event_id, dropped=True)
            return False
        except Exception as exc:
            self.failure(exc, event_id=event_id)
            return False

    def _write(self, fn, args, event_id):
        try:
            fn(*args)
            with self._guard:
                self._written += 1
            return True
        except Exception as exc:
            self.failure(exc, event_id=event_id)
            return False

    def _run(self):
        while True:
            try:
                fn, args, event_id = self.queue.get(timeout=.05)
            except queue.Empty:
                if self._closing:
                    return
                continue
            try:
                self._write(fn, args, event_id)
            finally:
                self.queue.task_done()

    def flush(self, timeout=1.):
        deadline = time.monotonic() + max(0., timeout)
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.queue.all_tasks_done.wait(remaining)
        return True

    def close(self, timeout=1.):
        with self._guard:
            self._closing = True
        deadline = time.monotonic() + max(0., timeout)
        flushed = self.flush(timeout)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(max(0., deadline - time.monotonic()))
        finished = flushed and (self.thread is None or not self.thread.is_alive())
        if not finished:
            self.failure('diagnostic shutdown timeout; pending output is not confirmed persisted')
        return finished

    def health(self):
        with self._guard:
            return {'enabled': True, 'asynchronous': self.asynchronous,
                    'queued': self.queue.qsize(), 'pending': self.queue.unfinished_tasks,
                    'capacity': self.queue.maxsize,
                    'written': self._written, 'write_errors': self._errors,
                    'dropped': self._dropped, 'last_error': self._last_error,
                    'lost_event_ids': list(self._lost), 'closing': self._closing,
                    'degraded': bool(self._errors or self._dropped),
                    'writer_alive': bool(self.thread and self.thread.is_alive())}
