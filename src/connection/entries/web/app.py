"""Independent browser entry: pages plus HTTP forwarding, no brain imports."""
import os
import uuid
from flask import Flask, request, render_template, jsonify, Response
from connection.clients.brain import BrainClient


ROUTES = {
    '/publish_task': ['POST'],
    '/api/publish_task': ['POST'], '/api/task_intent': ['POST'],
    '/api/task_preflight': ['POST'], '/api/task_status': ['GET'],
    '/api/task_pause': ['POST'], '/api/task_continue': ['POST'], '/api/task_cancel': ['POST'],
    '/api/success_pending': ['GET'], '/api/failure_pending': ['GET'],
    '/api/save_success_experience': ['POST'], '/api/save_failure_experience': ['POST'],
    '/api/save_experience': ['POST'], '/api/experiences': ['GET'], '/api/auto_tools': ['GET'],
    '/api/scene_state': ['GET'], '/api/belief': ['GET'], '/api/sop': ['GET'],
    '/api/demo/parse': ['POST'], '/api/demo/status': ['GET'], '/api/demo/result': ['GET'],
    '/api/demo/save': ['POST'], '/api/reception/run': ['POST'], '/api/reception/report': ['GET'],
    '/api/reception/sop': ['GET'], '/api/update_scene': ['POST'], '/api/get_tool_config': ['POST'],
    '/api/robot_status': ['GET'], '/api/record/start': ['POST'], '/api/record/stop': ['POST'],
    '/api/record/status': ['GET'], '/api/record/download/<filename>': ['GET'],
    '/api/quad_latest': ['GET'], '/api/timeline': ['GET'], '/task_timeline/<path:filename>': ['GET'],
    '/api/execution_profile': ['GET'], '/api/media_source': ['GET'], '/api/task_events': ['GET'],
}


def create_app(brain_url=None, *, client=None, ops_client=None):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024
    brain = client or BrainClient(brain_url or os.environ.get('MASTER_URL', 'http://127.0.0.1:5000'), timeout=90, source='web')
    ops = ops_client or BrainClient(os.environ.get('CONNECTION_OPS_URL', 'http://127.0.0.1:5678'), source='web')

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/teach')
    def teach():
        return render_template('teach.html')

    @app.get('/reception')
    def reception():
        return render_template('reception.html')

    def forward(**ignored):
        headers = {'X-FQ-Source': request.headers.get('X-FQ-Source') or 'web',
                   'X-FQ-Client': request.headers.get('X-FQ-Client') or request.remote_addr or '',
                   'X-FQ-Via': 'web'}
        if request.headers.get('X-FQ-Operator'):
            headers['X-FQ-Operator'] = request.headers['X-FQ-Operator']
        kwargs = {'headers': headers, 'params': request.args}
        if request.files:
            kwargs['files'] = [(key, (value.filename, value.stream, value.content_type))
                               for key, value in request.files.items(multi=True)]
            kwargs['data'] = request.form
        elif request.method == 'POST':
            payload = request.get_json(silent=True) or {}
            if request.path in {'/api/publish_task', '/publish_task'}:
                payload.setdefault('task_id', uuid.uuid4().hex)
                payload.setdefault('refresh', True)
            kwargs['json'] = payload
        try:
            response = (ops if request.path == '/api/validate-config' else brain).request(
                request.method, "/publish_task" if request.path == "/api/publish_task" else request.path, **kwargs)
            result = Response(response.content, status=response.status_code,
                              content_type=response.headers.get('content-type', 'application/json'))
            if response.headers.get('content-disposition'):
                result.headers['Content-Disposition'] = response.headers['content-disposition']
            return result
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc), 'unavailable': True}), 503

    for index, (path, methods) in enumerate({**ROUTES, '/api/validate-config': ['POST']}.items()):
        app.add_url_rule(path, 'forward_' + str(index), forward, methods=methods)
    return app
