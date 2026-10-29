"""Operator-triggered three-camera launcher; no VLA task or robot control."""
from pathlib import PurePosixPath
import shlex
import subprocess
from urllib.parse import urlsplit

from ops import vla_remote

SESSION = 'connection_vla_cameras'
OWNER = 'connection-vla-camera-G1-wireless-v1'
SCRIPT = 'groot_colloction/GR00T-WholeBodyControl/local/bin/07_start_nx_cameras.sh'
RUN_DIR = '/tmp/connection-vla-camera-${UID}'


def _remote_script(action, *, lines=80):
    root = str(PurePosixPath(vla_remote.settings()['remote_dir']).parent)
    prefix = f'''set -euo pipefail
cd {shlex.quote(root)}
source scripts/select_robot.sh G1 wireless
run="{RUN_DIR}"
[[ ! -L "$run" ]] || exit 2
mkdir -p -m 700 "$run"
exec 9>"$run/launcher.lock"
flock -w 5 9
'''
    session = shlex.quote('=' + SESSION)
    owned = f'''[[ "$(tmux show-options -v -t {session} @connection_camera_owner 2>/dev/null)" == {shlex.quote(OWNER)} ]]'''
    if action == 'logs':
        return f'''set -euo pipefail
run="{RUN_DIR}"
if [[ -f "$run/start.log" ]]; then tail -n {max(1, min(int(lines), 500))} "$run/start.log"; else echo '暂无面板相机启动日志'; fi
if [[ -f "$run/exit-code" ]]; then printf '启动脚本退出码：'; cat "$run/exit-code"; fi
''', 15
    if action == 'stop':
        return prefix + f'''if ! tmux has-session -t {session} 2>/dev/null; then
  echo '没有本面板启动的相机会话；未停止外部相机服务。'; exit 0
fi
{owned} || {{ echo '会话归属不符，未停止任何进程。' >&2; exit 2; }}
tmux send-keys -t {session} C-c
for _ in {{1..15}}; do
  tmux has-session -t {session} 2>/dev/null || break
  sleep .2
done
if tmux has-session -t {session} 2>/dev/null; then
  {owned} && tmux kill-session -t {session}
fi
echo '已结束面板相机启动会话；相机是否关闭以端口状态为准。VLA、O6 与 SONIC 未操作。'
''', 20
    if action != 'start':
        raise ValueError('不支持的相机操作')
    launch = f'''set -euo pipefail
cd {shlex.quote(root)}
source scripts/select_robot.sh G1 wireless
CAMERA_EGO_ENABLED=True CAMERA_LEFT_WRIST_ENABLED=True CAMERA_RIGHT_WRIST_ENABLED=True bash {shlex.quote(SCRIPT)}
'''
    wrapper = f'''set -o pipefail
run="{RUN_DIR}"
bash -lc {shlex.quote(launch)} 2>&1 | tee "$run/start.log"
code=${{PIPESTATUS[0]}}
printf '%s\\n' "$code" > "$run/exit-code"
exit "$code"
'''
    # The original G1 script has no reuse check; do not create a second owner.
    return prefix + f'''if tmux has-session -t {session} 2>/dev/null; then
  {owned} || {{ echo '同名会话不属于本面板，未重复启动。' >&2; exit 2; }}
  echo '相机启动会话已存在，未重复启动。'; exit 0
fi
if python3 - "$NX_HOST" "$NX_CAMERA_PORT" <<'PY'
import socket, sys
try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1): pass
except OSError:
    raise SystemExit(1)
PY
then
  echo '已有相机服务端口在线，未重复启动；请核对头部和双腕三路画面。'; exit 0
fi
rm -f "$run/exit-code"
tmux new-session -d -s {shlex.quote(SESSION)} {shlex.quote('bash -lc ' + shlex.quote(wrapper))} 9>&-
tmux set-option -t {session} @connection_camera_owner {shlex.quote(OWNER)}
sleep .3
if [[ -f "$run/exit-code" ]]; then
  tail -n 30 "$run/start.log"
  exit "$(cat "$run/exit-code")"
fi
echo '三路相机启动已提交；请查看相机日志和端口状态，再启动 VLA 接口。'
''', 20


def _execute(action, *, lines=80, runner=None):
    remote, timeout = _remote_script(action, lines=lines)
    argv, timeout, env = vla_remote.command_argv('bash -lc ' + shlex.quote(remote), timeout)
    try:
        result = (runner or vla_remote._default_runner)(argv, timeout, env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(vla_remote._friendly_error(exc)) from exc
    output = '\n'.join(s.strip() for s in (result.stdout, result.stderr) if s and s.strip())
    if result.returncode:
        raise RuntimeError(vla_remote._friendly_error(RuntimeError(f'相机脚本退出码 {result.returncode}'), output))
    return output


def run(action, *, runner=None):
    from shared.log_setup import append_monitor_log
    if action not in {'start', 'stop'}:
        raise ValueError('相机卡片只支持启动和停止')
    append_monitor_log('vla_camera', '请求相机 ' + action)
    try:
        output = _execute(action, runner=runner)
    except RuntimeError as exc:
        append_monitor_log('vla_camera', str(exc))
        raise
    append_monitor_log('vla_camera', output)
    return output


def logs(lines):
    try:
        return _execute('logs', lines=lines), '操控机 /tmp/connection-vla-camera-<UID>/start.log', None
    except RuntimeError as exc:
        return '相机启动日志读取失败：' + str(exc), None, None


def health(service):
    from shared.networks import lan
    from ops.services import _tcp_open, _record_probe
    endpoint = urlsplit(lan().http('robot', 'camera_port'))
    ok = _tcp_open(endpoint.port, endpoint.hostname)
    detail = '相机发布端口已连通；尚未核验三路出帧' if ok else '相机端口未连通；请先启动相机并查看日志'
    url = f'tcp://{endpoint.hostname}:{endpoint.port}'
    _record_probe(service.id, detail, ok=ok, signature=(url, ok))
    return dict(ok=ok, detail=detail, url=url, scope='tcp_connectivity_only', frames_verified=False)
