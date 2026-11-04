"""Run the VLA-owned launcher; the NX gateway has an independent service lifecycle."""

from __future__ import annotations

import os
import copy
import json
import math
import shlex
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ops.services import _load_dotenv

DEFAULT_REMOTE_DIR = "/home/lgj_4090/文档/sonic_wbc/g1_brain_vla_bridge"
START_ACK = "PHYSICAL_ESTOP_READY"
ASKPASS = Path(__file__).resolve().parent / "ssh_askpass.sh"
ALLOWED_ACTIONS = {"start", "stop", "status"}
_HISTORY: deque[str] = deque(maxlen=120)
STATUS_SCHEMA = 'fq/vla-runtime-status/v1'
ACTION_SCHEMA = 'fq/vla-runtime-action/v1'
RESULT_PREFIX = 'VLA_RUNTIME_RESULT '
_STATUS_LOCK = threading.Lock()
_ACTION_LOCK = threading.Lock()
_STATUS_CACHE = None
_STATUS_KEY = None
_STATUS_AT = 0.0
STATUS_TTL_SEC = 5.0
Runner = Callable[..., subprocess.CompletedProcess]


def settings() -> dict[str, str]:
    _load_dotenv()
    from shared.networks import lan

    site = lan()
    return {
        "ssh_target": site.ssh_target("control"),
        "remote_dir": os.getenv("VLA_REMOTE_DIR", DEFAULT_REMOTE_DIR).strip()
        or DEFAULT_REMOTE_DIR,
        "ack": os.getenv("BRAIN_VLA_REAL_ACTION_ACK", START_ACK).strip() or START_ACK,
        "password": os.getenv("VLA_SSH_PASSWORD", "").strip(),
        "http_base": site.http("control"),
    }


def history() -> str:
    return "\n".join(_HISTORY)


def record(line: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    formatted = f"[{stamp}] {line}"
    _HISTORY.append(formatted)
    try:
        from shared.log_setup import append_monitor_log

        append_monitor_log("vla", formatted, stamped=True)
    except OSError:
        return


def _remote_script(action: str) -> tuple[str, float]:
    cfg = settings()
    remote_dir = shlex.quote(cfg["remote_dir"])
    runtime = "./run_agent_vla_runtime.sh"
    if action == "start":
        # The official launcher owns checks/dependencies, including managed O6.
        remote = (
            f"cd {remote_dir} && "
            f"BRAIN_VLA_REAL_ACTION_ACK={shlex.quote(cfg['ack'])} {runtime} start"
        )
        return remote, 300.0
    if action == "stop":
        return f"cd {remote_dir} && {runtime} stop", 60.0
    return f"cd {remote_dir} && {runtime} status-json", 15.0


def ssh_argv(action: str) -> tuple[list[str], float, dict[str, str]]:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"不支持的 VLA 远程操作: {action}")
    remote, timeout = _remote_script(action)
    return command_argv(remote, timeout)


def command_argv(remote: str, timeout: float) -> tuple[list[str], float, dict[str, str]]:
    """Shared SSH authentication for VLA-owned service launchers."""
    cfg = settings()
    argv = [
        "setsid",
        "-w",
        "ssh",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        "StrictHostKeyChecking=accept-new",
        cfg["ssh_target"],
        remote,
    ]
    env = os.environ.copy()
    password = cfg["password"]
    if password:
        if not ASKPASS.is_file():
            raise RuntimeError(f"缺少 SSH askpass 脚本: {ASKPASS}")
        argv[3:3] = [
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
        ]
        env["SSH_ASKPASS"] = str(ASKPASS)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = env.get("DISPLAY") or ":0"
        env["SSH_ASKPASS_PASSWORD"] = password
        env["VLA_SSH_ASKPASS_PASSWORD"] = password
    else:
        argv[3:3] = ["-o", "BatchMode=yes"]
    return argv, timeout, env


