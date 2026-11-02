"""Daily per-process log files under ``logs/YYYY-MM-DD/<service>/<HH-MM-SS>.log``."""

from __future__ import annotations

import atexit
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

_ANSI = re.compile(r'\x1b\[[0-9;]*m')

PROCESS_LOG_ENV = "FQPLANNER_PROCESS_LOG"
LOG_ROOT_ENV = "FQPLANNER_LOG_ROOT"
RECEPTION_RUNTIME_ENV = "FQPLANNER_RECEPTION_RUNTIME"
_ATTACHED = False
_PROCESS_WRITER = None


def project_root() -> Path:
    from shared.paths import workspace_root
    return workspace_root()


def log_root() -> Path:
    override = os.environ.get(LOG_ROOT_ENV)
    if override:
        return Path(override)
    return project_root() / "logs"


def create_process_log_path(service: str, *, now: datetime | None = None) -> Path:
    """Return ``logs/<date>/<service>/<HH-MM-SS>.log`` and create parent folders."""

    name = str(service or "").strip().lower()
    if not name or any(sep in name for sep in ("/", "\\", "..")):
        raise ValueError(f"invalid log service name: {service!r}")
    stamp = now or datetime.now().astimezone()
    day_dir = log_root() / stamp.strftime("%Y-%m-%d") / name
    day_dir.mkdir(parents=True, exist_ok=True)
    filename = stamp.strftime("%H-%M-%S") + ".log"
    path = day_dir / filename
    if path.exists():
        path = day_dir / f"{stamp.strftime('%H-%M-%S')}_{os.getpid()}.log"
    return path


def reception_runtime_dir(*, now: datetime | None = None) -> Path:
    """Daily reception archive: ``logs/<date>/reception/``."""

    stamp = now or datetime.now().astimezone()
    path = log_root() / stamp.strftime("%Y-%m-%d") / "reception"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_process_note(line: str) -> None:
    """Append one line to the current process log (and console, if attached)."""

    if not os.environ.get(PROCESS_LOG_ENV):
        return
    text = str(line).rstrip("\n")
    _safe_print(text)


def print_beijing(level, name, body):
    from shared.task_text import beijing_block
    from shared.diagnostic_io import clip_text, redact
    _safe_print(clip_text(redact(beijing_block(level, body, name=name)), 8192))


def log_forward(purpose, method, url, *, status=None, duration_s=None, request_text=None,
                result=None, error=None, model=None):
    """One outbound forward in the process log. Secrets are never included."""
    ok = error is None and (status is None or int(status) < 400)
    lines = [f'{"转发完成" if ok else "转发失败"}：{purpose} {method} {url}']
    meta = []
    if model:
        meta.append(f'model={model}')
    if status is not None:
        meta.append(f'HTTP {status}')
    if duration_s is not None:
        meta.append(f'耗时={duration_s:.2f}s')
    if meta:
        lines.append('  ' + '  '.join(meta))
    if request_text:
        lines.append('  请求=' + compact_log_text(request_text, 500))
    if result is not None and str(result).strip():
        lines.append('  结果=' + compact_log_text(result, 800))
    if error is not None:
        lines.append('  error=' + str(error))
    print_beijing('INFO' if ok else 'ERROR', 'master', '\n'.join(lines))


def compact_log_text(value, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def note_task_request(kind: str, task: str, **fields) -> None:
    """Print inbound task text. Flask access logs only show method/path."""

    parts = [f"[task] {kind}", compact_log_text(task) or "(empty)"]
    for key, value in fields.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            value = "；".join(str(item) for item in value)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(f"{key}={compact_log_text(value, 240)}")
    _safe_print(" ".join(parts))


def _safe_print(text):
    from shared.diagnostic_io import redact
    try:
        print(redact(text), flush=True)
    except Exception as exc:
        if _PROCESS_WRITER is not None:
            _PROCESS_WRITER.failure(exc)


def process_log_health():
    return _PROCESS_WRITER.health() if _PROCESS_WRITER is not None else {'enabled': False, 'degraded': False}


def close_process_log(timeout=1.):
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, _Tee):
            stream.finish_line()
    return _PROCESS_WRITER.close(timeout) if _PROCESS_WRITER else True


class _ProcessFile:
    """Retain size segments and continue the same process in the new day folder."""
    def __init__(self, path, max_bytes=16 * 1024 * 1024):
        self.path, self.max_bytes = path, max_bytes
        self.root, self.service, self.filename = path.parents[2], path.parent.name, path.name

    def write(self, text):
        from shared.diagnostic_io import append_rotating
        day = datetime.now().astimezone().strftime('%Y-%m-%d')
        target = self.root / day / self.service / self.filename
        append_rotating(target, text, max_bytes=self.max_bytes)

    def flush(self):
        pass  # append_rotating opens and flushes each bounded queued batch.


class _BytesTee:
    def __init__(self, parent):
        self.parent = parent

    def write(self, data):
        text = data.decode('utf-8', 'replace') if isinstance(data, bytes) else str(data)
        self.parent.write(text)
        return len(data)

    def flush(self):
        self.parent.flush()


