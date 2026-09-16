"""Service catalog, health checks, unmanaged process detection, and logs."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from log_setup import compact_log_text


ROOT = Path(__file__).resolve().parents[1]
PROBE_HISTORY: dict[str, deque[str]] = {}
_MAX_PROBES = 80


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    layer: str
    controllable: bool
    window: Optional[str] = None
    port: Optional[int] = None
    confirm_restart: bool = False
    match: str = ""
    log_service: Optional[str] = None
    extra_log: Optional[Path] = None
    health: Optional[Callable[["Service"], dict[str, Any]]] = field(
        default=None, hash=False, compare=False
    )


def _venv_python() -> Path:
    return ROOT / ".venv" / "bin" / "python"


def _feishu_python() -> Path:
    candidate = ROOT / ".venv_feishu" / "bin" / "python"
    return candidate if candidate.exists() else _venv_python()


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _http_get(url: str, timeout: float = 3.0) -> tuple[bool, str, int, str]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4000).decode("utf-8", "replace")
            ms = int((time.monotonic() - started) * 1000)
            return True, f"HTTP {response.status}", ms, body
    except urllib.error.HTTPError as exc:
        ms = int((time.monotonic() - started) * 1000)
        return False, f"HTTP {exc.code} {exc.reason}", ms, ""
    except Exception as exc:
        ms = int((time.monotonic() - started) * 1000)
        return False, str(exc), ms, ""


def summarize_task_status(payload: dict[str, Any]) -> str:
    """One-line card text. Do not dump task_status JSON onto the card."""

    if payload.get("active") is not True:
        return "空闲"
    task = compact_log_text(payload.get("task") or "", 32)
    completed = payload.get("completed")
    total = payload.get("total")
    progress = ""
    if completed is not None and total is not None:
        progress = f" {completed}/{total}"
    if payload.get("all_done"):
        if payload.get("failed"):
            return f"已结束(失败) {task}".strip()
        return f"已结束 {task}".strip()
    return f"执行中 {task}{progress}".strip()


def format_http_health_detail(ok: bool, status: str, body: str) -> str:
    if not ok:
        return status
    text = (body or "").strip()
    if not text:
        return status
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        extra = compact_log_text(text, 80)
        return f"{status} {extra}".strip()
    if isinstance(payload, dict) and ("active" in payload or "task" in payload):
        return f"{status} {summarize_task_status(payload)}"
    extra = compact_log_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), 80
    )
    return f"{status} {extra}".strip()


def _tcp_open(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _record_probe(service_id: str, line: str) -> None:
    bucket = PROBE_HISTORY.setdefault(service_id, deque(maxlen=_MAX_PROBES))
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    bucket.append(f"[{stamp}] {line}")


def probe_history(service_id: str) -> str:
    return "\n".join(PROBE_HISTORY.get(service_id, ()))


def _health_redis(_service: Service) -> dict[str, Any]:
    from scripts.start_redis import redis_cli_bin, redis_env

    cli = redis_cli_bin()
    if not cli.exists():
        return {"ok": False, "detail": "redis-cli 不存在"}
    import subprocess

    try:
        ping = subprocess.run(
            [str(cli), "-h", "127.0.0.1", "-p", "6379", "ping"],
            env=redis_env(),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
    ok = ping.returncode == 0 and ping.stdout.strip().upper() == "PONG"
    return {"ok": ok, "detail": ping.stdout.strip() or ping.stderr.strip() or "ping 失败"}


def _health_http(url: str):
    def check(_service: Service) -> dict[str, Any]:
        ok, status, ms, body = _http_get(url)
        return {
            "ok": ok,
            "detail": format_http_health_detail(ok, status, body),
            "latency_ms": ms,
        }

    return check


def _health_alive(_service: Service) -> dict[str, Any]:
    return {"ok": True, "detail": "进程在运行"}


def _health_remote(service_id: str, env_name: str, default: str):
    def check(_service: Service) -> dict[str, Any]:
        base = os.getenv(env_name, default).rstrip("/")
        url = f"{base}/health"
        ok, status, ms, body = _http_get(url)
        detail = format_http_health_detail(ok, status, body)
        _record_probe(service_id, f"{'通' if ok else '不通'} {url} {ms}ms {detail}")
        return {
            "ok": ok,
            "detail": detail,
            "latency_ms": ms,
            "url": url,
        }

    return check


def feishu_log_path() -> Path:
    raw = os.getenv("LARK_LOG_PATH", "integrations/feishu/runtime/feishu.log").strip()
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def catalog() -> list[Service]:
    _load_dotenv()
    dream = os.getenv("DREAM_BASE_URL", "http://127.0.0.1:8001")
    vla = os.getenv("VLA_BASE_URL", "http://127.0.0.1:8091")
    return [
        Service(
            id="redis",
            name="Redis",
            layer="brain",
            controllable=True,
            window="redis",
            port=6379,
            match="redis-server",
            log_service="redis",
            health=_health_redis,
        ),
        Service(
            id="master",
            name="Master",
            layer="brain",
            controllable=True,
            window="master",
            port=5000,
            confirm_restart=True,
            match="master/run.py",
            log_service="master",
            health=_health_http("http://127.0.0.1:5000/api/task_status"),
        ),
        Service(
            id="deploy",
            name="Deploy",
            layer="brain",
            controllable=True,
            window="deploy",
            port=8888,
            match="deploy/run.py",
            log_service="deploy",
            health=_health_http("http://127.0.0.1:8888/api/task_status"),
        ),
        Service(
            id="feishu",
            name="飞书桥接",
            layer="brain",
            controllable=True,
            window="feishu",
            match="integrations/feishu/run.py",
            extra_log=feishu_log_path(),
            health=_health_alive,
        ),
        Service(
            id="slaver",
            name="Slaver",
            layer="brain",
            controllable=True,
            window="slaver",
            match="slaver/run.py",
            log_service="slaver",
            health=_health_alive,
        ),
        Service(
            id="dream",
            name="导航 DREAM",
            layer="robot",
            controllable=False,
            extra_log=None,
            health=_health_remote("dream", "DREAM_BASE_URL", dream),
        ),
        Service(
            id="vla",
            name="VLA",
            layer="robot",
            controllable=False,
            health=_health_remote("vla", "VLA_BASE_URL", vla),
        ),
    ]


def by_id(service_id: str) -> Service:
    for item in catalog():
        if item.id == service_id:
            return item
    raise KeyError(service_id)


def start_shell(service: Service) -> str:
    """Shell command executed inside a tmux pane (must stay in the foreground)."""

    python = _venv_python()
    if service.id == "redis":
        from log_setup import create_process_log_path
        from scripts.start_redis import build_redis_command, redis_env

        log_path = create_process_log_path("redis")
        cmd = build_redis_command(
            daemonize=False, log_path=log_path, log_to_stdout=True
        )
        if "--daemonize" in cmd and cmd[cmd.index("--daemonize") + 1] != "no":
            raise RuntimeError("Redis tmux 启动必须前台运行")
        env = redis_env()
        ld = env.get("LD_LIBRARY_PATH", "")
        quoted = " ".join(_shell_quote(part) for part in cmd)
        return (
            f"echo '[log] redis -> {log_path}'; "
            f"export LD_LIBRARY_PATH={_shell_quote(ld)}; "
            f"exec {quoted} 2>&1 | tee -a {_shell_quote(str(log_path))}"
        )
    if service.id == "master":
        return f"exec {_shell_quote(str(python))} master/run.py"
    if service.id == "deploy":
        return f"exec {_shell_quote(str(python))} deploy/run.py"
    if service.id == "feishu":
        return f"exec {_shell_quote(str(_feishu_python()))} integrations/feishu/run.py"
    if service.id == "slaver":
        return f"exec {_shell_quote(str(python))} slaver/run.py"
    raise ValueError(f"{service.id} 不能由面板启动")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def iter_matching_pids(match: str) -> list[int]:
    if not match:
        return []
    found: list[int] = []
    proc = Path("/proc")
    if not proc.exists():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if match in cmdline:
            found.append(pid)
    return found


def descendants(pid: int) -> list[int]:
    tree: dict[int, list[int]] = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        child = int(entry.name)
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        ppid = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split(":", 1)[1].strip() or 0)
                break
        tree.setdefault(ppid, []).append(child)
    out: list[int] = []
    stack = [pid]
    while stack:
        current = stack.pop()
        for child in tree.get(current, []):
            out.append(child)
            stack.append(child)
    return out


def latest_log_file(service: Service) -> Optional[Path]:
    if service.extra_log and service.extra_log.exists():
        return service.extra_log
    name = service.log_service
    if not name:
        return None
    day = ROOT / "log" / datetime.now().strftime("%Y-%m-%d") / name
    if not day.is_dir():
        return None
    files = sorted(day.glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    return files[0] if files else None


def tail_file(path: Path, lines: int = 200) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            remaining = handle.tell()
            block = 8192
            data = b""
            found = 0
            while remaining > 0 and found <= lines:
                step = min(block, remaining)
                remaining -= step
                handle.seek(remaining)
                data = handle.read(step) + data
                found = data.count(b"\n")
            text = data.decode("utf-8", "replace")
    except OSError as exc:
        return f"(无法读取 {path}: {exc})"
    parts = text.splitlines()
    return "\n".join(parts[-lines:])


def port_open(service: Service) -> bool:
    if service.port is None:
        return False
    return _tcp_open(service.port)


def feishu_ready_from_text(text: str) -> bool:
    return "飞书长连接已就绪" in text
