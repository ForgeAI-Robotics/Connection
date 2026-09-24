"""Project installed capabilities into the existing tools response."""
import ast
import json
from pathlib import Path
import requests
import yaml
from shared.paths import workspace_root


def configured_tools(value=None):
    path = Path(value or workspace_root() / 'config/slaver.yaml')
    config = yaml.safe_load(path.read_text())
    robot = config.get('robot') or {}
    if robot.get('call_type') != 'local':
        response = requests.post(robot['path'].rstrip('/') + '/mcp', timeout=5)
        if response.status_code >= 500:
            raise ValueError('MCP 服务不可达')
        return []
    source = path.parent / robot['path'] / 'skill.py'
    return extract_tools(source.read_text(), str(source))


def extract_tools(source, filename):
    result = []
    for node in ast.parse(source, filename=filename).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(isinstance(d, ast.Call) and getattr(d.func, 'attr', None) == 'tool' for d in node.decorator_list):
            continue
        result.append({'name': node.name, 'description': ast.get_docstring(node) or '',
            'parameters': [{'name': a.arg, 'type': ast.unparse(a.annotation) if a.annotation else 'any'}
                           for a in node.args.args if a.arg != 'self']})
    return result


def active_tools(service):
    from brain.adapters.ports import select_backend
    from brain.skills.catalog import CATALOG
    try:
        backend = select_backend(service.config, 'generic')
    except Exception:
        backend = 'unavailable'
    if backend.startswith('slaver:'):
        inputs = service.planning.inputs
        transport = inputs.transport()
        agents = transport.collaborator.read_all_agents_info()
        result = []
        for name, raw in agents.items():
            info = json.loads(raw)
            for item in info.get('robot_tool', []):
                func = item.get('function') or {}
                props = (func.get('parameters') or {}).get('properties') or {}
                result.append({'robot': name, 'name': func.get('name', ''), 'description': func.get('description', ''),
                    'parameters': [{'name': k, 'type': v.get('type', 'any')} for k, v in props.items()]})
        return result
    return [{'robot': 'FQrobot', 'name': item.skill_id, 'description': item.applies,
             'parameters': [], 'execution': item.execution, 'backend': backend} for item in CATALOG]