class _Tee:
    encoding = 'utf-8'
    errors = 'replace'

    def __init__(self, original, file_obj, writer=None):
        from shared.diagnostic_io import DiagnosticWriter
        self._original, self._file = original, file_obj
        self._writer = writer or DiagnosticWriter()
        self._pending = ''
        self._lock = threading.Lock()
        self.buffer = _BytesTee(self)

    def _output(self, text):
        errors = []
        for stream in (self._original, self._file):
            try:
                stream.write(text)
                stream.flush()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise OSError('; '.join(str(e) for e in errors))

    def write(self, data):
        from shared.diagnostic_io import clip_text, redact
        if not data:
            return 0
        if isinstance(data, bytes):
            return self.buffer.write(data)
        length = len(data)
        try:
            with self._lock:
                self._pending += _ANSI.sub('', data)
                while '\n' in self._pending:
                    line, self._pending = self._pending.split('\n', 1)
                    text = clip_text(redact(line), 8192) + '\n'
                    self._writer.submit(self._output, text, critical=('ERROR' in text or 'Traceback' in text))
                if len(self._pending) > 65536:
                    # Do not flush an arbitrary prefix which could leak a split secret.
                    self._pending = '[diagnostic truncated: unterminated output line]'
        except Exception as exc:
            self._writer.failure(exc)
        return length

    def finish_line(self):
        from shared.diagnostic_io import clip_text, redact
        with self._lock:
            if self._pending:
                text, self._pending = self._pending, ''
                self._writer.submit(self._output, clip_text(redact(text), 8192) + '\n')

    def flush(self):
        # A partial line stays buffered so print('Authorization:', secret) is
        # sanitized as one record. Closing drains any final partial line.
        if not self._writer.asynchronous:
            self._writer.flush(.1)

    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._original.fileno()


def attach_process_log(service: str) -> Path:
    """Attach fail-isolated, bounded output; business state never depends on it."""
    from shared.diagnostic_io import DiagnosticWriter, process_metadata, redact
    global _ATTACHED, _PROCESS_WRITER
    service = str(service or '').strip().lower()
    if not service or any(sep in service for sep in ('/', '\\', '..')):
        raise ValueError(f'invalid log service name: {service!r}')
    if _ATTACHED and os.environ.get(PROCESS_LOG_ENV):
        return Path(os.environ[PROCESS_LOG_ENV])
    _PROCESS_WRITER = DiagnosticWriter(asynchronous=True, name='brain-process-log')
    try:
        path = create_process_log_path(service)
    except Exception as exc:
        # Preserve a diagnosable destination even when the directory cannot be made.
        stamp = datetime.now().astimezone()
        path = log_root() / stamp.strftime('%Y-%m-%d') / service / (stamp.strftime('%H-%M-%S') + '.log')
        _PROCESS_WRITER.failure(exc)
    handle = _ProcessFile(path)
    metadata = process_metadata()
    header = (f'# service={service}\n# started_at={datetime.now().astimezone().isoformat(timespec="seconds")}\n'
              f'# pid={os.getpid()}\n# service_instance_id={metadata["service_instance_id"]}\n'
              f'# code_revision={metadata["code_revision"]}\n')
    # argv can include credentials; log only the executable/script identifier.
    _PROCESS_WRITER.submit(handle.write, redact(header), critical=True)
    os.environ[PROCESS_LOG_ENV] = str(path)
    if service == 'master':
        try:
            os.environ[RECEPTION_RUNTIME_ENV] = str(reception_runtime_dir())
        except Exception as exc:
            _PROCESS_WRITER.failure(exc)
    sys.stdout = _Tee(sys.stdout, handle, _PROCESS_WRITER)
    sys.stderr = _Tee(sys.stderr, handle, _PROCESS_WRITER)
    _ATTACHED = True
    atexit.register(close_process_log, 1.)
    from shared.quiet_http_log import configure_process_logging
    configure_process_logging(service)
    if service == 'master':
        print_beijing('INFO', 'master', f'大脑进程已启动（master boot）\n  pid={os.getpid()}\n  log={path}')
        from shared.brain_journal import attach_master_journal
        attach_master_journal()
    else:
        _safe_print(f'[log] {service} -> {path}')
    return path


def monitor_log_path(service: str, *, now: datetime | None = None) -> Path:
    """Daily append-only monitor file: ``logs/<date>/<service>/monitor.log``."""

    name = str(service or "").strip().lower()
    if not name or any(sep in name for sep in ("/", "\\", "..")):
        raise ValueError(f"invalid log service name: {service!r}")
    stamp = now or datetime.now().astimezone()
    day_dir = log_root() / stamp.strftime("%Y-%m-%d") / name
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / "monitor.log"


def append_monitor_log(service: str, line: str, *, stamped: bool = False) -> Path:
    """Append one line to today's monitor log for a panel card."""

    path = monitor_log_path(service)
    text = str(line).rstrip("\n")
    if not stamped:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        text = f"[{stamp}] {text}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    return path
