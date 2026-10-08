"""Read models and existing entry services, owned by the brain application."""
from datetime import datetime, timedelta
import json
from pathlib import Path
from urllib.parse import quote
import requests
import yaml
from flask import g, jsonify, request, Response, send_from_directory
from shared.paths import workspace_root, data_path
from brain.api.media import observation_url
from brain.storage.tasks import KernelStore
from brain.kernel.memory import event_window_settings
from contracts.intent import classify_entry


def register_views(app, application):
    service = application.service
    root = workspace_root()
    from brain.learning.demo import DemoLearning
    demo = DemoLearning(root / 'data/business')
    application.extra_workers.append(demo)

    def raw_proxy(path, *, method='GET', payload=None):
        g.poll_diagnostic = {'reason': '大脑观察模块不可用，尚未发出下游请求'}
        try:
            url = observation_url() + path
            g.poll_diagnostic = {'target': url, 'reason': '下游观察服务返回异常'}
            response = requests.request(method, url, json=payload, timeout=30)
            return Response(response.content, status=response.status_code,
                            content_type=response.headers.get('content-type', 'application/octet-stream'))
        except Exception as exc:
            if g.poll_diagnostic.get('target'):
                g.poll_diagnostic['reason'] = '大脑访问下游观察服务失败'
            return jsonify({'error': str(exc), 'success': False}), 503

    def static_scene():
        value = service.config.get('profile') or {}
        path = data_path(value.get('path', 'scene/profile.yaml') if isinstance(value, dict) else value)
        source = yaml.safe_load(path.read_text()) if path.is_file() else {}
        return {item['name']: item for item in (source or {}).get('scene', [])}

    @app.post('/api/task_intent')
    def intent():
        task = (request.get_json(silent=True) or {}).get('task')
        if not isinstance(task, str) or not task.strip():
            return jsonify({'error': '缺少有效 task 字段', 'intent': 'chat'}), 400
        report = classify_entry(task.strip())
        if report.get('needs_llm_route'):
            try:
                from brain.adapters.intent import route_ambiguous_with_llm
                label = route_ambiguous_with_llm(task)
                report = classify_entry(task, llm_label=label)
                report['llm_route'] = label
            except Exception as exc:
                report['llm_route_error'] = str(exc)
        return jsonify(report)

    @app.post('/api/chat')
    @app.post('/api/chat/route')
    def chat():
        from brain.adapters.chat import DeepSeekChat
        text = (request.get_json(silent=True) or {}).get('task')
        if not isinstance(text, str) or not text.strip():
            return jsonify({'error': '缺少有效 task 字段'}), 400
        try:
            model = DeepSeekChat()
            answer = model._route(text) if request.path.endswith('/route') else model._reply(text)
            return jsonify({'answer': answer})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 503

    @app.get('/api/auto_tools')
    @app.post('/api/get_tool_config')
    def tools():
        from brain.api.tools import active_tools, configured_tools
        try:
            items = (configured_tools((request.get_json(silent=True) or {}).get('slaver_config'))
                     if request.method == 'POST' else active_tools(service))
            return jsonify({'success': True, 'data': items})
        except Exception as exc:
            return jsonify({'success': False, 'message': str(exc), 'data': []}), 400

    @app.get('/robot_status')
    def robots():
        state = application.status()
        return jsonify([{'robot_name': 'FQrobot', 'robot_state': state.get('state') or 'idle'}])

    @app.get('/api/scene_state')
    def scene():
        try:
            from execution.robot_api.client import get_scene
            data = static_scene()
            observed = get_scene()
            if isinstance(observed, dict):
                data.update(observed.get('objects') or {})
            return jsonify({'success': True, 'data': data, 'source': 'configured_scene_and_observation'})
        except Exception as exc:
            return jsonify({'success': False, 'data': {}, 'message': str(exc)})

    @app.get('/api/belief')
    def belief():
        g.poll_diagnostic = {'reason': '大脑场景认知接口配置不可用'}
        try:
            from execution.robot_api.config import load_robot_api_config
            config = load_robot_api_config()
            url = config.server_url.rstrip('/') + '/belief'
            g.poll_diagnostic = {'target': url, 'reason': '下游场景认知服务返回异常'}
            response = requests.get(url, timeout=5)
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            if isinstance(exc, (requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema,
                                requests.exceptions.InvalidURL)):
                g.poll_diagnostic['reason'] = '大脑场景认知接口的目标地址为空或格式无效，尚未发出下游请求'
            elif g.poll_diagnostic.get('target'):
                g.poll_diagnostic['reason'] = '大脑访问下游场景认知服务或解析其响应失败'
            return jsonify({'objects': {}, 'error': str(exc)}), 503

    @app.post('/api/update_scene')
    def update_scene():
        data = request.get_json(silent=True) or {}
        location, action, obj = (data.get(key) for key in ('location', 'action', 'object'))
        if not all(isinstance(value, str) and value for value in (location, action, obj)):
            return jsonify({'success': False, 'message': '需要 location, action, object 三个字段'}), 400
        if action not in {'add_object', 'remove_object'}:
            return jsonify({'success': False, 'message': '不支持的 action'}), 400
        if location not in static_scene():
            return jsonify({'success': False, 'message': f"位置 '{location}' 不存在"}), 404
        _, ttl = event_window_settings(service.config)
        observed = datetime.now().astimezone()
        observation = {'subject': f'location:{location}', 'value': {'action': action, 'object': obj},
            'source': 'human:' + (request.headers.get('X-FQ-Operator') or request.headers.get('X-FQ-Source') or 'api'),
            'observed_at': observed.isoformat(), 'valid_until': (observed + timedelta(seconds=ttl)).isoformat()}
        store = KernelStore(root / 'data/scene')
        store.append_event('manual_observation', **observation)
        if application.status().get('task_id'):
            def record():
                runtime = service.attach()
                runtime.record_scene(subject=observation['subject'], value=observation['value'],
                    source=observation['source'], valid_until=observation['valid_until'])
            application.owner.call(record, control=True)
        return jsonify({'success': True, 'message': f"{action} '{obj}' at '{location}'；已记录人工线索",
                        'data': observation, 'confirmed': False})

    @app.get('/api/sops')
    def registered_sops():
        from brain.packages.registry import SOPS, VARIANTS
        return jsonify({'success': True, 'packages': SOPS, 'variants': VARIANTS})

    @app.get('/api/sop')
    @app.get('/api/reception/sop')
    def sop():
        reception = '/reception/' in request.path
        path = root / 'config/business' / ('reception_sop.yaml' if reception else 'demo_sop.yaml')
        version = 'v1'
        if reception and path.with_name('reception_sop_v2.yaml').is_file():
            path = path.with_name('reception_sop_v2.yaml'); version = 'v2'
        try:
            payload = {'success': True, 'sop': yaml.safe_load(path.read_text())}
            if reception:
                payload['version'] = version
            return jsonify(payload)
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc)}), 500

    @app.post('/api/demo/parse')
    def demo_parse():
        video = request.files.get('video')
        if not video:
            return jsonify({'success': False, 'error': '没收到视频文件'}), 400
        try:
            return jsonify(demo.submit(video, request.form.get('task') or '会议接待补货'))
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 409

    @app.get('/api/demo/status')
    def demo_status():
        return jsonify(demo.status())

    @app.get('/api/demo/result')
    def demo_result():
        result = demo.result()
        return (jsonify({'success': True, **result}) if result else
                (jsonify({'success': False, 'error': '尚无结果'}), 404))

    @app.post('/api/demo/save')
    def demo_save():
        try:
            return jsonify(demo.save((request.get_json(silent=True) or {}).get('job_id')))
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 409

    @app.get('/api/media_source')
    def media_source():
        try:
            return jsonify({'available': True, 'url': observation_url()})
        except Exception as exc:
            return jsonify({'available': False, 'reason': str(exc)}), 503

    @app.get('/api/robot_status')
    def robot_scene():
        return raw_proxy('/scene')

    @app.get('/api/quad_latest')
    def quad():
        return raw_proxy('/camera/latest')

    @app.route('/api/record/<action>', methods=['GET', 'POST'])
    def record(action):
        if action not in {'start', 'stop', 'status'}:
            return jsonify({'error': 'unknown record action'}), 404
        if request.method != ('GET' if action == 'status' else 'POST'):
            return jsonify({'error': 'method not allowed'}), 405
        return raw_proxy('/record/' + action, method=request.method, payload=request.get_json(silent=True))

    @app.get('/api/record/download/<filename>')
    def download(filename):
        return raw_proxy('/record/download/' + quote(filename, safe=''))

    @app.get('/api/timeline')
    def timeline():
        return jsonify(application.media.timeline())

    @app.get('/task_timeline/<path:filename>')
    def frame(filename):
        return send_from_directory(str(application.media.root), filename)

    @app.get('/api/task_events')
    def task_events():
        from brain.service_support import runtime_dir
        task_id = request.args.get('task_id') or application.status().get('task_id')
        records = KernelStore(runtime_dir(service.config)).read_events()
        return jsonify({'task_id': task_id, 'events': [r for r in records if r.get('task_id') == task_id]})
