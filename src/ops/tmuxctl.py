"""One tmux session per brain service."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Optional

from ops.services import ROOT, Service, descendants, iter_matching_pids


def session_name(service: Service | str) -> str:
    if isinstance(service, str):
        return service
    return service.window or service.id


def _session_target(name: str) -> str:
    return f"={name}"


def _pane_target(name: str) -> str:
    # capture-pane / send-keys take a pane target. `=master` is not a pane.
    return f"={name}:"


def _run(args: list[str], *, check: bool = False, timeout: float = 8.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "tmux not found")


def tmux_available() -> bool:
    try:
        result = _run(["tmux", "-V"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def session_exists(name: str) -> bool:
    if not tmux_available():
        return False
    result = _run(["tmux", "has-session", "-t", _session_target(name)])
    return result.returncode == 0


def list_sessions() -> list[str]:
    if not tmux_available():
        return []
    result = _run(["tmux", "list-sessions", "-F", "#{session_name}"])
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def pane_pid(name: str) -> Optional[int]:
    if not session_exists(name):
        return None
    result = _run(
        [
            "tmux",
            "list-panes",
            "-t",
            _pane_target(name),
            "-F",
            "#{pane_pid} #{pane_dead}",
        ]
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    first = result.stdout.splitlines()[0].strip().split()
    if len(first) >= 2 and first[1] not in {"0", "false"}:
        return None
    try:
        return int(first[0])
    except ValueError:
        return None


def session_alive(name: str) -> bool:
    pid = pane_pid(name)
    if pid is None:
        return False
    return os.path.exists(f"/proc/{pid}")


def capture_pane(name: str, lines: int = 200) -> str:
    if not session_exists(name):
        return ""
    result = _run(
        [
            "tmux",
            "capture-pane",
            "-p",
            "-J",
            "-t",
            _pane_target(name),
            "-S",
            f"-{max(1, lines)}",
        ]
    )
    if result.returncode != 0:
        return result.stderr.strip()
    text = result.stdout.rstrip()
    parts = text.splitlines()
    return "\n".join(parts[-lines:])


def start_session(service: Service, command: str) -> None:
    name = session_name(service)
    if not name:
        raise RuntimeError(f"{service.id} 没有 tmux session 名")
    if not tmux_available():
        raise RuntimeError("未安装 tmux，请先执行: sudo apt install tmux")
    if session_alive(name):
        return
    if session_exists(name):
        _run(["tmux", "kill-session", "-t", _session_target(name)])
        time.sleep(0.2)
    result = _run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-n",
            name,
            "-c",
            str(ROOT),
            "/bin/bash",
            "-lc",
            command,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"无法创建 tmux session {name}")


def stop_session(service: Service, timeout: float = 4.0) -> None:
    name = session_name(service)
    if not name or not session_exists(name):
        return
    pid = pane_pid(name)
    _run(["tmux", "send-keys", "-t", _pane_target(name), "C-c"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not session_exists(name):
            return
        time.sleep(0.2)
    if pid:
        _terminate_tree(pid)
    _run(["tmux", "kill-session", "-t", _session_target(name)])


def tmux_pids_for(service: Service) -> set[int]:
    pid = pane_pid(session_name(service))
    if pid is None:
        return set()
    return {pid, *descendants(pid)}


def unmanaged_pids(service: Service) -> list[int]:
    owned = tmux_pids_for(service)
    return [pid for pid in iter_matching_pids(service.match) if pid not in owned]


def _terminate_tree(pid: int) -> None:
    targets = [pid, *descendants(pid)]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = []
        for target in targets:
            try:
                os.kill(target, sig)
                alive.append(target)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
        if sig == signal.SIGTERM:
            time.sleep(0.8)
            targets = alive


def stop_unmanaged(service: Service) -> list[int]:
    stopped = unmanaged_pids(service)
    if not stopped:
        return []
    if service.id == "redis":
        _shutdown_redis()
        time.sleep(0.4)
        leftover = unmanaged_pids(service)
        if leftover:
            for pid in leftover:
                _terminate_tree(pid)
        leftover = unmanaged_pids(service)
        if leftover:
            raise RuntimeError(f"{service.name} 仍有未托管进程: {leftover}")
        return stopped
    for pid in list(stopped):
        _terminate_tree(pid)
    time.sleep(0.2)
    leftover = unmanaged_pids(service)
    if leftover:
        raise RuntimeError(f"{service.name} 仍有未托管进程: {leftover}")
    return stopped


def _shutdown_redis() -> None:
    from ops.redis_service import redis_cli_bin, redis_env

    cli = redis_cli_bin()
    if not cli.exists():
        return
    subprocess.run(
        [str(cli), "-h", "127.0.0.1", "-p", "6379", "shutdown", "nosave"],
        env=redis_env(),
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )


def wait_session(service: Service, timeout: float = 8.0) -> None:
    name = session_name(service)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if session_alive(name):
            return
        time.sleep(0.2)
    raise RuntimeError(f"{service.name} 未能在 tmux session {name} 中启动")
