"""Read-only capture/VLM adapter for the existing capture_scene result contract."""
import json
import time
import base64
from contracts.look import cameras_for
from brain.adapters.vlm import _endpoint_cfg, _api_key, _vlm_cfg
from shared.vision_options import generation_options, client_options


def capture_scene(context='', camera_name='', *, brain_config=None):
    from execution.robot_api.client import capture_image
    from openai import OpenAI
    from brain.adapters.vla_camera import real_camera_route, capture_head
    route = real_camera_route()
    cameras = ['ego_view'] if route else ([camera_name] if camera_name else cameras_for(context)[:2])
    content = [{'type': 'text', 'text': '根据当前画面如实描述视野中的物体，不编造看不见的东西，不控制机器人。用户问：' + context}]
    captured = []
    metadata = None
    for camera in cameras:
        if route:
            if camera_name and camera_name not in {'head_cam', 'ego_view', 'head'}:
                raise ValueError('真机现场描述只支持头部 ego_view，不使用其他视角代替')
            metadata, raw = capture_head(config=brain_config, route=route)
            result = {'success': True, 'image': base64.b64encode(raw).decode('ascii')}
        else:
            result = capture_image(camera_name=camera)
        if result.get('success') and result.get('image'):
            captured.append(camera)
            content.extend([{'type': 'text', 'text': '[' + camera + ']'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + result['image']}}])
    if not captured:
        raise RuntimeError('截图失败：所有相机均不可用')
    endpoint, config = _endpoint_cfg(), _vlm_cfg()
    key = 'local' if endpoint.get('auth_required', True) is False else _api_key(endpoint['key_names'])
    client = OpenAI(api_key=key, base_url=endpoint['api_base'],
                    **client_options(endpoint['api_base'], timeout=config.get('timeout_sec', 60)))
    options = generation_options(config, max_tokens=1000)
    url = str(endpoint['api_base']).rstrip('/') + '/chat/completions'
    started = time.perf_counter()
    try:
        result = client.chat.completions.create(model=endpoint['model'], messages=[{'role': 'user', 'content': content}],
            **options)
    except Exception as exc:
        _log_capture(url, endpoint['model'], context, started, error=exc)
        raise
    text = (result.choices[0].message.content or '').strip()
    _log_capture(url, endpoint['model'], context, started, status=200, result=text)
    if not text:
        raise RuntimeError('VLM 返回空描述')
    status = {'_status': 'success'}
    if metadata is not None:
        status['camera_frame'] = metadata
    return json.dumps(['视野描述（' + '、'.join(captured) + '）：' + text, status], ensure_ascii=False)


def _log_capture(url, model, context, started, **fields):
    try:
        from shared.log_setup import log_forward
        log_forward('画面描述（capture）', 'POST', url, model=model,
                    duration_s=time.perf_counter() - started,
                    request_text=context or '画面描述（image）', **fields)
    except Exception:
        pass
