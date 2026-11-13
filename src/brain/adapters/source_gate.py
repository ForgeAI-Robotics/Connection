"""Direct NAV business calls with the SONIC source-selection management API."""
from urllib.parse import quote

from brain.adapters.http_client import HttpClient, HttpContractError
from brain.adapters.execution import BodyAdapter, interpret_observation, _unconfirmed
from contracts.body_control import ControlError
from contracts.submission import submission_refusal, refusal_observation
from contracts.vla import NATIVE_OPERATIONS, validate_evidence, manual_holding_assumed, visual_arm_grasp

VERSION = 'g1/source-gate/v1'


def source_gate_enabled(config):
    control = (config.get('reception_real') or {}).get('body_control') or {}
    return control.get('enabled') is True and control.get('mode', 'unified') == 'source_gate'


class SourceGateClient:
    def __init__(self, settings):
        if not settings['gateway_base_url'].startswith('https://') or not settings.get('gateway_ca_file'):
            raise ValueError('Source gate requires HTTPS and the trusted gateway certificate')
        self.http = HttpClient(settings['gateway_base_url'],
            timeout_sec=float(settings.get('gateway_timeout_sec', 2)), ca_file=settings['gateway_ca_file'])
        self.http.auth_token_env = settings.get('gateway_token_env', 'BRAIN_GATEWAY_TOKEN')

    def call(self, method, path, payload=None):
        value = self.http.request_json(method, path, payload, accepted_statuses=(200, 202))[0]
        if value.get('contract_version') != VERSION:
            raise ControlError('SOURCE_GATE_CONTRACT', '接收源开关合同不匹配')
        return value

    def current(self):
        value = self.call('GET', '/v1/input-source')
        if (not isinstance(value.get('receiver_instance_id'), str) or not value['receiver_instance_id']
                or value.get('effective_source') not in {'navigation', 'vla', 'none'}
                or type(value.get('revision')) is not int or value['revision'] < 0
                or type(value.get('pending_requests')) is not int or value['pending_requests'] < 0
                or value.get('data_forwarding') is not False):
            raise ControlError('SOURCE_GATE_STATE', 'SONIC 当前选源状态无效')
        return value

    def set(self, payload):
        return self.call('PUT', '/v1/input-source', payload)

    def query(self, request_id):
        return self.call('GET', '/v1/input-source/requests/' + quote(request_id, safe=''))


def not_submitted(request, message):
    body = request['body']
    raw = {'success': False, 'error': {'code': 'SAFETY_NOT_READY', 'message': message,
        'details': {'task_id': body['task_id'], 'command_id': body['command_id'],
                    'action_started': False, 'action_state_uncertain': False,
                    'source': 'brain_source_gate_before_business_submission'}}}
    return submission_refusal(409, raw, command_id=body['command_id'], task_id=body['task_id'])


class SourceGateNavigationAdapter(BodyAdapter):
    """NAV only. No prepare, permits, body checks or real VLA fallback."""
    requires_authoritative_handoff = False

    def __init__(self, dream, authority):
        super().__init__(dream, None)
        self.authority = authority
        if isinstance(getattr(dream, 'http', None), HttpClient):
            dream.http.auth_token_env = authority.settings.get('nav_token_env') or authority.settings.get('token_env', 'BRAIN_CONTROL_TOKEN')

    @property
    def gate_open(self):
        return True  # The configured MotionGate remains the real-dispatch switch.

    def capabilities(self):
        return {'operations': {}, 'real_action_enabled': False, 'supported_sources': ['navigation']}

    def submit(self, command_id, request):
        try:
            raw = self._dispatch(request)
            return dict(accepted=True, unclear=False, completed=False, command_id=command_id, raw=raw)
        except (ControlError, HttpContractError, ValueError) as exc:
            refusal = None
            if not self.authority.was_submitted(request):
                refusal = not_submitted(request, str(exc))
            elif isinstance(exc, HttpContractError):
                refusal = submission_refusal(exc.status_code, exc.payload, command_id=command_id,
                                             task_id=request['body']['task_id'])
            return dict(accepted=False, unclear=refusal is None, completed=False, command_id=command_id,
                        error=str(exc), raw=exc.response() if isinstance(exc, ControlError) else getattr(exc, 'payload', None),
                        submission_rejected=refusal)

    def _dispatch(self, request):
        return self.authority.submit_navigation(self.dream, request)

    def query(self, command_id, request):
        if not self.authority.was_submitted(request):
            return refusal_observation(not_submitted(request, '原导航业务未提交；选源结果与导航业务分别记录'))
        return super().query(command_id, request)

    def cancel(self, command_id, request):
        try:
            raw = self.authority.cancel_navigation(self.dream, request)
            return dict(accepted=True, completed=raw.get('stopped') is True, command_id=command_id, raw=raw)
        except (ControlError, HttpContractError) as exc:
            return dict(accepted=True, completed=False, command_id=command_id, error=str(exc))


