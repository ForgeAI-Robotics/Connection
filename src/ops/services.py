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

from shared.log_setup import append_monitor_log, compact_log_text, log_root
from shared.brain_journal import (
    BRAIN_LOG,
    HTTP_ACCESS_LOG,
    PIN_FILE,
    latest_named_log,
)

MASTER_NAMED_LOGS = {BRAIN_LOG, HTTP_ACCESS_LOG}


from shared.paths import workspace_root
ROOT = workspace_root()
PROBE_HISTORY: dict[str, deque[str]] = {}
_MAX_PROBES = 80
_LAST_MONITOR: dict[str, tuple[object, object]] = {}


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
    return ROOT / "venv" / "core" / "bin" / "python"


def _3dgs_python() -> Path:
    candidate = ROOT / "venv" / "gpu" / "bin" / "python"
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
    from shared.networks import apply_proxy_bypass

    apply_proxy_bypass()


class _NoProbeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_get(url: str, timeout: float = 3.0, *, token_env: str | None = None) -> tuple[bool, str, int, str]:
    started = time.monotonic()
    try:
        if token_env:
            from shared.service_credentials import service_token
            token = service_token(token_env)
            if not token:
                raise ValueError('健康探测凭据未配置：' + token_env)
            request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token,
                                                          'Cache-Control': 'no-cache'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoProbeRedirect())
            response_context = opener.open(request, timeout=timeout)
        else:
            response_context = urllib.request.urlopen(url, timeout=timeout)
        with response_context as response:
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


def _record_probe(service_id: str, line: str, *, ok: Optional[bool] = None, signature=None,
                  record_changes: bool = False) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    formatted = f"[{stamp}] {line}"
    bucket = PROBE_HISTORY.setdefault(service_id, deque(maxlen=_MAX_PROBES))
    bucket.append(formatted)
    previous, last_signature = _LAST_MONITOR.get(service_id, (None, None))
    signature = line if signature is None else signature
    if ok is True and previous is not False and not (record_changes and signature != last_signature):
        _LAST_MONITOR[service_id] = (True, signature)
        return  # Healthy polling stays silent, including the first probe.
    if previous is ok and signature == last_signature:
        return
    if ok is True and previous is False:
        formatted = f"[{stamp}] 监测恢复：{line}"
    elif record_changes:
        formatted = f"[{stamp}] 监测状态：{line}"
    try:
        append_monitor_log(service_id, formatted, stamped=True)
    except OSError:
        return
    _LAST_MONITOR[service_id] = (ok, signature)


def probe_history(service_id: str) -> str:
    return "\n".join(PROBE_HISTORY.get(service_id, ()))


def _health_redis(_service: Service) -> dict[str, Any]:
    from ops.redis_service import redis_cli_bin, redis_env

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


