"""Read-only capture/VLM adapter for the existing capture_scene result contract."""
import json
from connection.contracts.look import cameras_for
from connection.brain.adapters.vlm import _endpoint_cfg, _api_key, _vlm_cfg


def capture_scene(context='', camera_name=''):
    from robot_api.client import capture_image
    from openai import OpenAI
    cameras = [camera_name] if camera_name else cameras_for(context)[:2]
    content = [{'type': 'text', 'text': '根据当前画面如实描述视野中的物体，不编造看不见的东西，不控制机器人。用户问：' + context}]
    captured = []
    for camera in cameras:
        result = capture_image(camera_name=camera)
        if result.get('success') and result.get('image'):
            captured.append(camera)
            content.extend([{'type': 'text', 'text': '[' + camera + ']'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + result['image']}}])
    if not captured:
        raise RuntimeError('截图失败：所有相机均不可用')
    endpoint, config = _endpoint_cfg(), _vlm_cfg()
    client = OpenAI(api_key=_api_key(endpoint['key_names']), base_url=endpoint['api_base'], timeout=60, max_retries=0)
    options = {'extra_body': config['extra_body']} if config.get('extra_body') else {}
    result = client.chat.completions.create(model=endpoint['model'], messages=[{'role': 'user', 'content': content}],
        max_tokens=config.get('max_tokens', 1000), temperature=0, **options)
    text = (result.choices[0].message.content or '').strip()
    if not text:
        raise RuntimeError('VLM 返回空描述')
    return json.dumps(['视野描述（' + '、'.join(captured) + '）：' + text, {'_status': 'success'}], ensure_ascii=False)