class SourceGateBodyAdapter(SourceGateNavigationAdapter):
    """Direct NAV/VLA business I/O; executor readiness stays with each executor."""
    requires_authoritative_handoff = True
    supports_body_only_manual_skip = True

    def __init__(self, dream, vla, authority):
        super().__init__(dream, authority)
        self.vla = vla
        if isinstance(getattr(vla, 'http', None), HttpClient):
            vla.http.auth_token_env = authority.settings.get('vla_token_env') or authority.settings.get('token_env', 'BRAIN_CONTROL_TOKEN')

    def capabilities(self):
        # Implemented business vocabulary, not a remote hardware preflight.
        from contracts.vla_direct import FINISH_REVISION
        return dict(operations={op: {'configured': True} for op in NATIVE_OPERATIONS},
                    real_action_enabled=True, execution_mode='direct',
                    finish_confirmation_revision=FINISH_REVISION,
                    readiness_checked_by_executor=True)

    def _dispatch(self, request):
        if request['skill'] == 'navigate':
            if request.get('required_holding'):
                self._current_holding(request)
            return self.authority.submit_navigation(self.dream, request)
        if request['skill'] not in NATIVE_OPERATIONS:
            raise ControlError('UNSUPPORTED_CONTROL_SKILL', '直连操控只接入已登记的开门、抓可乐和关门')
        return self.authority.submit_vla(self.vla, request)

    @staticmethod
    def _view(command_id, raw, request):
        return interpret_observation(command_id, raw, request, vla_execution_mode='direct')

    def query(self, command_id, request):
        if request['skill'] == 'navigate':
            return super().query(command_id, request)
        if not self.authority.was_submitted(request):
            return refusal_observation(not_submitted(request, '原操控业务未提交；查询不会补发动作'))
        try:
            return self._view(command_id, self.vla.task(command_id), request)
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)

    def wait(self, command_id, request):
        if request['skill'] == 'navigate':
            return super().wait(command_id, request)
        try:
            raw = self.vla.wait_task(command_id, timeout_sec=float(request.get('deadline_sec') or 30),
                poll_interval_sec=float(request.get('poll_interval_sec') or .5),
                stop_states={'running', 'waiting_result_confirmation'})
            return self._view(command_id, raw, request)
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)

    def effect_control(self, action, command_id, request, *, evidence=None, authorization=''):
        body = request['body']
        raw = self.vla.task(command_id)
        if self._view(command_id, raw, request)['evidence'].get('identity_ok') is not True:
            raise ValueError('VLA 回执不属于本次 direct 操控命令')
        if action == 'query':
            return self._view(command_id, raw, request)
        self.authority.check_effect(request)
        if action == 'pause':
            raw = self.vla.pause(body['task_id'], command_id)
        elif action == 'finish':
            if raw.get('state') not in {'running', 'waiting_result_confirmation'}:
                raise ValueError('须等待 VLA 执行中或已暂停后才能提交效果证据')
            window = raw.get('execution_ready_at') if raw['state'] == 'running' else raw.get('verification_ready_at')
            validate_evidence(body['operation'], body['object_id'], evidence, window)
            raw = self.vla.finish(body['task_id'], command_id, evidence, authorization)
        else:
            raise ValueError('未知 VLA 核验操作')
        return self._view(command_id, raw, request)

    def wait_completion(self, command_id, request):
        """Poll the original task using the existing client; never re-send finish."""
        try:
            raw = self.vla.wait_task(command_id, timeout_sec=float(request.get('deadline_sec') or 30),
                                    poll_interval_sec=float(request.get('poll_interval_sec') or .5))
            return self._view(command_id, raw, request)
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)

    def cancel(self, command_id, request):
        if request['skill'] == 'navigate':
            return super().cancel(command_id, request)
        try:
            raw = self.authority.cancel_vla(self.vla, request)
            return dict(accepted=True, completed=raw.get('stopped') is True, command_id=command_id, raw=raw)
        except (ControlError, HttpContractError) as exc:
            return dict(accepted=True, completed=False, command_id=command_id, error=str(exc))

    def handoff_with_context(self, context):
        command, request = context.get('source_command_id'), context.get('source_request')
        answer = dict(available=True, confirmed=False, kind=context['kind'], reason='source_unavailable')
        if not command or not request:
            return answer
        source = self.query(command, request)
        if (context.get('manual_skips') and context['kind'] == 'to_nav'
                and request.get('skill') in NATIVE_OPERATIONS):
            # Manual disposition replaces the business/hand outcome. Navigation
            # needs the current body publishing path, not an old O6 exit receipt.
            return self._current_body_path(dict(answer, source=source),
                                           reason='manual_skip_current_body_path_clear')
        reason = self._source_handoff_blocker(source)
        if context.get('manual_skips') and reason in {'source_failed', 'source_cancelled'}:
            reason = None
        answer.update(source=source, confirmed=reason is None,
                      reason=reason or 'source_business_finished_and_stopped')
        # Source selection happens once in the next command's dispatch path.
        return answer

    def retry_ready(self, command_id, request, original):
        """Retry lifecycle boundary only; VLA owns current O6/model/camera admission."""
        answer = dict(available=True, confirmed=False, kind='retry_vla', source=original)
        if (request.get('skill') not in NATIVE_OPERATIONS or original.get('command_id') != command_id
                or original.get('terminal') not in {'failed', 'cancelled'} or original.get('timed_out')):
            return dict(answer, reason='source_not_retryable')
        return self._current_body_path(answer, reason='vla_retry_current_body_path_clear')

    def _current_body_path(self, answer, *, reason):
        answer['scope'] = 'current_body_publishing_path'
        source = answer['source']
        evidence = source.get('evidence') or {}
        if evidence.get('identity_ok') is not True or evidence.get('time_ok') is not True:
            return dict(answer, reason='source_receipt_invalid')
        if source.get('terminal') not in {'succeeded', 'failed', 'cancelled'}:
            return dict(answer, reason='source_command_not_terminal')
        try:
            current = self.vla.control_status()
        except HttpContractError as exc:
            return dict(answer, reason='current_body_status_unavailable', error=str(exc))
        if not isinstance(current, dict):
            return dict(answer, reason='current_body_status_invalid')
        answer['current_body_status'] = {k: current.get(k) for k in (
            'execution_mode', 'service_instance_id', 'status_generated_at',
            'active_command_id', 'policy_running', 'action_port_released')}
        instance = current.get('service_instance_id')
        if current.get('execution_mode') != 'direct' or not isinstance(instance, str) or not instance.strip():
            return dict(answer, reason='current_body_status_invalid')
        if 'active_command_id' not in current or current['active_command_id'] not in (None, ''):
            return dict(answer, reason='current_vla_command_active')
        if current.get('policy_running') is not False:
            return dict(answer, reason='current_vla_publisher_not_stopped')
        if current.get('action_port_released') is not True:
            return dict(answer, reason='current_body_port_unreleased')
        return dict(answer, confirmed=True, reason=reason)

    def _current_holding(self, request):
        if manual_holding_assumed(request):
            return  # Explicit human skip satisfies business holding; handoff/selection remain separate.
        # Consume the verified task effect for the current wrist/arm contract.
        # Legacy contracts below retain their current signed O6 holding check.
        task_id, obj = request['body']['task_id'], request['required_holding']
        rows = self.authority._read()['commands'].values()
        grasp = next((r['request'] for r in reversed(list(rows))
                      if r['request']['body']['task_id'] == task_id and r['request']['skill'] == 'grasp_cola'), None)
        if not grasp:
            raise ControlError('HOLDING_UNCONFIRMED', '本任务没有原抓取命令')
        original = self.query(grasp['body']['command_id'], grasp)
        if self._source_handoff_blocker(original) is not None:
            raise ControlError('HOLDING_UNCONFIRMED', '原抓取效果或停止结果未确认')
        result = (original.get('raw') or {}).get('result') or {}
        if visual_arm_grasp(result.get('evidence')):
            if (original.get('evidence') or {}).get('supports') is not True or result.get('holding') != obj:
                raise ControlError('HOLDING_UNCONFIRMED', '本任务双腕／双臂抓取效果尚未核验')
            return  # Verified task effect; no additional signed O6 holding gate before NAV.
        status = self.vla.control_status()
        proof = status.get('holding_evidence') or {}
        if (status.get('execution_mode') != 'direct' or status.get('current_holding') != obj
                or proof.get('valid') is not True or proof.get('holding') != obj
                or proof.get('task_id') != task_id or proof.get('command_id') != grasp['body']['command_id']
                or not proof.get('receiver_instance_id')):
            raise ControlError('HOLDING_UNCONFIRMED', 'VLA 未确认当前仍持有本任务物体')
        try:
            from contracts.vla_direct import effect_window
            validate_evidence('grasp_cola', obj, proof,
                              effect_window(original['raw'], original['raw']['result'].get('evidence')))
        except ValueError as exc:
            raise ControlError('HOLDING_UNCONFIRMED', str(exc)) from exc
