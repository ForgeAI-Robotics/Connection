"""Daily per-process log files under ``logs/YYYY-MM-DD/<service>/<HH-MM-SS>.log``."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

_ANSI = re.compile(r'\x1b\[[0-9;]*m')

PROCESS_LOG_ENV = "FQPLANNER_PROCESS_LOG"
LOG_ROOT_ENV = "FQPLANNER_LOG_ROOT"
RECEPTION_RUNTIME_ENV = "FQPLANNER_RECEPTION_RUNTIME"
_ATTACHED = False


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
    print(text, flush=True)


def print_beijing(level, name, body):
    from shared.task_text import beijing_block
    print(beijing_block(level, body, name=name), flush=True)


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
    print(" ".join(parts), flush=True)


class _BytesTee:
    def __init__(self, original_buffer, file_obj):
        self._original = original_buffer
        self._file = file_obj

    def write(self, data):
        if not data:
            return 0
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        elif isinstance(data, bytes):
            data = _ANSI.sub('', data.decode("utf-8", "replace")).encode("utf-8")
        self._original.write(data)
        self._file.write(data.decode("utf-8", "replace"))
        self._file.flush()
        return len(data)

    def flush(self):
        self._original.flush()
        self._file.flush()


class _Tee:
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, original, file_obj):
        self._original = original
        self._file = file_obj
        self.buffer = _BytesTee(getattr(original, "buffer", original), file_obj)

    def write(self, data):
        if not data:
            return 0
        if isinstance(data, bytes):
            return self.buffer.write(data)
        data = _ANSI.sub('', data)
        self._original.write(data)
        self._file.write(data)
        self._file.flush()
        return len(data)

    def flush(self):
        self._original.flush()
        self._file.flush()

    def isatty(self):
        return self._original.isatty()

    def fileno(self):
        return self._original.fileno()


def attach_process_log(service: str) -> Path:
    """Create today's start-time log, tee stdout/stderr, and export the path."""

    global _ATTACHED
    if _ATTACHED and os.environ.get(PROCESS_LOG_ENV):
        return Path(os.environ[PROCESS_LOG_ENV])
    path = create_process_log_path(service)
    handle = path.open("a", encoding="utf-8", buffering=1)
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    handle.write(
        f"# service={service}\n# started_at={started}\n"
        f"# pid={os.getpid()}\n# argv={sys.argv!r}\n\n"
    )
    handle.flush()
    os.environ[PROCESS_LOG_ENV] = str(path)
    if service == "master":
        archive = reception_runtime_dir()
        os.environ[RECEPTION_RUNTIME_ENV] = str(archive)
    sys.stdout = _Tee(sys.stdout, handle)
    sys.stderr = _Tee(sys.stderr, handle)
    _ATTACHED = True
    from shared.quiet_http_log import configure_process_logging
    configure_process_logging(service)
    if service == "master":
        print_beijing('INFO', 'master', f'大脑进程已启动（master boot）\n  pid={os.getpid()}\n  log={path}')
        from shared.brain_journal import attach_master_journal

        attach_master_journal()
    else:
        print(f"[log] {service} -> {path}", flush=True)
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
