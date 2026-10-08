"""Local environment switch transaction. Never starts/stops remote robot services."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

import yaml

from shared.execution_profile import ROOT, DEFAULT, resolve


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}


_KERNEL_TOKEN = r'(?:true|false|True|False|yes|no|Yes|No|on|off|~|null)'


def reception_motion_permitted(profile):
    route = (profile.get('routes') or {}).get('reception') or {}
    return route.get('available') is True and route.get('backend') == 'reception_real'


def render_kernel_enabled(text, enabled):
    """Flip only reception_real.kernel_enabled. Leave every other line as written."""
    flag = 'true' if enabled else 'false'
    pattern = re.compile(rf'(kernel_enabled[ \t]*:[ \t]*){_KERNEL_TOKEN}')
    if pattern.search(text):
        return pattern.sub(rf'\g<1>{flag}', text, count=1)
    block = re.compile(r'(?m)^(reception_real[ \t]*:[ \t]*(?:#.*)?\n)')
    if block.search(text):
        return block.sub(rf'\g<1>  kernel_enabled: {flag}\n', text, count=1)
    flow = re.compile(r'(reception_real[ \t]*:[ \t]*\{)')
    if flow.search(text):
        return flow.sub(rf'\g<1>kernel_enabled: {flag}, ', text, count=1)
    suffix = '' if text.endswith('\n') or not text else '\n'
    return text + suffix + f'reception_real:\n  kernel_enabled: {flag}\n'


def assert_idle(status):
    state = status.get('state')
    if (status.get('active') or status.get('blocks_new_motion') or status.get('command_unknown')
            or status.get('resources_cleared') is False
            or (state and str(state).lower() not in {'succeeded', 'failed', 'cancelled',
                                                      'completed_hand_state_only'})):
        raise ValueError(f'任务尚未核清，不能切换环境：{state or "active"}')


class LocalServices:
    """Use existing tmux management. Queries are read-only; no task is published."""
    def __init__(self):
        from ops.control import _start, _stop, _brain_state
        from ops.services import by_id
        self.start = lambda name: _start(by_id(name))
        self.stop = lambda name: _stop(by_id(name))
        self.running = lambda name: _brain_state(by_id(name)) != 'stopped'

    def status(self):
        return self.get('http://127.0.0.1:5000/api/task_status')

    @staticmethod
    def get(url):
        import urllib.request
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.load(response)

    def targets_healthy(self, routes):
        for route in routes.values():
            if route['available'] and route['backend'] == 'reception_protocol':
                from shared.protocol_simulation import require_simulator_pair
                require_simulator_pair(route['endpoints'], self.get)
            if route['available'] and route['mode'] == 'simulation' and route['url']:
                endpoint = '/health' if route['backend'] in {'desk', 'simple_o7'} else '/camera/status'
                if route['backend'] == 'simple_o7':
                    from urllib.request import build_opener, ProxyHandler
                    with build_opener(ProxyHandler({})).open(route['url'] + endpoint, timeout=3) as response:
                        result = json.load(response)
                else:
                    result = self.get(route['url'] + endpoint)
                if route['backend'] == 'simple_o7' and (
                        result.get('contract_version') != 'connection/simple-o7/v1' or result.get('ready') is not True):
                    raise ValueError('SIMPLE O7 远端服务未就绪或合同不匹配')

    def healthy(self, names, revision=None, timeout=90):
        from ops.control import _service_status
        from ops.services import by_id, latest_log_file, tail_file
        pending = list(names)
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            for name in list(pending):
                try:
                    if not _service_status(by_id(name))['health'].get('ok'):
                        continue
                    if name == 'slaver':
                        path = latest_log_file(by_id(name))
                        if not path or 'connection success' not in tail_file(path, 120):
                            continue
                    if revision and name in {'master', 'deploy'}:
                        port = 5000 if name == 'master' else 8888
                        if self.get(f'http://127.0.0.1:{port}/api/execution_profile').get('revision') != revision:
                            continue
                    pending.remove(name)
                except Exception:
                    pass
            if pending:
                time.sleep(.5)
        if pending:
            raise RuntimeError('服务未就绪或版本未生效：' + ', '.join(pending))


class Switcher:
    def __init__(self, root=ROOT, services=None):
        self.root = Path(root)
        self.state = self.root / 'data' / 'system' / 'execution.json'
        self.draft = self.root / 'config' / 'execution.yaml'
        self.block = self.root / 'data' / 'system' / 'execution.blocked'
        self.result = self.root / 'data' / 'system' / 'execution-apply.json'
        self.services = services

    def status(self):
        applied = json.loads(self.state.read_text()) if self.state.exists() else None
        draft = read_yaml(self.draft) if self.draft.exists() else (applied or {}).get('config', DEFAULT)
        last = json.loads(self.result.read_text()) if self.result.exists() else None
        self.state.parent.mkdir(parents=True, exist_ok=True)
        with (self.state.parent / 'execution.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                switching = False
            except BlockingIOError:
                switching = True
        return {'switching': switching, 'draft': draft, 'applied': applied, 'last_apply': last,
                'blocked': self.block.exists(),
                'real_motion_enabled': (read_yaml(self.root / 'config/brain.yaml').get('reception_real') or {}).get('kernel_enabled') is True}

    def preview(self, config):
        return resolve(config, read_yaml(self.root / 'config/robot_api.yaml'))

    def ledgers_idle(self):
        config = read_yaml(self.root / 'config/brain.yaml')
        if (config.get('brain') or {}).get('scheduler', 'runtime') != 'runtime':
            raise ValueError('统一环境切换要求 brain.scheduler=runtime')
        real = config.get('reception_real') or {}
        for key, default, filename in [('kernel_runtime_dir', 'data/tasks', 'current_task.json')]:
            directory = Path(real.get(key) or default)
            if not directory.is_absolute():
                directory = self.root / directory
            path = directory / filename
            if path.exists():
                record = json.loads(path.read_text())
                if not isinstance(record, dict):
                    raise ValueError(f'无法核对任务账本：{path}')
                assert_idle(record)

    def apply(self, config):
        profile = self.preview(config)  # Validate before touching services or files.
        profile['revision'] = uuid.uuid4().hex
        self.state.parent.mkdir(parents=True, exist_ok=True)
        with (self.state.parent / 'execution.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('已有任务正在提交或环境切换正在执行，请稍后重试') from None
            return self._apply_locked(profile)

    def _apply_locked(self, profile):
        services = self.services or LocalServices()
        self.ledgers_idle()
        for name in ['master', 'slaver']:
            config_path = self.root / 'config' / ('brain.yaml' if name == 'master' else 'slaver.yaml')
            if config_path.exists() and (read_yaml(config_path).get('collaborator') or {}).get('clear'):
                raise ValueError(f'{name} 配置会在启动时清空 Redis；先将 collaborator.clear 设为 false')
        if services.running('master'):
            assert_idle(services.status())
        previous = self.state.read_text() if self.state.exists() else None
        original = [n for n in ['master', 'slaver'] if services.running(n)]
        desired = list(dict.fromkeys(original + ['master']))
        routes = profile['routes']
        execution = routes['execution']
        if execution['available'] and execution['backend'] not in {'desk', 'simple_o7'} and 'slaver' not in desired:
            desired.append('slaver')
        if 'slaver' in desired and not services.running('redis'):
            raise ValueError('Slaver 路径需要先启动 Redis；环境切换不重启或清空 Redis')
        needed = { {'desk': 'desk', 'mujoco': 'mujoco', 'mujoco_3dgs': 'gs'}[r['backend']]
                   for r in routes.values() if r['available'] and r['mode'] == 'simulation'
                   and r['backend'] in {'desk', 'mujoco', 'mujoco_3dgs'} }
        if routes['reception']['available'] and routes['reception']['backend'] == 'reception_protocol':
            needed.update({'reception_nav', 'reception_vla'})
        started_dependencies = []
        brain_backup = None
        try:
            atomic_write(self.block, 'Environment application in progress.\n')
            atomic_write(self.result, json.dumps({'state': 'applying', 'revision': profile['revision']}))
            # Stops ingress first; the admission lock fences concurrent submissions/continues.
            for name in original:
                services.stop(name)
            self.ledgers_idle()  # Also catches tasks accepted by a pre-upgrade process.
            for name in sorted(needed):
                if not services.running(name):
                    started_dependencies.append(name)
                    services.start(name)
            services.healthy(sorted(needed))
            services.targets_healthy(routes)
            brain_backup = self._sync_motion_permit(profile)
            atomic_write(self.state, json.dumps(profile, ensure_ascii=False, indent=2))
            for name in ['slaver', 'master']:
                if name in desired:
                    services.start(name)
            services.healthy(desired, revision=profile['revision'])
            atomic_write(self.draft, yaml.safe_dump(profile['config'], allow_unicode=True, sort_keys=False))
            self.block.unlink(missing_ok=True)
            atomic_write(self.result, json.dumps({'state': 'succeeded', 'revision': profile['revision']}, ensure_ascii=False))
            return profile
        except Exception as exc:
            # Never leave mixed generations accepting tasks after a failed switch.
            atomic_write(self.block, 'Environment application failed; recovery in progress.\n')
            rollback_error = None
            try:
                for name in ['master', 'slaver']:
                    if name in desired:
                        services.stop(name)
                if previous is None:
                    self.state.unlink(missing_ok=True)
                else:
                    atomic_write(self.state, previous)
                if brain_backup is not None:
                    atomic_write(self.root / 'config' / 'brain.yaml', brain_backup)
                for name in started_dependencies:
                    services.stop(name)
                for name in ['slaver', 'master']:
                    if name in original:
                        services.start(name)
                services.healthy(original, revision=json.loads(previous)['revision'] if previous else None)
                self.block.unlink(missing_ok=True)
            except Exception as rollback:
                rollback_error = str(rollback)
            atomic_write(self.result, json.dumps({'state': 'failed', 'error': str(exc),
                         'rollback_error': rollback_error}, ensure_ascii=False))
            raise RuntimeError(f'切换失败：{exc}；' + (f'回退未完成：{rollback_error}，已阻止新任务' if rollback_error else '已恢复原运行配置')) from exc

    def _sync_motion_permit(self, profile):
        """Open the real-motion permit exactly when reception is the real route."""
        path = self.root / 'config' / 'brain.yaml'
        if not path.exists():
            raise ValueError('缺少 config/brain.yaml，不能同步真机动作许可')
        original = path.read_text(encoding='utf-8')
        before = yaml.safe_load(original) or {}
        if not isinstance(before, dict):
            raise ValueError('config/brain.yaml 无法读取')
        real = before.get('reception_real') or {}
        if not isinstance(real, dict):
            raise ValueError('config/brain.yaml 的 reception_real 必须是映射')
        enabled = reception_motion_permitted(profile)
        if (real.get('kernel_enabled') is True) == enabled:
            return None
        updated = render_kernel_enabled(original, enabled)
        after = yaml.safe_load(updated) or {}
        if not isinstance(after, dict):
            raise ValueError('真机动作许可写入后配置无法读取')
        expected = dict(before)
        expected_real = dict(real)
        expected_real['kernel_enabled'] = enabled
        expected['reception_real'] = expected_real
        if after != expected:
            raise ValueError('真机动作许可写入改变了其它配置')
        atomic_write(path, updated)
        return original
