"""Read-only identity check for a NAV/VLA simulation pair (local protocol or remote SIMPLE)."""
import json
from urllib.request import build_opener, ProxyHandler

from contracts.tasks import CONTRACT_VERSION
from shared.execution_profile import PROTOCOL_URLS


def require_simulator_pair(endpoints, get=None, kind='protocol'):
    """kind=protocol: local timed simulators; kind=simple_physics: remote SIMPLE episode service."""
    if kind == 'protocol' and endpoints != PROTOCOL_URLS:
        raise ValueError("协议模拟端点必须是已登记的本机服务")
    if kind not in {'protocol', 'simple_physics'} or set(endpoints) != {'dream', 'vla'}:
        raise ValueError("模拟端点类型无效")
    if get is None:
        def get(url):
            with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
                return json.load(response)
    identities = []
    for key, role in [('dream', 'nav'), ('vla', 'vla')]:
        body = get(endpoints[key] + '/health')
        sim = body.get('simulation') or {}
        if (body.get('contract_version') != CONTRACT_VERSION or body.get('status') != 'ok'
                or sim.get('kind') != kind or sim.get('role') != role or not sim.get('instance_id')):
            raise ValueError('协议模拟服务身份或版本不匹配：' + key)
        identities.append(sim['instance_id'])
    if identities[0] != identities[1]:
        raise ValueError('导航和操控模拟进程没有使用同一份持久化状态')
    return identities[0]