def _default_runner(
    argv: list[str], timeout: float, env: Optional[dict[str, str]] = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _friendly_error(exc: BaseException, output: str = "") -> str:
    text = " ".join(part for part in (str(exc), output) if part).strip()
    lowered = text.lower()
    if "permission denied" in lowered:
        target = settings()["ssh_target"]
        return (
            "操控机 SSH 登录失败。"
            f"请确认 VLA_SSH_PASSWORD 与 {target} 的密码一致。"
        )
    if "timed out" in lowered or "timeout" in lowered:
        return f"SSH 等待超时: {text}"
    if "name or service not known" in lowered or "could not resolve" in lowered:
        return f"找不到操控机 {settings()['ssh_target']}。请核对 config/networks.yaml 里的当前 WiFi。"
    return text or "SSH 调用失败"


def _sanitize_argv(argv: list[str]) -> str:
    visible = []
    for part in argv:
        if part.startswith("BRAIN_VLA_REAL_ACTION_ACK="):
            visible.append("BRAIN_VLA_REAL_ACTION_ACK=PHYSICAL_ESTOP_READY")
        else:
            visible.append(part)
    return " ".join(visible[:8]) + " …"


def _validate_status(value):
    if not isinstance(value, dict) or value.get('schema') != STATUS_SCHEMA:
        raise ValueError('VLA 未提供新版状态合同，请先部署匹配的 VLA 启动器')
    try:
        observed = datetime.fromisoformat(value['observed_at'])
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if observed.tzinfo is None or not -5 <= age <= 60:
            raise ValueError('stale')
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('VLA 状态时间缺失或已过期') from None
    for name in ('http', 'camera', 'o6'):
        part = value.get(name)
        if (not isinstance(part, dict) or part.get('state') not in {'running', 'stopped', 'unknown'}
                or part.get('ready') is not None and type(part.get('ready')) is not bool):
            raise ValueError('VLA 组件状态合同无效')
    if type(value['http'].get('can_stop')) is not bool:
        raise ValueError('VLA 缺少停止能力核查结果')
    if value['http'].get('managed') is not None and type(value['http']['managed']) is not bool:
        raise ValueError('VLA 服务归属字段无效')
    if type(value['camera'].get('frames_verified')) is not bool:
        raise ValueError('VLA 缺少图像核验范围')
    streams = value['camera'].get('streams', {})
    if not isinstance(streams, dict):
        raise ValueError('VLA 三路图像状态合同无效')
    for key, stream in streams.items():
        if key not in {'ego_view', 'left_wrist', 'right_wrist'} or not isinstance(stream, dict):
            raise ValueError('VLA 三路图像状态合同无效')
        if stream.get('ready') is not None and type(stream['ready']) is not bool:
            raise ValueError('VLA 图像就绪字段无效')
        for field in ('fps', 'age_ms'):
            number = stream.get(field)
            if number is not None and (type(number) not in (int, float) or not math.isfinite(number)
                    or number < 0 and (field == 'fps' or stream.get('ready') is True)):
                raise ValueError('VLA 图像测量字段无效')
    return value


def unknown_status(reason):
    return {'schema': STATUS_SCHEMA, 'observed_at': None, 'error': reason,
            'http': {'state': 'unknown', 'ready': None, 'managed': None, 'can_stop': False, 'reason': reason},
            'camera': {'state': 'unknown', 'ready': None, 'frames_verified': False, 'streams': {}},
            'o6': {'state': 'unknown', 'ready': None}}


def invalidate_status():
    global _STATUS_CACHE, _STATUS_AT, _STATUS_KEY
    with _STATUS_LOCK:
        _STATUS_CACHE, _STATUS_AT, _STATUS_KEY = None, 0.0, None


def runtime_status(*, runner=None, force=False):
    """A bounded, single-flight SSH read; never opens camera subscriptions."""
    global _STATUS_CACHE, _STATUS_AT, _STATUS_KEY
    cfg = settings()
    key = (cfg['ssh_target'], cfg['remote_dir'])
    with _STATUS_LOCK:
        if not force and _STATUS_CACHE is not None and _STATUS_KEY == key and time.monotonic() - _STATUS_AT < STATUS_TTL_SEC:
            return copy.deepcopy(_STATUS_CACHE)
        try:
            argv, timeout, env = ssh_argv('status')
            response = (runner or _default_runner)(argv, timeout, env)
            if response.returncode:
                raise RuntimeError(_friendly_error(RuntimeError(
                    f'状态查询退出码 {response.returncode}；请核对已部署支持 status-json 的 VLA 启动器'), response.stderr))
            if len(response.stdout) > 65536:
                raise ValueError('VLA 状态响应过大')
            value = _validate_status(json.loads(response.stdout))
        except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as exc:
            value = unknown_status(str(exc))
        _STATUS_CACHE, _STATUS_AT, _STATUS_KEY = copy.deepcopy(value), time.monotonic(), key
        return value


def health(service):
    from ops.services import _record_probe
    value = runtime_status()
    http = value['http']
    if http['state'] == 'unknown':
        state, detail = 'unknown', http.get('reason') or 'VLA 状态未核清'
    elif http['state'] == 'stopped':
        state, detail = 'stopped', 'HTTP 已停止；相机与 O6 分别显示实际状态'
    else:
        ready = all(value[name].get('ready') is True for name in ('http', 'camera', 'o6'))
        state, detail = ('up', 'VLA 接口、相机与 O6 检查通过') if ready else ('partial', 'HTTP 运行中，组件尚未全部就绪')
    _record_probe(service.id, detail, ok=state == 'up', signature=(state, detail))
    return {'ok': state == 'up', 'state': state, 'detail': detail,
            'url': settings()['http_base'], 'runtime': value}


def _action_result(action, stdout):
    lines = [line[len(RESULT_PREFIX):] for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    if len(lines) != 1 or len(lines[0]) > 65536:
        raise RuntimeError('VLA 未返回唯一的启停核验结果；结果未确认，请核对匹配版本和实际服务状态')
    try:
        result = json.loads(lines[0])
        if not isinstance(result, dict) or result.get('schema') != ACTION_SCHEMA or result.get('action') != action:
            raise ValueError('identity')
        status = _validate_status(result['status'])
        if result.get('ok') is not True or result.get('confirmed') is not True:
            raise RuntimeError(result.get('message') or 'VLA 启停结果未确认')
        http = status['http']
        confirmed = (http['state'] == 'stopped' if action == 'stop'
                     else http['state'] == 'running' and all(status[n].get('ready') is True for n in ('http', 'camera', 'o6')))
        if not confirmed:
            raise RuntimeError('VLA 回执与组件状态不一致，不能确认操作完成')
        if not isinstance(result.get('message'), str) or not result['message'].strip():
            raise ValueError('message')
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('VLA 启停回执合同无效，操作结果未确认') from exc


def run(action: str, *, runner: Optional[Runner] = None):
    if action == 'status':
        return runtime_status(runner=runner, force=True)
    if action not in {'start', 'stop'}:
        raise ValueError('VLA 仅支持启动和停止 HTTP')
    if not _ACTION_LOCK.acquire(blocking=False):
        raise RuntimeError('已有 VLA 启停操作正在进行，请等待原操作结果')
    try:
        return _run_action(action, runner=runner)
    finally:
        invalidate_status()
        _ACTION_LOCK.release()


def _run_action(action: str, *, runner: Optional[Runner] = None):
    argv, timeout, env = ssh_argv(action)
    if action == "start":
        record("调用 VLA 官方启动器，核验或启动相机与 O6 后启动 HTTP；NX 网关独立管理")
    else:
        record(f"远程执行 VLA {action}")
    record(_sanitize_argv(argv))
    try:
        completed = (runner or _default_runner)(argv, timeout, env)
    except subprocess.TimeoutExpired as exc:
        message = _friendly_error(exc)
        record(message)
        raise RuntimeError(message) from exc
    except OSError as exc:
        message = _friendly_error(exc)
        record(message)
        raise RuntimeError(message) from exc

    output = "\n".join(
        part.strip()
        for part in (completed.stdout or "", completed.stderr or "")
        if part and part.strip()
    )
    if completed.returncode != 0:
        message = _friendly_error(
            RuntimeError(f"ssh 退出码 {completed.returncode}"),
            output,
        )
        record(message)
        raise RuntimeError(message)
    if output:
        record(output)
    result = _action_result(action, completed.stdout or '')
    record(result['message'])
    return result
