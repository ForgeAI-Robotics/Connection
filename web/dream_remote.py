"""SSH into the DREAM workstation and run the reviewed navigation one-click.

现场执行口径：

    cd /home/fq/BJHYZJ_FQ/DREAM
    bash tools/g1_three_party_oneclick.sh start

该脚本会：

1. 静态 preflight（不发真机动作）；
2. 要求交互确认后输入 ``READY``；
3. 准备或复用 NX SONIC、相机、灵巧手，并拉起 4090 VLA HTTP/relay；
4. 进入 DREAM 两次 Enter 站立、解肩带确认、9882 点初始位/朝向并 Approve。

面板只自动完成 1–2，并在确认后提交官方 ``start``。站立 Enter 要人点「站立 Enter」按钮，
一次一个；9882 的 Approve 面板永远不代点。

脚本跑在导航机自己的 tmux 会话里，输出用 ``pipe-pane`` 落到导航机文件，面板只是 ``tail -F``。
这样面板重启或 SSH 断开都不会带走编排，也不会像 2026-09-17 那样把导航机的报错吞掉：
工作流等两次站立 Enter 只给 600 秒，超时会打出
``POSE ready file was not created; navigation remains locked.`` 然后退出。
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from web.services import _load_dotenv


DEFAULT_SSH_TARGET = "dream"
DEFAULT_REMOTE_DIR = "/home/fq/BJHYZJ_FQ/DREAM"
DEFAULT_HTTP = "http://192.168.5.18:8001"
DEFAULT_REVIEW = "http://192.168.5.18:9882"
ONECLICK = "tools/g1_three_party_oneclick.sh"
ASKPASS = Path(__file__).resolve().parent / "ssh_askpass.sh"
ALLOWED_ACTIONS = {
    "start",
    "resume",
    "stop",
    "status",
    "preflight",
    "check-remote",
    "stand-enter",
}
WORKFLOW_SESSION = "g1_fixed_map_relocalize_navigation"
ADAPTER_TARGET = f"{WORKFLOW_SESSION}:adapter"
# 一键脚本跑在导航机自己的 tmux 里：面板重启或 SSH 断开都带不走它，
# pipe-pane 把输出落到导航机文件，面板只是 tail，断了可以重接。
PANEL_SESSION = "g1_panel_oneclick"
REMOTE_LOG = "/tmp/fqplanner_dream_oneclick.log"
DEFAULT_FOLLOW_MARKER = "/tmp/fqplanner_dream_follow"
GATE_SECONDS = 600
GATE_HINT = (
    "注意：脚本在这里只等 600 秒（工作流 g1_fixed_map_relocalize_navigation_workflow.sh 第 563 行）。"
    "两次站立 Enter 必须在 %s 之前按完，超时它会打出 "
    "'POSE ready file was not created; navigation remains locked.' 并退出，9882 就不会起。"
)
GATE_PATTERNS = ("First Enter -> wait for stage 1 stable", "Use the opened adapter window")
REVIEW_PORT = 9882
CHECK_REMOTE_ADVISORY = (
    "check-remote 失败不拦启动：它会 SSH Administrator@大脑机（按旧 Windows 大脑写的），"
    "现在大脑是 gpu3080 Linux，这一项必然不通。"
    "官方 start 走 runtime-sync，本身跳过这台机器，并自带严格检查。"
)
FOLLOW_UP = (
    "脚本已收到 READY。现场还要完成："
    "NX 窗口若提示密码则输入导航组给的 NX 密码；"
    "adapter 两次 Enter 进入 POSE 站立；"
    "确认带子并解除肩带；"
    f"打开 {DEFAULT_REVIEW} 在 2D 图上点初始位置和朝向，然后 Approve。"
)
_HISTORY: deque[str] = deque(maxlen=160)
_FOLLOW_PROC: Optional[subprocess.Popen[bytes]] = None
_FOLLOW_LOCK = threading.Lock()
_TUNNEL_PROC: Optional[subprocess.Popen[bytes]] = None
Runner = Callable[..., subprocess.CompletedProcess]


def settings() -> dict[str, str]:
    _load_dotenv()
    return {
        "ssh_target": os.getenv("DREAM_SSH_TARGET", DEFAULT_SSH_TARGET).strip()
        or DEFAULT_SSH_TARGET,
        "remote_dir": os.getenv("DREAM_REMOTE_DIR", DEFAULT_REMOTE_DIR).strip()
        or DEFAULT_REMOTE_DIR,
        "password": os.getenv("DREAM_SSH_PASSWORD", "").strip(),
        "http_base": (
            os.getenv("DREAM_BASE_URL", DEFAULT_HTTP).strip() or DEFAULT_HTTP
        ).rstrip("/"),
        "review_url": (
            os.getenv("DREAM_REVIEW_URL", DEFAULT_REVIEW).strip() or DEFAULT_REVIEW
        ).rstrip("/"),
        "review_forward_bind": os.getenv("DREAM_REVIEW_FORWARD_BIND", "0.0.0.0").strip()
        or "0.0.0.0",
        "review_forward_port": os.getenv(
            "DREAM_REVIEW_FORWARD_PORT", str(REVIEW_PORT)
        ).strip()
        or str(REVIEW_PORT),
    }


def follow_marker() -> Path:
    return Path(os.getenv("DREAM_FOLLOW_MARKER", DEFAULT_FOLLOW_MARKER))


def history() -> str:
    return "\n".join(_HISTORY)


def record(line: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    formatted = f"[{stamp}] {line}"
    _HISTORY.append(formatted)
    try:
        from log_setup import append_monitor_log

        append_monitor_log("dream", formatted, stamped=True)
    except OSError:
        return


def _remote_script(action: str) -> tuple[str, float]:
    cfg = settings()
    remote_dir = shlex.quote(cfg["remote_dir"])
    script = f"./{ONECLICK}"
    if action == "preflight":
        return (
            f"cd {remote_dir} && "
            f"command -v tmux >/dev/null 2>&1 || {{ "
            f"echo '导航机找不到 tmux。{ONECLICK} 进入 8001/9882 工作流必须有 tmux。'; "
            f"echo '请在 192.168.5.18 安装: sudo apt install tmux'; exit 2; }}; "
            f"echo '== preflight ==' && {script} preflight",
            90.0,
        )
    if action == "check-remote":
        return (
            f"cd {remote_dir} && echo '== check-remote ==' && {script} check-remote",
            90.0,
        )
    if action == "stand-enter":
        session = shlex.quote(WORKFLOW_SESSION)
        target = shlex.quote(ADAPTER_TARGET)
        # 一次点击只发一个 Enter：第一次是接管，第二次才进 POSE 站立。
        return (
            f"tmux has-session -t {session} 2>/dev/null || {{ "
            f"echo '导航 tmux 会话不在，先点启动'; exit 2; }}; "
            f"tmux send-keys -t {target} Enter && sleep 4 && "
            f"echo '== adapter 最新输出 ==' && "
            f"tmux capture-pane -p -t {target} | tail -n 25",
            60.0,
        )
    if action in {"start", "resume"}:
        session = shlex.quote(PANEL_SESSION)
        log = shlex.quote(REMOTE_LOG)
        # tmux 给脚本一个真 TTY（它要求 [[ -t 0 ]]），并让它脱离这条 SSH 存活。
        return (
            f"tmux has-session -t {session} 2>/dev/null && {{ "
            f"echo '导航机上 {PANEL_SESSION} 会话仍在，先点停止或到导航机看这个会话'; "
            f"exit 2; }}; "
            f": > {log} && "
            f"tmux new-session -d -s {session} -x 200 -y 50 -c {remote_dir} "
            f"{shlex.quote(f'bash {ONECLICK} {action} 2>&1')} && "
            f"tmux pipe-pane -o -t {session} {shlex.quote(f'cat >> {REMOTE_LOG}')} && "
            f"echo 'ONECLICK_TMUX_STARTED session={PANEL_SESSION} log={REMOTE_LOG}'",
            60.0,
        )
    if action == "stop":
        session = shlex.quote(PANEL_SESSION)
        return (
            f"cd {remote_dir} && {script} stop; rc=$?; "
            f"tmux kill-session -t {session} 2>/dev/null && "
            f"echo '已收掉面板启动会话 {PANEL_SESSION}'; exit $rc",
            120.0,
        )
    return f"cd {remote_dir} && {script} status", 60.0


def ssh_argv(action: str, *, allocate_tty: bool = False) -> tuple[list[str], float, dict[str, str]]:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"不支持的导航远程操作: {action}")
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
    if allocate_tty:
        argv.insert(3, "-tt")
    env = os.environ.copy()
    password = cfg["password"]
    if password:
        if not ASKPASS.is_file():
            raise RuntimeError(f"缺少 SSH askpass 脚本: {ASKPASS}")
        insert_at = 4 if allocate_tty else 3
        argv[insert_at:insert_at] = [
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
        insert_at = 4 if allocate_tty else 3
        argv[insert_at:insert_at] = ["-o", "BatchMode=yes"]
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
        return (
            "导航机 SSH 登录失败。"
            "请确认 gpu3080 的 DREAM_SSH_PASSWORD 与 fq@192.168.5.18 密码一致。"
        )
    if "timed out" in lowered or "timeout" in lowered:
        return f"SSH 等待超时: {text}"
    if "name or service not known" in lowered or "could not resolve" in lowered:
        return (
            "找不到 SSH 主机 dream。"
            "请确认 ~/.ssh/config 已写入 Host dream，或设置 DREAM_SSH_TARGET。"
        )
    return text or "SSH 调用失败"


def _sanitize_argv(argv: list[str]) -> str:
    return " ".join(argv[:8]) + " …"


def _collect_output(completed: subprocess.CompletedProcess) -> str:
    return "\n".join(
        part.strip()
        for part in (completed.stdout or "", completed.stderr or "")
        if part and part.strip()
    )


def _run_sync(action: str, *, runner: Optional[Runner] = None) -> str:
    argv, timeout, env = ssh_argv(action)
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
    output = _collect_output(completed)
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


def ready_argv() -> tuple[list[str], float, dict[str, str]]:
    """等一键脚本打出 READY 提示，再用 send-keys 递进去。"""

    session = shlex.quote(PANEL_SESSION)
    remote = (
        f"for _ in $(seq 1 90); do "
        f"tmux capture-pane -p -t {session} 2>/dev/null | grep -q '输入 READY' && {{ "
        f"tmux send-keys -t {session} READY Enter; echo READY_SENT; exit 0; }}; "
        f"tmux has-session -t {session} 2>/dev/null || {{ "
        f"echo ONECLICK_SESSION_GONE; exit 3; }}; "
        f"sleep 1; done; echo READY_PROMPT_TIMEOUT; exit 3"
    )
    argv, _timeout, env = ssh_argv("status")
    return argv[:-1] + [remote], 120.0, env


def follow_argv() -> tuple[list[str], dict[str, str]]:
    argv, _timeout, env = ssh_argv("status")
    return argv[:-1] + [f"tail -n +1 -F {shlex.quote(REMOTE_LOG)}"], env


def _note_gate(line: str) -> None:
    if not any(pattern in line for pattern in GATE_PATTERNS):
        return
    deadline = datetime.now().astimezone() + timedelta(seconds=GATE_SECONDS)
    record(GATE_HINT % deadline.strftime("%H:%M:%S"))


def _reap_orphan_followers() -> None:
    """面板换进程后，上一条 tail 会变孤儿，留着只是白占 SSH 连接。"""

    subprocess.run(
        ["pkill", "-f", f"tail -n +1 -F {REMOTE_LOG}"],
        check=False,
        capture_output=True,
    )


def _follow_remote_log() -> None:
    _reap_orphan_followers()
    argv, env = follow_argv()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        record(f"跟随导航机日志失败: {_friendly_error(exc)}")
        return
    global _FOLLOW_PROC
    _FOLLOW_PROC = proc
    assert proc.stdout is not None
    previous = ""
    for raw in proc.stdout:
        text = raw.decode("utf-8", "replace").rstrip()
        if not text or text == previous:
            continue
        previous = text
        if "fqplanner_dream_oneclick.log" in text and (
            "cannot open" in text or "无法以读模式打开" in text
        ):
            record("导航机还没有本次启动的日志文件，等启动写出后自动接上")
            follow_marker().unlink(missing_ok=True)
            return
        record(text)
        _note_gate(text)


def ensure_log_follower() -> None:
    """面板重启后照样能把导航机 tmux 的输出接回来，且只留一个 tail。"""

    with _FOLLOW_LOCK:
        if _FOLLOW_PROC is not None and _FOLLOW_PROC.poll() is None:
            return
        if not follow_marker().exists():
            return
        _stop_follower()
        threading.Thread(target=_follow_remote_log, daemon=True).start()


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.5.18", 22))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


def review_tunnel_argv() -> tuple[list[str], dict[str, str]]:
    """9882 在导航机上只绑 127.0.0.1，转发到本机才能从 Mac 打开。"""

    cfg = settings()
    argv, _timeout, env = ssh_argv("status")
    base = argv[:-1]
    bind = f"{cfg['review_forward_bind']}:{cfg['review_forward_port']}"
    forward = [
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-L",
        f"{bind}:127.0.0.1:{REVIEW_PORT}",
    ]
    return base[:-1] + forward + [base[-1]], env


def ensure_review_tunnel() -> str:
    global _TUNNEL_PROC
    cfg = settings()
    port = int(cfg["review_forward_port"])
    url = f"http://{_local_ip()}:{port}"
    if _TUNNEL_PROC is not None and _TUNNEL_PROC.poll() is None:
        return url
    if _port_open("127.0.0.1", port):
        record(f"本机 {port} 已在监听，复用现有 9882 转发：{url}")
        return url
    argv, env = review_tunnel_argv()
    try:
        _TUNNEL_PROC = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        record(f"9882 转发起不来: {_friendly_error(exc)}")
        return cfg["review_url"]
    record(f"已把导航机 127.0.0.1:{REVIEW_PORT} 转发到本机 {port}，Mac 上开 {url}")
    return url


def review_url_for_panel() -> str:
    return f"http://{_local_ip()}:{int(settings()['review_forward_port'])}"


def review_ready() -> bool:
    """转发端口通、且 9882 自己的 status.json 有响应，才算审核页真的起来。"""

    port = int(settings()["review_forward_port"])
    if not _port_open("127.0.0.1", port):
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status.json", timeout=2.0
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _kill(proc: Optional[subprocess.Popen[bytes]]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    except OSError:
        return


def _stop_review_tunnel() -> None:
    global _TUNNEL_PROC
    _kill(_TUNNEL_PROC)
    _TUNNEL_PROC = None


def _stop_follower() -> None:
    global _FOLLOW_PROC
    _kill(_FOLLOW_PROC)
    _FOLLOW_PROC = None


def _run_remote(
    argv: list[str], timeout: float, env: dict[str, str], *, runner: Optional[Runner]
) -> str:
    record(_sanitize_argv(argv))
    call = runner or _default_runner
    try:
        completed = call(argv, timeout, env)
    except TypeError:
        completed = call(argv, timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        message = _friendly_error(exc)
        record(message)
        raise RuntimeError(message) from exc
    output = _collect_output(completed)
    if completed.returncode != 0:
        message = _friendly_error(
            RuntimeError(f"ssh 退出码 {completed.returncode}"), output
        )
        record(message)
        raise RuntimeError(message)
    if output:
        record(output)
    return output


def _launch(action: str, *, runner: Optional[Runner] = None) -> str:
    record("先跑官方 preflight，不发真机动作")
    _run_sync("preflight", runner=runner)
    try:
        _run_sync("check-remote", runner=runner)
    except RuntimeError:
        record(CHECK_REMOTE_ADVISORY)
    record(f"在导航机 tmux {PANEL_SESSION} 里跑 {ONECLICK} {action}")
    argv, timeout, env = ssh_argv(action)
    _run_remote(argv, timeout, env, runner=runner)
    if runner is None:
        follow_marker().write_text(f"{PANEL_SESSION}\n", encoding="utf-8")
        ensure_log_follower()
    if action == "start":
        record("等脚本要 READY，然后 send-keys 递进去")
        ready_cmd, ready_timeout, ready_env = ready_argv()
        _run_remote(ready_cmd, ready_timeout, ready_env, runner=runner)
    review = ensure_review_tunnel() if runner is None else settings()["review_url"]
    follow = FOLLOW_UP.replace(DEFAULT_REVIEW, review)
    record(follow)
    return follow


def run(action: str, *, runner: Optional[Runner] = None) -> str:
    if action in {"start", "resume"}:
        return _launch(action, runner=runner)
    if action == "stand-enter":
        record(f"向导航机 {ADAPTER_TARGET} 发 1 个 Enter，不代解肩带")
        return _run_sync("stand-enter", runner=runner)
    if action == "stop":
        record("停止 DREAM 与 VLA HTTP/relay，保留 NX SONIC / 相机 / 灵巧手")
        output = _run_sync("stop", runner=runner)
        follow_marker().unlink(missing_ok=True)
        _stop_follower()
        _stop_review_tunnel()
        return output
    record(f"远程执行导航 {action}")
    return _run_sync(action, runner=runner)
