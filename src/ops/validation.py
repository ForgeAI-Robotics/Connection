"""Read-only validation for the selected execution dependencies."""
from pathlib import Path
from shared.config import load_config
from flask import jsonify, request
from shared.paths import workspace_root


def register(app):
    @app.post('/api/validate-config')
    def validate_config():
        from ops.execution import Switcher
        try:
            root = workspace_root()
            data = request.get_json(silent=True) or {}
            path = Path(data.get('master_config') or root / 'config/brain.yaml')
            config = load_config(path)
            if not isinstance(config, dict):
                raise ValueError('大脑配置不是 YAML 映射')
            status = Switcher(root).status()
            profile = status.get('applied') or Switcher(root).preview(status['draft'])
            route = profile['routes']['execution']
            requires_slaver = route['available'] and route['backend'] not in {'desk', 'unavailable', 'disabled'}
            if requires_slaver:
                other = load_config(data.get('slaver_config') or root / 'config/slaver.yaml')
                a, b = config.get('collaborator') or {}, other.get('collaborator') or {}
                if any(a.get(key) != b.get(key) for key in ('host', 'port', 'db', 'password')):
                    raise ValueError('大脑和 Slaver collaborator 配置不匹配')
                if a.get('clear') or b.get('clear'):
                    raise ValueError('collaborator.clear 必须为 false')
                import redis
                client = redis.Redis(**{k: a[k] for k in ('host', 'port', 'password', 'db') if k in a},
                                     socket_connect_timeout=3, socket_timeout=3)
                try:
                    client.ping()
                finally:
                    client.close()
            return jsonify({'success': True, 'message': '配置校验通过', 'requires_slaver': requires_slaver})
        except Exception as exc:
            return jsonify({'success': False, 'message': str(exc)}), 400
