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

from log_setup import append_monitor_log, compact_log_text, log_root
from brain_journal import (
    BRAIN_LOG,
    HTTP_ACCESS_LOG,
    PIN_FILE,
    latest_named_log,
)

MASTER_NAMED_LOGS = {BRAIN_LOG, HTTP_ACCESS_LOG}


ROOT = Path(__file__).resolve().parents[1]
PROBE_HISTORY: dict[str, deque[str]] = {}
_MAX_PROBES = 80
_LAST_MONITOR: dict[str, tuple[object, float]] = {}
_MONITOR_HEARTBEAT_SEC = 60.0


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
    remote_control: str = ""
    remote_actions: tuple[str, ...] = ()
    confirm_start: bool = False
    confirm_start_message: str = ""
    confirm_stop: bool = False
    confirm_stop_message: str = ""
    action_confirms: tuple[tuple[str, str], ...] = ()
    disabled_action: str = ""
    disabled_reason: str = ""
    note: str = ""
    health: Optional[Callable[["Service"], dict[str, Any]]] = field(
        default=None, hash=False, compare=False
    )


def _venv_python() -> Path:
    return ROOT / ".venv" / "bin" / "python"


def _feishu_python() -> Path:
    candidate = ROOT / ".venv_feishu" / "bin" / "python"
    return candidate if candidate.exists() else _venv_python()


def _3dgs_python() -> Path:
    candidate = ROOT / ".venv_3dgs" / "bin" / "python"
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
    if payload.get("state") == "RECOVERY_REQUIRED":
        return f"待恢复 {task}".strip()
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


def _record_probe(service_id: str, line: str, *, ok: Optional[bool] = None) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    formatted = f"[{stamp}] {line}"
    bucket = PROBE_HISTORY.setdefault(service_id, deque(maxlen=_MAX_PROBES))
    bucket.append(formatted)
    previous, last_write = _LAST_MONITOR.get(service_id, (object(), 0.0))
    changed = previous is not ok
    heartbeat = (time.monotonic() - last_write) >= _MONITOR_HEARTBEAT_SEC
    if not changed and not heartbeat:
        return
    try:
        append_monitor_log(service_id, formatted, stamped=True)
    except OSError:
        return
    _LAST_MONITOR[service_id] = (ok, time.monotonic())


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
        _record_probe(service_id, f"{'通' if ok else '不通'} {url} {ms}ms {detail}", ok=ok)
        return {
            "ok": ok,
            "detail": detail,
            "latency_ms": ms,
            "url": url,
        }

    return check


def feishu_ready_from_text(text: str) -> bool:
    return "飞书长连接已就绪" in text


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
            log_service="feishu",
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
            id="desk",
            name="Desk 仿真",
            layer="brain",
            controllable=True,
            window="desk",
            port=5008,
            match="serve_desk/main.py",
            log_service="desk",
            health=_health_http("http://127.0.0.1:5008/health"),
        ),
        Service(
            id="mujoco",
            name="MuJoCo 仿真",
            layer="brain",
            controllable=True,
            window="mujoco",
            port=5001,
            match="serve/main.py",
            log_service="mujoco",
            health=_health_http("http://127.0.0.1:5001/camera/status"),
        ),
        Service(
            id="gs",
            name="3DGS 仿真",
            layer="brain",
            controllable=True,
            window="gs",
            port=5002,
            match="serve_3dgs/main.py",
            log_service="gs",
            health=_health_http("http://127.0.0.1:5002/camera/status"),
        ),
        Service(
            id="dream",
            name="导航 DREAM",
            layer="robot",
            controllable=False,
            log_service="dream",
            remote_control="ssh_dream",
            remote_actions=("start", "stand-enter", "stop"),
            action_confirms=(
                (
                    "stand-enter",
                    "只向导航机 adapter 窗口发 1 个 Enter。"
                    "第一次点是接管（等同手柄 A+X+B+Y），此时肩带必须还挂着；"
                    "等 adapter 报 stage 1 稳定后再点第二次，那一次才进 POSE 站立。"
                    "面板不会代解肩带、不会点 9882。"
                    "确认现场有人扶住、急停在手？",
                ),
            ),
            confirm_start=True,
            confirm_start_message=(
                "将按导航组一键脚本在 192.168.5.18 启动："
                "g1_three_party_oneclick.sh。"
                "会准备 NX SONIC、DREAM 8001/9882，以及 4090 上的 VLA HTTP/relay。"
                "面板只代填 READY，不会代按站立 Enter，也不会点 9882。"
                "之后仍需在 http://192.168.5.18:9882 点初始位置/朝向并 Approve。"
                "确认现场已支撑、肩带连接、急停人员就位？"
            ),
            confirm_stop=True,
            confirm_stop_message=(
                "停止只关 DREAM 与 VLA HTTP/relay，保留 NX SONIC、相机和灵巧手。"
                "确定继续？"
            ),
            note="一键脚本拉 SONIC+导航+VLA HTTP；9882 需人工 Approve",
            health=_health_remote("dream", "DREAM_BASE_URL", dream),
        ),
        Service(
            id="vla",
            name="VLA",
            layer="robot",
            controllable=False,
            log_service="vla",
            remote_control="ssh_vla",
            remote_actions=("start", "stop"),
            confirm_start=True,
            confirm_start_message=(
                "将按文档在 4090 上启动完整 VLA 运行时："
                "check 全部脚本/hook → 检查 SONIC/相机/灵巧手 → "
                "启动 8091 HTTP 和导航 relay。"
                "不启动 SONIC，也不提前启动抓取 RTC。确认现场急停可用？"
            ),
            confirm_stop=True,
            confirm_stop_message=(
                "停止只关 Brain HTTP 和受管 relay，不停 SONIC、相机或灵巧手。"
                "若任务正在执行，远端会拒绝停止。确定继续？"
            ),
            note="按文档启动 8091 HTTP + 导航 relay",
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
    if service.id == "desk":
        return f"exec {_shell_quote(str(python))} serve_desk/main.py"
    if service.id == "mujoco":
        return (
            "export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl; "
            f"exec {_shell_quote(str(python))} serve/main.py --no-viewer"
        )
    if service.id == "gs":
        return (
            "export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl; "
            f"exec {_shell_quote(str(_3dgs_python()))} serve_3dgs/main.py "
            "--no-viewer --no-composite --robot xlerobot"
        )
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


def latest_log_file(service: Service, kind: str | None = None) -> Optional[Path]:
    name = service.log_service
    selected = str(kind or "auto").strip().lower()
    if name == "master" and selected in {"auto", "brain"}:
        found = latest_named_log("master", BRAIN_LOG)
        if found:
            return found
        if selected == "brain":
            return None
    if name == "master" and selected == "http":
        return latest_named_log("master", HTTP_ACCESS_LOG)
    files: list[Path] = []
    name = service.log_service
    root = log_root()
    if name and root.exists():
        try:
            days = sorted(
                (path for path in root.iterdir() if path.is_dir()),
                key=lambda path: path.name,
                reverse=True,
            )
        except OSError:
            days = []
        for day in days:
            folder = day / name
            if not folder.is_dir():
                continue
            files.extend(folder.glob("*.log"))
            if name == "master":
                files = [path for path in files if path.name not in MASTER_NAMED_LOGS]
            if files:
                break
    if service.extra_log and service.extra_log.exists():
        files.append(service.extra_log)
    timed: list[tuple[float, Path]] = []
    for path in files:
        try:
            timed.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not timed:
        return None
    return max(timed)[1]


def master_pin_text() -> Optional[str]:
    path = latest_named_log("master", PIN_FILE)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def display_log_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


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