def _health_remote(service_id: str, base: str):
    def check(_service: Service) -> dict[str, Any]:
        url = f"{base.rstrip('/')}/health"
        if service_id == 'dream':
            # NAV protects health as well as business routes. Use the same
            # private credential source as the brain; never log the token.
            from shared.config import load_config
            try:
                config = load_config(ROOT / 'config/brain.yaml')
                control = (config.get('reception_real') or {}).get('body_control') or {}
                token_env = control.get('nav_token_env') or control.get('token_env') or 'BRAIN_CONTROL_TOKEN'
                ok, status, ms, body = _http_get(url, token_env=token_env)
            except (OSError, ValueError, TypeError) as exc:
                ok, status, ms, body = False, str(exc), 0, ''
        else:
            ok, status, ms, body = _http_get(url)
        detail = format_http_health_detail(ok, status, body)
        _record_probe(service_id, f"{'通' if ok else '不通'} {url} {ms}ms {detail}", ok=ok, signature=(url, detail))
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
    from ops import simple_remote, gateway_service, vla_camera
    from shared.networks import lan

    site = lan()
    dream = site.http("nav")
    vla = site.http("control")
    review = site.review_url()
    return [
        Service(
            id="redis",
            name="Redis",
            layer="support",
            controllable=True,
            window="redis",
            port=6379,
            match="redis-server",
            log_service="redis",
            health=_health_redis,
        ),
        Service(
            id="master",
            name="大脑",
            layer="brain",
            controllable=True,
            window="master",
            port=5000,
            confirm_restart=True,
            match="-m brain",
            log_service="master",
            health=_health_http("http://127.0.0.1:5000/health"),
        ),
        Service(
            id="deploy",
            name="任务网页",
            layer="brain",
            controllable=True,
            window="deploy",
            port=8888,
            match="-m entries.web",
            log_service="deploy",
            health=_health_http("http://127.0.0.1:8888/api/task_status"),
        ),
        Service(
            id="feishu",
            name="飞书入口",
            layer="brain",
            controllable=True,
            window="feishu",
            match="-m entries.feishu",
            log_service="feishu",
            health=_health_alive,
        ),
        Service(
            id="voice",
            name="语音入口",
            layer="brain",
            controllable=True,
            window="voice",
            port=8890,
            match="-m entries.voice",
            log_service="voice",
            health=_health_http("http://127.0.0.1:8890/health"),
            note="识别文本 POST /publish_task，交给大脑处理",
        ),
        Service(
            id="slaver",
            name="Slaver",
            layer="support",
            controllable=True,
            window="slaver",
            match="-m execution.slaver",
            log_service="slaver",
            health=_health_alive,
        ),
        Service(
            id="reception_nav", name="接待导航协议模拟", layer="environment", controllable=True,
            window="reception_nav", port=18001, match="-m execution.reception_sim nav",
            log_service="reception_nav", health=_health_http("http://127.0.0.1:18001/health"),
            note="独立 NAV HTTP 进程 · 单罐接待协议模拟，无物理运动",
        ),
        Service(
            id="reception_vla", name="接待操控协议模拟", layer="environment", controllable=True,
            window="reception_vla", port=18091, match="-m execution.reception_sim vla",
            log_service="reception_vla", health=_health_http("http://127.0.0.1:18091/health"),
            note="独立 VLA HTTP 进程 · 校验原导航命令，延时返回模拟结果",
        ),
        Service(
            id="desk",
            name="Desk 仿真",
            layer="environment",
            controllable=True,
            window="desk",
            port=5008,
            match="simulation/backends/desk/main.py",
            log_service="desk",
            health=_health_http("http://127.0.0.1:5008/health"),
        ),
        Service(
            id="mujoco",
            name="MuJoCo 仿真",
            layer="environment",
            controllable=True,
            window="mujoco",
            port=5001,
            match="simulation/backends/mujoco/main.py",
            log_service="mujoco",
            health=_health_http("http://127.0.0.1:5001/camera/status"),
        ),
        Service(
            id="gs",
            name="3DGS 仿真",
            layer="environment",
            controllable=True,
            window="gs",
            port=5002,
            match="simulation/backends/gs/main.py",
            log_service="gs",
            health=_health_http("http://127.0.0.1:5002/camera/status"),
        ),
        Service(
            id="simple_o7",
            name="SIMPLE 仿真（远端）",
            layer="environment",
            controllable=False,
            log_service="simple_o7",
            remote_control="ssh_simple",
            remote_actions=("start", "restart", "stop"),
            note="O6 物理仿真 · 接待与单步抓取；有未核清命令时拒绝停止",
            health=simple_remote.health,
        ),
        Service(
            id="dream",
            name="导航 DREAM",
            layer="robot",
            controllable=False,
            log_service="dream",
            remote_control="ssh_dream",
            remote_actions=("stand-enter", "stop"),
            disabled_action="启动",
            disabled_reason="导航入口待同步，暂不支持从面板启动",
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
                f"将按导航组一键脚本在 {site.ip('nav')} 启动："
                "g1_brain_navigation_only_oneclick.sh。"
                "启动正式 NAV 网关链路和 DREAM 8001/9882；依导航脚本复用或初始化 SONIC。"
                "面板只代填 READY，不会代按站立 Enter，也不会点 9882。"
                f"之后仍需在 {review} 点初始位置/朝向并 Approve。"
                "确认现场已支撑、肩带连接、急停人员就位？"
            ),
            confirm_stop=True,
            confirm_stop_message=(
                "停止只关 NAV 工作流，保留独立网关及 SONIC；任务退出须先核清。"
                "确定继续？"
            ),
            note="导航入口待同步，启动暂不可用；9882 仍需人工 Approve",
            health=_health_remote("dream", dream),
        ),
        Service(
            id='vla_camera', name='VLA 相机', layer='robot', controllable=False,
            log_service='vla_camera', remote_control='ssh_vla_camera', remote_actions=('start', 'stop'),
            confirm_stop=True,
            confirm_stop_message='将结束本面板启动的三路相机会话，操控会失去图像输入。请先结束操控任务；不停止 VLA 接口、O6 或 SONIC。确定继续？',
            note='① 先启动头部＋左腕＋右腕相机，再启动 VLA 接口',
            health=vla_camera.health,
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
                "将按操控工程的 run_agent_vla_runtime.sh 启动 VLA 运行时，"
                "执行其检查与依赖准备。请先启动 VLA 相机。确认现场急停可用？"
            ),
            confirm_stop=True,
            confirm_stop_message=(
                "将停止 VLA HTTP 接口，O6 反馈与保持服务继续运行；相机、NAV 和 SONIC 不受影响。"
                "若任务正在执行，远端会拒绝停止。确定继续？"
            ),
            note="② 相机就绪后启动 VLA 接口；任务由大脑下发",
            health=_health_remote("vla", vla),
        ),
        Service(
            id="gateway", name="机器人统一网关", layer="robot", controllable=False,
            log_service="gateway", note="由大脑授权 · 连接正常不代表机器人已就绪",
            health=gateway_service.health,
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
        from shared.log_setup import create_process_log_path
        from ops.redis_service import build_redis_command, redis_env

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
        return f"exec {_shell_quote(str(python))} -m brain"
    if service.id == "deploy":
        return f"exec {_shell_quote(str(python))} -m entries.web"
    if service.id == "feishu":
        return f"exec {_shell_quote(str(python))} -m entries.feishu"
    if service.id == "voice":
        return f"exec {_shell_quote(str(python))} -m entries.voice"
    if service.id == "slaver":
        return f"exec {_shell_quote(str(python))} -m execution.slaver"
    if service.id in {"reception_nav", "reception_vla"}:
        role = "nav" if service.id == "reception_nav" else "vla"
        return f"exec {_shell_quote(str(python))} -m execution.reception_sim {role}"
    if service.id == "desk":
        return f"exec {_shell_quote(str(python))} simulation/backends/desk/main.py"
    if service.id == "mujoco":
        return (
            "export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl; "
            f"exec {_shell_quote(str(python))} "
            "simulation/backends/mujoco/main.py --no-viewer"
        )
    if service.id == "gs":
        return (
            "export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl; "
            f"exec {_shell_quote(str(_3dgs_python()))} "
            "simulation/backends/gs/main.py "
            "--no-viewer --no-composite --robot xlerobot"
        )
    raise ValueError(f"{service.id} 不能由面板启动")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _matches_service_process(argv: list[str], match: str) -> bool:
    """Match the executable/entrypoint, never text inside a launcher command.

    A tmux server can retain its first new-session command in argv, including
    `python -m brain`. Treating that as the brain kills every independent session.
    """
    if not argv or not match:
        return False
    executable = Path(argv[0]).name
    if match == "redis-server":
        # Redis rewrites argv[0] to include its listening address.
        return executable.split(" ", 1)[0] == "redis-server"
    import re
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return False
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "-m":
            expected = match.split()
            return expected[0] == "-m" and argv[index:index + len(expected)] == expected
        if arg == "-c":
            return False
        if arg in {"-W", "-X", "--check-hash-based-pycs"}:
            index += 2
            continue
        if arg == "--":
            index += 1
            break
        if not arg.startswith("-"):
            break
        index += 1
    if index >= len(argv) or match.startswith("-m "):
        return False
    script = Path(argv[index]).as_posix()
    return script == match or script.endswith("/" + match)


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
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]
        if _matches_service_process(argv, match):
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
    if name == "master" and selected == "brain":
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
                (path for path in root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]") if path.is_dir()),
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
