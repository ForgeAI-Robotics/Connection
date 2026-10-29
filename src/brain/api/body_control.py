"""Small authenticated authority API, separate from the Runtime owner wait path."""
import hmac
from flask import jsonify, request
from contracts.body_control import ControlError
from shared.service_credentials import service_token


def register_body_control(app, application):
    def authority():
        control = getattr(application, 'body_control', None)
        if control is None:
            raise ControlError('HANDOFF_DISABLED', '大脑联合控制未启用', 501)
        if getattr(control, 'mode', '') == 'source_gate':
            raise ControlError('HANDOFF_DISABLED', '接收源开关模式不提供旧联合授权接口', 501)
        try:
            token = service_token(control.settings.get('token_env', 'BRAIN_CONTROL_TOKEN'))
        except ValueError as exc:
            raise ControlError('CONTROL_AUTH_UNCONFIGURED', str(exc), 503) from None
        if not token:
            raise ControlError('CONTROL_AUTH_UNCONFIGURED', '控制接口凭据未配置', 503)
        actual = request.headers.get('Authorization', '')
        if not hmac.compare_digest(actual.encode('utf-8'), ('Bearer ' + token).encode('utf-8')):
            raise ControlError('UNAUTHORIZED', '控制接口需要服务凭据', 401)
        application.start()
        return control

    def respond(call):
        try:
            value, status = call(authority())
        except ControlError as exc:
            value, status = exc.response(), exc.status
        except (ValueError, TypeError, KeyError):
            value, status = ControlError('INVALID_REQUEST', '控制参数无效', 400).response(), 400
        response = jsonify(value)
        response.headers['Cache-Control'] = 'no-store'
        return response, status

    @app.get('/v1/vla-handoff/capabilities')
    def control_capabilities():
        return respond(lambda control: (control.capabilities(), 200))

    @app.get('/v1/vla-handoff/leases/<lease_id>')
    def control_query(lease_id):
        def query(control):
            if (set(request.args) - {'request_id', 'observation_id'}
                    or any(len(request.args.getlist(k)) != 1 for k in request.args)):
                raise ControlError('INVALID_QUERY', '查询参数重复或无效', 400)
            return control.query(lease_id, request_id=request.args.get('request_id'),
                                 observation_id=request.args.get('observation_id')), 200
        return respond(query)

    @app.post('/v1/vla-handoff/leases/<lease_id>/<action>')
    def control_mutate(lease_id, action):
        def mutate(control):
            if action not in {'acquire', 'release'}:
                raise ControlError('UNKNOWN_OPERATION', '未知交接动作', 404)
            if len(request.get_data()) > 65536:
                raise ControlError('INVALID_REQUEST', '控制请求过大', 413)
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or body.get('lease_id') != lease_id or body.get('action') != action:
                raise ControlError('IDENTITY_MISMATCH', '路径与请求身份不匹配', 400)
            value = control.submit(body)
            return value, 202 if value['state'] == 'pending' else 200
        return respond(mutate)
