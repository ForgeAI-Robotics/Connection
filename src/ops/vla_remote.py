"""Run the VLA-owned launcher; relay has an independent service lifecycle."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ops.services import _load_dotenv

DEFAULT_REMOTE_DIR = "/home/lgj_4090/文档/sonic_wbc/g1_brain_vla_bridge"
START_ACK = "PHYSICAL_ESTOP_READY"
ASKPASS = Path(__file__).resolve().parent / "ssh_askpass.sh"
ALLOWED_ACTIONS = {"start", "stop", "status"}
_HISTORY: deque[str] = deque(maxlen=120)
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
            f"BRAIN_VLA_REAL_ACTION_ACK={shlex.quote(cfg['ack'])} {runtime} start && "
            f"echo '== status ==' && {runtime} status"
        )
        return remote, 180.0
    if action == "stop":
        return f"cd {remote_dir} && {runtime} stop", 60.0
    return f"cd {remote_dir} && {runtime} status", 20.0


def ssh_argv(action: str) -> tuple[list[str], float, dict[str, str]]:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"不支持的 VLA 远程操作: {action}")
    cfg = settings()
    remote, timeout = _remote_script(action)
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


def run(action: str, *, runner: Optional[Runner] = None) -> str:
    argv, timeout, env = ssh_argv(action)
    if action == "start":
        record("调用 VLA 官方启动器，由启动器执行检查、依赖准备与 HTTP 启动；relay 独立管理")
    else:
        record(f"远程执行 VLA {action}")
    record(_sanitize_argv(argv))
    try:
        completed = (runner or _default_runner)(argv, timeout, env)
    except TypeError:
        completed = (runner or _default_runner)(argv, timeout)
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
    else:
        record(f"{action} 完成")
    return output
