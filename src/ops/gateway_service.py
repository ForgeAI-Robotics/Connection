"""Read-only independent gateway monitoring; never grants or starts robot services."""
from brain.adapters.http_client import HttpClient
from contracts.body_control import VERSION, REVISION
from shared.config import load_config
from shared.paths import workspace_root


def health(service):
    url = ''
    try:
        config = load_config(workspace_root() / 'config/brain.yaml')
        settings = config['reception_real']['body_control']
        url = settings['gateway_base_url'].rstrip('/')
        if not url.startswith('https://') or not settings.get('gateway_ca_file'):
            raise ValueError('网关 HTTPS／受信任证书尚未配置')
        client = HttpClient(url, timeout_sec=.5, ca_file=settings['gateway_ca_file'])
        client.auth_token_env = settings.get('gateway_token_env', 'BRAIN_GATEWAY_TOKEN')
        value, _, _ = client.request_json('GET', '/v1/body-input/status')
        if (value.get('contract_version') != VERSION or value.get('coordination_revision') != REVISION
                or not value.get('gateway_instance_id')):
            raise ValueError('网关合同或实例不匹配')
        permit = value.get('permit')
        native = value.get('native') or {}
        phase = permit.get('phase') if permit else None
        detail = '网关在线 · ' + ({'prepare': '正在准备', 'execute': '已允许执行'}.get(phase,
            '授权状态待确认') if permit else '等待任务')
        if value.get('observation_error'):
            detail += ' · ' + str(value['observation_error'])
        elif value.get('hand_policy') != 'body_only' or native.get('hand_participation') != 'not_required':
            detail += ' · 身体独立模式尚未确认'
        elif native.get('old_hand_task_pending') is not False:
            detail += ' · 旧手部任务未退出'
        elif native.get('body_hold_verified') is not True:
            detail += ' · 身体保持未核清'
        return dict(ok=True, detail=detail, url=url+'/v1/body-input/status',
                    gateway_instance_id=value['gateway_instance_id'],
                    permit_phase=permit.get('phase') if permit else None,
                    physical_readiness_verified=False)
    except Exception as exc:
        return dict(ok=False, detail=str(exc), url=url+'/v1/body-input/status' if url else '')
