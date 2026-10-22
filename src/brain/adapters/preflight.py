"""Read-only reception interface checks; never arm, dispatch or maintain a route."""
import math
from datetime import datetime

from brain.adapters.dream_client import DreamClient
from brain.adapters.http_client import HttpContractError
from brain.adapters.ports import select_backend
from brain.packages.registry import reception_catalog
from contracts.tasks import Rejected


def compare_navigation_contract(points, contract, version):
    blockers, warnings = [], []
    if not isinstance(contract, dict) or not contract:
        return [], ['导航未提供点位／能力声明，无法确认两端定义一致。']
    if contract.get('contract_version') != version:
        blockers.append('导航协议版本缺失或与大脑不同。')
    frame = contract.get('frame_id') or contract.get('map_frame')
    if frame != 'map':
        blockers.append('导航坐标系缺失或不是 map。')
    admission = contract.get('goal_admission') or {}
    if not isinstance(admission, dict):
        return blockers + ['导航目标准入声明格式无效。'], warnings
    independent = admission.get('strategy') == 'independent_map_goal'
    coordinates_optional = independent and admission.get('reviewed_coordinates_enforced') is False
    metadata_optional = independent and admission.get('route_metadata_required') is False
    legs = contract.get('legs')
    if not isinstance(legs, list):
        return blockers, warnings + ['导航未公布可比较的点位定义，点位一致性未核实。']
    for name, point in points.items():
        leg = next((item for item in legs if isinstance(item, dict)
                    and item.get('leg_index') == point['leg_index']), None)
        if not leg:
            (warnings if metadata_optional else blockers).append(f'{name}：导航未声明该段。')
            continue
        for field in ('target_id', 'route_phase', 'motion_mode'):
            if point[field] != leg.get(field):
                (warnings if metadata_optional else blockers).append(f'{name}：{field} 与导航声明不同。')
        if leg.get('require_final_orientation') is not True:
            (warnings if metadata_optional else blockers).append(f'{name}：最终朝向要求未对齐。')
        try:
            goal = leg['goal_xyt']
            if (len(goal) != 3 or any(isinstance(x, bool) or not isinstance(x, (int, float))
                                     or not math.isfinite(x) for x in goal)):
                raise ValueError('invalid pose')
            local = point['goal_xyt']
            yaw = math.atan2(math.sin(local[2] - goal[2]), math.cos(local[2] - goal[2]))
            different = any(abs(local[i] - goal[i]) > 1e-5 for i in (0, 1)) or abs(yaw) > 1e-5
        except (KeyError, TypeError, ValueError):
            different = True
        if different:
            (warnings if coordinates_optional else blockers).append(f'{name}：坐标或朝向与导航声明不同。')
    if coordinates_optional or metadata_optional:
        warnings.append('导航声明支持独立目标；非强制的参考点位／路段差异仅提示，业务流程仍由大脑维护。')
    return blockers, warnings


def reception_preflight(config, task, *, client_factory=DreamClient):
    report = {'ready': True, 'required': False, 'task_type': 'runtime', 'blockers': [],
              'warnings': [], 'checked': False, 'checked_at': datetime.now().astimezone().isoformat(),
              'scope': '只读接口与定义检查；不代表定位批准、运动许可或真实抓放能力。'}
    text = str(task).strip().rstrip('。！!，, ')
    try:
        sops, variants, skill = reception_catalog(config)
        chosen = next((sop for sop in (sops['reception'], *variants['reception'])
                       if text in sop.get('triggers', ())), None)
        if chosen is None:
            report['warnings'] = ['尚未绑定明确的接待 SOP；本次未执行导航预检。']
            return report
        backend = select_backend(config, 'reception')
        report.update(task_type='reception', execution_backend=backend, skill_sha256=skill.snapshot()['sha256'])
        if backend != 'reception_real' or chosen['id'].endswith('.ros_sim_manipulation'):
            report['warnings'] = ['本预检仅检查真机导航接口；仿真沿用原有实例与合同校验。']
            return report
        report['required'] = True
        settings = config.get('reception_real') or {}
        if not settings.get('dream_base_url'):
            raise Rejected('未配置导航接口地址。')
        client = client_factory(settings['dream_base_url'], request_timeout_sec=3,
                                contract_version=skill.definition['contract_version'])
        status = client.status()
        if not isinstance(status, dict) or status.get('interface_online') is not True:
            raise Rejected('导航未确认接口在线。')
        if status.get('contract_version') != skill.definition['contract_version']:
            report['blockers'].append('导航状态接口协议版本与大脑不同。')
        # Read the existing metadata endpoint directly, never invoke a planning/goal endpoint.
        graph = client.http.request_json('GET', '/total_scene_graph_latest.json')[0]
        contract = graph.get('agent_navigation_contract')
        blocked, warnings = compare_navigation_contract(skill.navigation_points, contract,
                                                        skill.definition['contract_version'])
        report['blockers'].extend(blocked)
        report['warnings'].extend(warnings)
        report['checked'] = bool(isinstance(contract, dict)
                                 and isinstance(contract.get('legs'), list) and contract['legs'])
        report['navigation_revision'] = {key: (contract if isinstance(contract, dict) else {}).get(key)
                                         for key in ('route_revision', 'lifecycle_revision')}
        report['motion_blockers'] = status.get('motion_blockers') or []
        if status.get('motion_ready') is not True:
            report['warnings'].append('导航当前未确认运动就绪；实际执行条件仍由导航判断。')
    except (Rejected, HttpContractError, ValueError, TypeError, KeyError) as exc:
        report['required'] = True
        report['blockers'].append(str(exc))
    report['ready'] = not report['blockers']
    return report
