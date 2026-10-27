"""Local relay process management; forwarding remains exclusively brain-owned."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import time
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from ops import tmuxctl
from ops.services import ROOT, _load_dotenv, _record_probe, iter_matching_pids
from shared.config import load_config
from shared.service_credentials import service_token
from contracts.body_control import VERSION, REVISION, fresh


def project_root():
    # Reuse the existing operator-configured VLA checkout location.
    from ops.vla_remote import DEFAULT_REMOTE_DIR
    return Path(os.environ.get('VLA_REMOTE_DIR') or DEFAULT_REMOTE_DIR).parent


def settings():
    _load_dotenv()
    config = load_config(ROOT / 'config/brain.yaml')
    real = config.get('reception_real') or {}
    control = real.get('body_control') or {}
    base = str(control.get('relay_base_url') or '').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('请先配置 reception_real.body_control.relay_base_url')
    return config, control, base


def read_status():
    _, control, base = settings()
    token = service_token(control.get('token_env', 'BRAIN_CONTROL_TOKEN'))
    if not token:
        raise ValueError('relay 管理凭据未配置')
    request = Request(base + '/v1/relay/status', headers={
        'Authorization': 'Bearer ' + token, 'Cache-Control': 'no-cache'})
    with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
        value = json.load(response)
    if (not isinstance(value, dict) or value.get('contract_version') != VERSION
            or value.get('coordination_revision') != REVISION
            or value.get('robot_serial') != control.get('robot_serial')
            or value.get('action_endpoint') != control.get('action_endpoint')
            or not value.get('relay_instance_id') or not fresh(value.get('updated_at'))):
        raise ValueError('relay 回执身份、版本或时间不匹配')
    return value


def health(_service):
    started = time.monotonic()
    base = ''
    try:
        _, _, base = settings()
        value = read_status()
        if value.get('recovery_required') is not False:
            detail = '管理在线 · 待恢复核查'
        elif value.get('forwarding_enabled') is True:
            detail = '管理在线 · 转发已开启 · ' + (
                '上游新鲜' if value.get('upstream_fresh') is True else '上游新鲜度未确认')
        elif value.get('bound') is False and value.get('action_port_released') is True:
            detail = '管理在线 · 转发关闭 · 动作端口已释放'
        else:
            detail = '管理在线 · 通道状态未核清'
        ready = value.get('execution_readiness') or {}
        if value.get('forwarding_enabled') is not True:
            if ready.get('ready') is True:
                detail += ' · 执行准备已就绪'
            else:
                detail += ' · 执行未就绪'
                blockers = ready.get('blockers') or []
                if blockers:
                    detail += '：' + '; '.join(str(item) for item in blockers)
        result = dict(ok=True, detail=detail, relay=value)
    except Exception as exc:
        result = dict(ok=False, detail=str(exc))
    result.update(url=base + '/v1/relay/status' if base else '',
                  latency_ms=int((time.monotonic() - started) * 1000))
    value = result.get('relay') or {}
    association = {key: value.get(key) for key in ('relay_instance_id', 'task_id', 'command_id', 'control_authorization')}
    signature = (result['detail'], json.dumps(association, sort_keys=True))
    identity = ' '.join(f'{key}={value[key]}' for key in ('relay_instance_id', 'task_id', 'command_id') if value.get(key))
    _record_probe('relay', result['detail'] + (' · ' + identity if identity else ''),
                  ok=result['ok'], signature=signature, record_changes=True)
    return result


def matching_pids(service):
    """An exact script match is not enough: never stop another robot/profile."""
    _, control, base = settings()
    parsed = urlsplit(base)
    expected = {'--control-host': parsed.hostname,
                '--control-port': str(parsed.port or (443 if parsed.scheme == 'https' else 80)),
                '--nx-action-bind': control['action_endpoint']}
    pids = iter_matching_pids(service.match)
    for pid in pids:
        args = Path(f'/proc/{pid}/cmdline').read_bytes().decode().rstrip('\0').split('\0')
        for flag, value in expected.items():
            if args.count(flag) != 1 or args.index(flag) + 1 >= len(args) or args[args.index(flag) + 1] != value:
                raise RuntimeError('relay 进程参数与当前配置不符，拒绝接管或停止')
    if len(pids) > 1:
        raise RuntimeError('发现多个 relay 进程，需先核查')
    return pids


@contextmanager
def maintenance_guard():
    # The existing Runtime writer lock also fences control grants while stopping.
    from brain.storage.body_control import ControlStore
    from brain.storage.tasks import load_existing
    from brain.service_support import runtime_dir
    config, control, _ = settings()
    store = ControlStore(runtime_dir(config))
    with store.writer():
        task = load_existing(runtime_dir(config)) or {}
        if task and (task.get('state') not in {'succeeded', 'failed', 'cancelled'}
                     or task.get('open_command_id')):
            raise RuntimeError('大脑仍有活动或未核清命令，拒绝启停 relay')
        state = store.read()
        if control.get('enabled') and state is None:
            raise RuntimeError('大脑控制账本缺失，拒绝启停 relay')
        if state and any((state.get('binding') or {}).get(key) != expected for key, expected in (
                ('robot_serial', control.get('robot_serial')),
                ('action_endpoint', control.get('action_endpoint')),
                ('relay', control.get('relay_base_url')))):
            raise RuntimeError('控制账本与当前 relay 配置不匹配，拒绝启停')
        if state and (state.get('current') or state.get('busy')
                      or any(row.get('state') != 'released' for row in state['leases'].values())
                      or any(op.get('receipt', {}).get('state') == 'pending'
                             or (op.get('receipt', {}).get('state') == 'unconfirmed'
                                 and state['leases'].get(op.get('body', {}).get('lease_id'), {}).get('state') != 'released')
                             for op in state['requests'].values())):
            raise RuntimeError('原控制授权尚未结束，拒绝启停 relay')
        yield


def start_command():
    from shared.log_setup import create_process_log_path
    _, control, base = settings()
    root = project_root()
    script = root / 'g1_navigation_vla_bridge/run_sequential_navigation_action_relay.sh'
    if not script.is_file():
        raise RuntimeError('本机未找到 relay 启动脚本，请核对 VLA_REMOTE_DIR')
    parsed = urlsplit(base)
    log = create_process_log_path('relay')
    q = shlex.quote
    # The existing loader chooses the robot/network. Check the selection before
    # exec so stale defaults can never silently start a different robot's relay.
    return '\n'.join([
        'set -euo pipefail',
        f'exec >>{q(str(log))} 2>&1',
        "echo 'relay management starting; forwarding starts closed'",
        f'source {q(str(root / "scripts/load_robot_network_config.sh"))}',
        f'source {q(str(root / "config/g1_tasks/control_services.env"))}',
        f'[[ "$EXPECTED_NX_SERIAL" == {q(control["robot_serial"])} && '
        f'"$RELAY_CONTROL_HOST" == {q(parsed.hostname)} && '
        f'"$RELAY_CONTROL_PORT" == {q(str(parsed.port or 80))} && '
        f'"${{NX_ACTION_BIND:-tcp://$WORKSTATION_HOST:$SONIC_ACTION_PORT}}" == {q(control["action_endpoint"])} ]] '
        '|| { echo "relay profile/endpoint mismatch"; exit 2; }',
        f'exec bash {q(str(script))}',
    ])


def start(service):
    # Clicking start must never kill an independently launched existing relay.
    if matching_pids(service):
        read_status()
        return
    with maintenance_guard():
        if matching_pids(service):
            return
        if tmuxctl.session_alive(service.window):
            raise RuntimeError('relay tmux 会话被其他进程占用，拒绝覆盖')
        tmuxctl.start_session(service, start_command())
        tmuxctl.wait_session(service)
    deadline = time.monotonic() + 8
    while True:
        try:
            if not matching_pids(service):
                raise RuntimeError('relay 进程尚未运行')
            read_status()
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError('relay 启动后管理接口未就绪，请查看日志') from None
            time.sleep(.2)


def stop(service):
    with maintenance_guard():
        pids = matching_pids(service)
        if not pids:
            raise RuntimeError('未找到可核对身份的本机 relay 进程，未发送停止')
        value = read_status()
        if (value.get('enabled') is not False or value.get('bound') is not False
                or value.get('forwarding_enabled') is not False
                or value.get('action_port_released') is not True
                or value.get('recovery_required') is not False or value.get('error')):
            raise RuntimeError('relay 仍在使用或退出状态未核清，请先由大脑完成交接')
        # Use the existing process matcher, never kill an unrelated tmux session.
        if tmuxctl.pane_pid(service.window) in pids:
            tmuxctl.stop_session(service)
        else:
            tmuxctl.stop_unmanaged(service)
        if matching_pids(service):
            raise RuntimeError('relay 进程尚未退出，未确认停止')


def logs(service, lines):
    from ops.services import latest_log_file, tail_file, display_log_path
    from shared.brain_journal import latest_named_log
    path = None
    try:
        for pid in matching_pids(service):
            output = Path(f'/proc/{pid}/fd/1').resolve()
            if output.is_file() and output.suffix == '.log':
                path = output
    except (OSError, ValueError, RuntimeError):
        pass
    path = path or latest_log_file(service)
    monitor = latest_named_log('relay', 'monitor.log')
    if monitor and monitor != path:
        if not path:
            return tail_file(monitor, lines), display_log_path(monitor), None
        process_lines = max(1, lines // 2)
        text = ('【relay 进程输出】\n' + tail_file(path, process_lines)
                + '\n\n【面板监测：状态变化】\n' + tail_file(monitor, max(1, lines - process_lines)))
        return text, display_log_path(path) + ' | ' + display_log_path(monitor), None
    if path:
        return tail_file(path, lines), display_log_path(path), None
    return '暂无 relay 日志；管理状态见上方卡片。', None, None
