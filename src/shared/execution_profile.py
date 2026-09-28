"""One applied execution profile per process. Draft YAML never hot-switches a task."""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from shared.paths import workspace_root
ROOT = workspace_root()
DRAFT_PATH = ROOT / 'config' / 'execution.yaml'
STATE_PATH = ROOT / 'data' / 'system' / 'execution.json'
LOCK_PATH = ROOT / 'data' / 'system' / 'execution.lock'
BLOCK_PATH = ROOT / 'data' / 'system' / 'execution.blocked'
SIM_BACKENDS = {'desk', 'mujoco', 'mujoco_3dgs', 'simple_o7'}
RECEPTION_BACKENDS = {'reception_mock', 'reception_protocol'}
PROTOCOL_URLS = {'dream': 'http://127.0.0.1:18001', 'vla': 'http://127.0.0.1:18091'}
DEFAULT = {'mode': 'simulation', 'simulation_backend': 'desk', 'modules': {}}


def normalize(value):
    if not isinstance(value, dict) or set(value) - {'mode', 'simulation_backend', 'modules'}:
        raise ValueError('运行配置只接受 mode、simulation_backend、modules')
    result = {**DEFAULT, **value}
    if result['mode'] not in {'simulation', 'real'}:
        raise ValueError('mode 必须是 simulation 或 real')
    if result['simulation_backend'] not in SIM_BACKENDS:
        raise ValueError('未知仿真后端')
    modules = result['modules']
    if not isinstance(modules, dict) or set(modules) - {'reception', 'execution', 'observation'}:
        raise ValueError('modules 只接受 reception、execution、observation')
    result['modules'] = {}
    for name in ('reception', 'execution', 'observation'):
        item = modules.get(name, {})
        if not isinstance(item, dict) or set(item) - {'mode', 'simulation_backend'}:
            raise ValueError(f'{name} 的配置项无效')
        mode = item.get('mode', 'inherit')
        backend = item.get('simulation_backend', 'inherit')
        if mode not in {'inherit', 'simulation', 'real', 'disabled'}:
            raise ValueError(f'{name}.mode 无效')
        choices = RECEPTION_BACKENDS if name == 'reception' else SIM_BACKENDS
        if backend not in choices | {'inherit'}:
            raise ValueError(f'{name}.simulation_backend 无效')
        result['modules'][name] = {'mode': mode, 'simulation_backend': backend}
    return result


def resolve(value, robot_config):
    config = normalize(value)
    routes = {}
    for name, item in config['modules'].items():
        mode = config['mode'] if item['mode'] == 'inherit' else item['mode']
        backend = config['simulation_backend'] if item['simulation_backend'] == 'inherit' else item['simulation_backend']
        route = {'mode': mode, 'backend': backend, 'available': True, 'reason': '', 'url': ''}
        if mode == 'disabled':
            route.update(backend='disabled', available=False, reason='此模块已停用')
        elif name == 'reception':
            route['backend'] = 'reception_real' if mode == 'real' else (
                item['simulation_backend'] if item['simulation_backend'] != 'inherit' else 'reception_mock')
            if route['backend'] == 'reception_protocol':
                route['endpoints'] = dict(PROTOCOL_URLS)
        elif mode == 'real':
            route.update(backend='unavailable', available=False,
                         reason=f'{name} 尚无已接入的完整真机适配；不会回落仿真')
        elif name == 'observation' and backend == 'desk':
            route.update(available=False, reason='Desk 不提供图像；可将观察模块显式指定为 3DGS 或 MuJoCo')
        elif name == 'observation' and backend == 'simple_o7':
            route.update(available=False, reason='SIMPLE O7 当前提供物理状态证据，尚未接入现场描述和网页相机')
        else:
            raw = (robot_config.get('backends') or {}).get(backend) or {}
            # Applied profiles explicitly enable the chosen simulation backend.
            if not raw.get('url'):
                raise ValueError(f'config/robot_api.yaml 缺少 {backend}.url')
            from urllib.parse import urlparse
            url = str(raw['url']).rstrip('/')
            parsed = urlparse(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f'{backend}.url 必须是无内嵌凭据的 HTTP 地址')
            route['url'] = url
        routes[name] = route
    return {'config': config, 'routes': routes}


@lru_cache(maxsize=1)
def applied_profile():
    path = Path(os.environ.get('FQ_EXECUTION_STATE', STATE_PATH))
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not value.get('revision') or not value.get('routes'):
        raise ValueError('已应用的运行配置损坏；拒绝回落旧路由')
    return value


@contextmanager
def admission(*, check_block=True):
    """Fence new tasks/continue against the whole service-switch transaction."""
    lock_path = Path(os.environ.get('FQ_EXECUTION_LOCK', LOCK_PATH))
    block_path = Path(os.environ.get('FQ_EXECUTION_BLOCK', BLOCK_PATH))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('运行环境正在切换，请稍后重试') from None
        try:
            if check_block and block_path.exists():
                raise ValueError('上次环境应用未完成，请先在面板重新应用配置')
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
