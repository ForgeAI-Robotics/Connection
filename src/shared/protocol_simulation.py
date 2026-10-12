"""Read-only identity check for a NAV/VLA simulation pair (local protocol or remote SIMPLE)."""
import json
from urllib.request import build_opener, ProxyHandler

from contracts.tasks import CONTRACT_VERSION
from shared.execution_profile import PROTOCOL_URLS


def require_simulator_pair(endpoints, get=None, kind='protocol', navigation_legs=None, navigation_only=None):
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
        if navigation_legs is not None:
            from contracts.reception_lan import NAVIGATION_LEGS
            actual = sim.get('navigation_legs', NAVIGATION_LEGS)
            if (not isinstance(actual, dict) or
                    sorted(actual.values(), key=lambda leg: leg.get('leg_index', 0)) !=
                    sorted(navigation_legs.values(), key=lambda leg: leg['leg_index'])):
                raise ValueError('SIMPLE 导航路线与当前 Skill 不匹配，动作未下发；需部署对应仿真路线')
        if navigation_only is not None and sim.get('navigation_only', False) is not navigation_only:
            raise ValueError('SIMPLE 的仅导航／完整接待模式与所选 SOP 不一致，动作未下发')
        identities.append(sim['instance_id'])
    if identities[0] != identities[1]:
        raise ValueError('导航和操控模拟进程没有使用同一份持久化状态')
    return identities[0]
