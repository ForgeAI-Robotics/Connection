"""Journaled NAV / independent gateway HTTP operations. No relay execution path."""
from copy import deepcopy
import math
import re
import time
import uuid
from urllib.parse import quote

from contracts.body_control import (VERSION, REVISION, PERMIT_MODE, GATEWAY_BINDING, ControlError,
                                    fresh, digest, encode, stamp, nav_prepare_refused)
from brain.adapters.http_client import HttpClient, HttpContractError
from shared.service_credentials import service_token


class ControlDriver:
    def __init__(self, config, *, clients=None):
        real = config['reception_real']
        self.settings = real['body_control']
        self.timeout = float(self.settings.get('operation_timeout_sec', 10))
        self.poll = float(self.settings.get('poll_interval_sec', .05))
        self.max_age = float(self.settings.get('evidence_max_age_sec', 10))
        self.request_timeout = float(self.settings.get('gateway_timeout_sec', 2))
        if not (0 < self.timeout <= 60 and 0 < self.poll <= self.timeout
                and 0 < self.max_age <= 60 and 0 < self.request_timeout <= 10):
            raise ValueError('Invalid bounded control timeout / evidence age')
        if clients is None:
            url = self.settings['gateway_base_url']
            if not url.startswith('https://') or not self.settings.get('gateway_ca_file'):
                raise ValueError('Gateway requires HTTPS and gateway_ca_file; TLS verification cannot be disabled')
            clients = dict(nav=HttpClient(real['dream_base_url'], timeout_sec=float(real.get('request_timeout_sec', 10))),
                vla=HttpClient(real['vla_base_url'], timeout_sec=float(real.get('request_timeout_sec', 10))),
                gateway=HttpClient(url, timeout_sec=self.request_timeout, ca_file=self.settings['gateway_ca_file']))
        self.clients = clients
        for service, client in self.clients.items():
            if isinstance(client, HttpClient):
                client.auth_token_env = self.token_env(service)

    def token_env(self, service):
        if service == 'gateway':
            return self.settings.get('gateway_token_env', 'BRAIN_GATEWAY_TOKEN')
        return self.settings.get(service + '_token_env') or self.settings.get('token_env', 'BRAIN_CONTROL_TOKEN')

    def _call(self, service, method, path, payload=None, *, record=None):
        try:
            token = service_token(self.token_env(service))
        except ValueError as exc:
            raise ControlError('CONTROL_AUTH_UNCONFIGURED', str(exc), 503) from None
        if not token:
            raise ControlError('CONTROL_AUTH_UNCONFIGURED', '控制服务凭据未配置', 503)
        try:
            body, status, _ = self.clients[service].request_json(method, path, payload, accepted_statuses=(200, 202),
                headers={'Authorization': 'Bearer ' + token, 'Cache-Control': 'no-cache'})
        except HttpContractError as exc:
            if record:
                record(dict(method=method, path=path, status_code=exc.status_code,
                            payload=deepcopy(exc.payload), message=str(exc), received_at=stamp()))
            raise
        if record:
            record(dict(method=method, path=path, status_code=status, payload=deepcopy(body), received_at=stamp()))
        return body

    def _fresh(self, value, *, robot_required=True):
        if not isinstance(value, dict) or not fresh(value.get('observed_at') or value.get('updated_at'), self.max_age):
            raise ControlError('STALE_EVIDENCE', '本端控制状态缺少新鲜观测时间')
        if (robot_required or 'robot_serial' in value) and value.get('robot_serial') != self.settings['robot_serial']:
            raise ControlError('IDENTITY_MISMATCH', '控制状态的机器人身份不匹配')
        return value

    def status(self, service):
        if service == 'gateway':
            observation = uuid.uuid4().hex
            started = time.monotonic()
            value = self._call(service, 'GET', '/v1/body-input/status?observation_id=' + observation)
            value['_request_elapsed_ms'] = (time.monotonic() - started) * 1000
            if (value.get('contract_version') != VERSION or value.get('coordination_revision') != REVISION
                    or not isinstance(value.get('gateway_instance_id'), str)
                    or value.get('permit_mode') != PERMIT_MODE or value.get('renew_required') is not False
                    or value.get('observation_id') != observation):
                raise ControlError('GATEWAY_CONTRACT_MISMATCH', '网关版本或实例不匹配')
            return value
        return self._fresh(self._call('nav', 'GET', '/v1/status?observation_id=' + uuid.uuid4().hex), robot_required=False)

    def native_facts(self, status, *, isolated=False, require_hold=True, vla=False):
        native = status.get('native')
        if not vla and 'not_required' not in status.get('supported_hand_modes', []):
            raise ControlError('HAND_POLICY_MISMATCH', '网关未支持本次身体独立控制')
        if not vla and status.get('hand_policy') != 'body_only':
            from brain.adapters.vla_control import capabilities
            capabilities(status)  # An explicitly extended gateway may still serve body-only NAV.
        if not isinstance(native, dict) or status.get('observation_error'):
            raise ControlError('NATIVE_UNAVAILABLE', 'SONIC 当前证据未就绪：' +
                               str(status.get('observation_error') or 'native missing'), details=status)
        timing = deepcopy(status.get('freshness'))
        if vla and isinstance(timing, dict):
            for key in ('sample_age_ms', 'body_sample_age_ms'):
                if type(timing.get(key)) in (int, float):
                    timing[key] += status.get('_request_elapsed_ms', 0)
        self.freshness(timing, gateway_instance_id=status['gateway_instance_id'])
        generated = status['freshness'].get('generated_monotonic_ns')
        observed = native.get('observed_monotonic')
        if (type(generated) is not int or type(observed) not in (int, float)
                or not 0 <= generated / 1e9 - observed <= .15):
            raise ControlError('STALE_EVIDENCE', 'native 时间与当前观测不一致')
        if not vla and (native.get('hand_participation') != 'not_required'
                or native.get('old_hand_task_pending') is not False):
            raise ControlError('OLD_HAND_TASK_UNRESOLVED', '手部未参与声明或旧手部退出尚未核清')
        if not vla and any(k not in native or native[k] is not None for k in
               ('o6_hold_verified', 'o6_output_fenced', 'hold_reference_id')):
            raise ControlError('HAND_POLICY_MISMATCH', '未参与的手部事实必须明确为 null')
        if require_hold or isolated:
            self.require(native, 'body_hold_verified')
            if vla and native.get('safety_override') is not False:
                raise ControlError('BODY_RUNTIME_UNCONFIRMED', '身体安全状态未核清')
        else:
            # Current permission may be observed during motion; stationarity
            # belongs at command admission and exit.
            sample = native.get('sample') or {}
            low_state = sample.get('low_state_monotonic_ns')
            if (sample.get('safety_override') is not False
                    or sample.get('policy_output_selected') is not True
                    or type(low_state) is not int
                    or not 0 <= status['freshness']['generated_monotonic_ns'] - low_state <= 100_000_000):
                raise ControlError('BODY_RUNTIME_UNCONFIRMED', '身体安全、控制输出或实时反馈未核清')
        if isolated:
            self.require(native, 'receiver_isolated', 'body_stopped')
        if not isinstance(native.get('controller_instance_id'), str) or not native['controller_instance_id']:
            raise ControlError('IDENTITY_MISMATCH', '缺少真实 SONIC 实例')
        return native

    def idle(self, pending=None):
        nav, gateway = self.status('nav'), self.status('gateway')
        if 'active_command_id' not in nav or nav['active_command_id'] not in ('', None):
            raise ControlError('SOURCE_NOT_RELEASED', 'NAV 仍有活动命令或状态未知')
        if gateway.get('permit') is not None or 'permit' not in gateway:
            raise ControlError('SOURCE_NOT_RELEASED', '网关许可尚未退出')
        self.native_facts(gateway, isolated=True)
        return dict(nav=nav, gateway=gateway)

    @staticmethod
    def gateway_receipt(value, payload):
        if (not isinstance(value, dict) or value.get('contract_version') != VERSION
                or value.get('coordination_revision') != REVISION
                or value.get('gateway_instance_id') != payload['gateway_instance_id']
                or value.get('request_id') != payload['request_id']
                or value.get('operation') != payload['operation']
                or encode(value.get('identity')) != encode(payload)):
            raise ControlError('IDENTITY_MISMATCH', '网关回执未精确关联原请求')
        if value.get('state') == 'completed':
            if value.get('accepted') is not True or value.get('effect_status') != 'confirmed':
                raise ControlError('INVALID_RECEIPT', '网关 completed 缺少效果确认')
            evidence = value.get('evidence') or {}
            op = payload['operation']
            if op in ('grant', 'allow_execute'):
                ControlDriver.require(evidence, 'permit_installed')
                permit = evidence.get('permit') or {}
                for k in (*GATEWAY_BINDING, 'phase', 'permit_seq', 'hand_mode', 'hold_reference_id'):
                    if encode(permit.get(k)) != encode(payload[k]):
                        raise ControlError('IDENTITY_MISMATCH', '网关确认的授权与请求不符: ' + k)
                until = permit.get('valid_until_monotonic')
                if ('valid_until_monotonic' not in permit or until is not None
                        or permit.get('permit_mode') != PERMIT_MODE):
                    raise ControlError('INVALID_RECEIPT', '网关未确认持续授权')
            elif op == 'revoke':
                ControlDriver.require(evidence, 'permit_revoked')
        return value

    def verify_current_permit(self, row, status, phase, *, require_hold=False):
        permit = status.get('permit') or {}
        binding = row['gateway_binding']
        if status.get('gateway_instance_id') != binding['gateway_instance_id']:
            raise ControlError('GATEWAY_INSTANCE_CHANGED', '网关实例改变，旧执行权不可恢复')
        if any(encode(permit.get(k)) != encode(binding[k]) for k in GATEWAY_BINDING):
            raise ControlError('STALE_AUTHORIZATION', '网关当前许可绑定不匹配')
        vla = row['target'] == 'vla'
        hand_mode = ('execute' if phase == 'execute' else 'hold') if vla else 'not_required'
        if (permit.get('phase') != phase or permit.get('permit_mode') != PERMIT_MODE
                or 'valid_until_monotonic' not in permit
                or permit.get('valid_until_monotonic') is not None or permit.get('hand_mode') != hand_mode
                or 'hold_reference_id' not in permit or permit['hold_reference_id'] != binding.get('hold_reference_id')):
            raise ControlError('PERMIT_NOT_CURRENT', '网关当前阶段或持续会话模式不符')
        native = self.native_facts(status, require_hold=require_hold, vla=vla)
        if vla:
            from brain.adapters.vla_control import hands, capabilities
            capabilities(status)
            hands(status, binding, phase)
        if native['controller_instance_id'] != binding['controller_instance_id']:
            raise ControlError('CONTROLLER_INSTANCE_CHANGED', 'SONIC 实例已变化')
        if (phase == 'execute' or vla) and native.get('active_session') != binding['input_session_id']:
            raise ControlError('STATIC_ACTIVATION_UNCONFIRMED', 'SONIC 未采用本会话')
        return permit

    def verify_exit(self, row, fence, status, source=None):
        binding = row.get('gateway_binding') or {}
        # Current exit is a fresh independent observation, never a rewrite of
        # an original unknown fence/retain receipt.
        facts = status.get('exit_status') or {}
        identity = facts.get('identity') or {}
        if any(encode(identity.get(k)) != encode(binding.get(k)) for k in GATEWAY_BINDING):
            raise ControlError('IDENTITY_MISMATCH', '当前退出证明未关联原会话')
        self.require(facts, 'exit_confirmed', 'body_forwarding_fenced', 'receiver_callbacks_sealed',
                     'receiver_decoded_caches_cleared', 'input_session_retired', 'body_stopped', 'body_hold_verified')
        vla = row['target'] == 'vla'
        if not vla and (facts.get('hand_participation') != 'not_required'
                or any(k not in facts or facts[k] is not None for k in
                       ('o6_output_fenced', 'o6_hold_verified', 'hold_reference_id'))):
            raise ControlError('HAND_POLICY_MISMATCH', '当前退出证明的手部范围不符')
        native = self.native_facts(status, isolated=True, vla=vla)
        if vla:
            from brain.adapters.vla_control import hands
            self.require(facts, 'o6_output_fenced', 'o6_hold_verified')
            never_bound = facts.get('o6_never_bound') is True
            if never_bound and facts.get('o6_instance_id') != binding['o6_instance_id']:
                raise ControlError('O6_EXIT_UNCONFIRMED', '未绑定退出证明未关联原 O6 实例')
            held = hands(status, binding, exiting=True, never_bound=never_bound)
            if not held.get('hold_reference_id') or facts.get('hold_reference_id') != held['hold_reference_id']:
                raise ControlError('O6_EXIT_UNCONFIRMED', '当前手部保持参考与退出回执不一致')
        last = status.get('last_binding') or {}
        if (status.get('permit') is not None or 'permit' not in status
                or any(encode(last.get(k)) != encode(binding.get(k)) for k in GATEWAY_BINDING)):
            raise ControlError('EXIT_UNCONFIRMED', '当前网关／控制器或退出关联已变化')
        if status.get('gateway_instance_id') != binding.get('gateway_instance_id'):
            if status.get('installed_at_boot_without_permit') is not True:
                raise ControlError('EXIT_UNCONFIRMED', '新网关未证明启动时未恢复执行许可')
        if native['controller_instance_id'] != binding.get('controller_instance_id'):
            if (facts.get('confirmation_basis') != 'old_receiver_process_absent_and_current_hold'
                    or facts.get('old_controller_absent') is not True
                    or facts.get('current_controller_instance_id') != native['controller_instance_id']
                    or native.get('native_binding') != {} or native.get('active_session') is not None
                    or native.get('request_pending') is not False
                    or (native.get('sample') or {}).get('local_stop_pending') is not False):
                raise ControlError('EXIT_UNCONFIRMED', '缺少旧控制器已退出、新控制器无任务且保持正常的证明')
        # The source can remain unknown: retired session + cache isolation prevents late input.
        source_closed = True if source and source.get('output_fenced') is True else None
        return dict(exit_confirmed=True, hold_verified=True, source_output_fenced=source_closed,
                    **{row['target']: source}, gateway=fence, current_gateway=status,
                    hand_participation='required' if vla else 'not_required', hold_scope='body_and_hands' if vla else 'body',
                    scope='VLA body and hand exit' if vla else 'NAV body exit only; hands not participating')

    def wait_exit(self, row, fence, source=None):
        """Observe asynchronous native stop/hold completion without replaying effects."""
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                return self.verify_exit(row, fence, self.status('gateway'), source)
            except ControlError as exc:
                if exc.code not in {'EXECUTION_UNCONFIRMED','NATIVE_UNAVAILABLE','STALE_EVIDENCE','EXIT_UNCONFIRMED'}:
                    raise
                if time.monotonic() >= deadline:
                    raise
            except HttpContractError:
                # An observation timeout is not an exit failure or proof of
                # nonexecution. Retry only GET within the existing exit budget.
                if time.monotonic() >= deadline:
                    raise
            time.sleep(self.poll)

    @staticmethod
    def require(value, *keys):
        if not isinstance(value, dict) or any(value.get(key) is not True for key in keys):
            raise ControlError('EXECUTION_UNCONFIRMED', '尚未核清: ' + ', '.join(keys))

    @staticmethod
    def same_control(value, row):
        reference = value.get('control_authorization') or {}
        if encode(reference) != encode({key: row[key] for key in ('lease_id', 'authority_id', 'ownership_epoch')}):
            raise ControlError('STALE_AUTHORIZATION', '执行回执不属于原授权')
        if value.get('task_id') != row['task_id'] or value.get('command_id') != row['command_id']:
            raise ControlError('IDENTITY_MISMATCH', '执行回执不属于原命令')

    def payload(self, row, action):
        value = dict(contract_version=VERSION, coordination_revision=REVISION, permit_mode=PERMIT_MODE,
                     robot_serial=row['robot_serial'], control_resource=row['control_resource'],
                     action_endpoint=row['action_endpoint'], control_session_id=row['control_session_id'],
                     source_id=row['target'], control_authorization={k: row[k] for k in
                        ('lease_id', 'authority_id', 'ownership_epoch')})
        if action == 'yield':
            value['cancel_scope'] = 'control_session'
        return value

    @staticmethod
    def freshness(value, *, gateway_instance_id=None, body_only=False):
        if not isinstance(value, dict) or not fresh(value.get('nav_generated_at') or value.get('generated_at'), 10):
            raise ControlError('STALE_EVIDENCE', '缺少新鲜观测时间')
        if gateway_instance_id and value.get('gateway_instance_id') != gateway_instance_id:
            raise ControlError('IDENTITY_MISMATCH', '观测网关实例不符')
        checks = [('body_sample_age_ms', 'body_sample_fresh', 100)]
        if not body_only:
            checks.append(('sample_age_ms', 'sample_fresh', 150))
        for age, flag, limit in checks:
            if (type(value.get(age)) not in (int, float) or not math.isfinite(value[age])
                    or not 0 <= value[age] <= limit or value.get(flag) is not True):
                raise ControlError('STALE_EVIDENCE', '当前身体／接收观测已过期')

    @staticmethod
    def operation_receipt(value, payload, service=None):
        if service == 'vla':
            from brain.adapters.vla_control import receipt
            return receipt(value, payload)
        if not isinstance(value, dict):
            raise ControlError('INVALID_RECEIPT', '控制回执必须为对象')
        view = deepcopy(value)
        if (encode(view.get('identity')) != encode(payload)
                or any(view.get(k) != payload.get(k) for k in
                       ('request_id', 'control_session_id', 'contract_version', 'coordination_revision', 'permit_mode'))):
            raise ControlError('IDENTITY_MISMATCH', 'NAV 控制会话回执身份不匹配')
        if view.get('state') == 'completed':
            if (view.get('accepted') is not True or view.get('historical') is not False
                    or view.get('nav_instance_id') != view.get('query_nav_instance_id')
                    or not view.get('nav_instance_id')):
                raise ControlError('STALE_PARTICIPANT', 'NAV 完成回执不是当前进程证明')
            ControlDriver.freshness(view.get('freshness'))
            facts = view.get('evidence') or {}
            for key in ('navigation_stopped', 'output_fenced', 'hold_verified', 'navigation_not_started'):
                if key in facts:
                    view[key] = facts[key]
            view['mode_ready'] = facts.get('mode_confirmed')
            if view.get('action') == 'prepare' and view.get('current_prepare_valid') is not True:
                reasons = {
                    'PREPARE_PERMISSION_NOT_INSTALLED': '网关尚未接受准备授权',
                    'PREPARE_CONTROL_SESSION_MISMATCH': '网关当前授权与本次请求不一致',
                    'PREPARE_INPUT_SESSION_NOT_ACTIVE': '机器人尚未确认使用本次导航输入',
                    'PREPARE_BODY_HOLD_UNCONFIRMED': '机器人当前身体保持未确认',
                    'PREPARE_NATIVE_SAMPLE_STALE': '机器人状态数据过期',
                    'PREPARE_BODY_SAMPLE_STALE': '身体反馈数据过期',
                    'GATEWAY_OBSERVATION_UNAVAILABLE': '暂时无法读取网关状态',
                }
                blockers = view.get('current_prepare_blockers')
                blockers = blockers if isinstance(blockers, list) else []
                explanation = '；'.join(reasons.get(str(code), str(code)) for code in blockers)
                raise ControlError('STATIC_ACTIVATION_UNCONFIRMED',
                    '导航准备未通过当前状态检查，目标尚未下发' + ('：' + explanation if explanation else ''),
                    details=dict(request_id=payload.get('request_id'), service=service,
                        current_prepare_valid=view.get('current_prepare_valid'),
                        current_prepare_blockers=deepcopy(blockers),
                        current_prepare_observation=deepcopy(view.get('current_prepare_observation')),
                        original_receipt=deepcopy(value)))
        return view

    nav_prepare_refused = staticmethod(nav_prepare_refused)

    def exchange(self, wire, *, send, record=None, history=None, until_registered=False):
        """Never repeat a POST, including when it raised after doing the operation."""
        service, payload = wire['service'], wire['payload']
        observations = deepcopy(history or {})
        def observed(value):
            key = 'first_post' if value['method'] == 'POST' else 'last_query'
            if key == 'first_post':
                observations.setdefault(key, deepcopy(value))
            else:
                observations[key] = deepcopy(value)
            if record:
                record(value)
        def call(method, path, body=None):
            return self._call(service, method, path, body, record=observed)
        def failed(code, message):
            first = observations.get('first_post') or {}
            response = first.get('payload')
            rejected = (type(first.get('status_code')) is int and 400 <= first['status_code'] < 500
                and isinstance(response, dict) and response.get('accepted') is False
                and response.get('contract_version') == VERSION
                and response.get('coordination_revision') == REVISION)
            if first.get('status_code') is not None and first['status_code'] >= 400:
                raw_error = response.get('error') if isinstance(response, dict) else None
                reason = (f"{raw_error.get('code', '')}: {raw_error.get('message', '')}"
                          if isinstance(raw_error, dict) else first.get('message', 'HTTP 请求失败'))
                operation = payload.get('operation') or payload.get('action') or wire['path'].rstrip('/').rsplit('/', 1)[-1]
                message = (f"{service.upper()} {operation} POST HTTP {first['status_code']}: "
                           f"{reason}; {message}")
            elif first.get('message'):
                message = f"{service.upper()} 原 POST：{first['message']}; {message}"
            details = dict(service=service, request_id=payload.get('request_id'),
                request_outcome='rejected' if rejected else 'unknown',
                nav_prepare_not_accepted=self.nav_prepare_refused(wire, first), **observations)
            return ControlError('HANDOFF_REJECTED' if rejected else code, message, details=details)
        if self.nav_prepare_refused(wire, observations.get('first_post')):
            raise failed('HANDOFF_REJECTED', '原 NAV prepare 在受理前被明确拒绝，未重发')
        if wire.get('kind') == 'navigation_cancel':
            value = None
            if send:
                try:
                    value = call('POST', wire['path'], payload)
                except HttpContractError:
                    pass
            deadline = time.monotonic() + self.timeout
            while True:
                if value is not None:
                    if value.get('request_id') != payload['request_id'] or value.get('identity') != payload:
                        raise ControlError('IDENTITY_MISMATCH', '取消回执不属于原命令')
                    if value.get('state') == 'completed' and value.get('accepted') is True:
                        return value
                    if value.get('state') in ('rejected','unknown','blocked'):
                        raise failed('STOP_UNCONFIRMED', str(value.get('error') or value.get('blockers')))
                if time.monotonic() >= deadline:
                    raise failed('STOP_UNCONFIRMED', '只查原取消 request_id，不重复停止')
                time.sleep(self.poll)
                try:
                    value = call('GET', wire['query_path'])
                except HttpContractError:
                    value = None
        value = None
        if send:
            try:
                value = call('POST', wire['path'], payload)
            except HttpContractError:
                if self.nav_prepare_refused(wire, observations.get('first_post')):
                    raise failed('HANDOFF_REJECTED', 'NAV prepare 在受理前被明确拒绝')
                # A conflict, 5xx or lost reply may hide effects: query original.
        deadline = time.monotonic() + self.timeout
        while True:
            if value is not None:
                if service == 'vla' and payload.get('action') == 'stop' and 'stop_requests' in value:
                    value = (value['stop_requests'] or {}).get(payload['request_id'])
                    if value is None:
                        if time.monotonic() >= deadline:
                            raise failed('STOP_UNCONFIRMED', '原停止请求没有可核查回执')
                        time.sleep(self.poll)
                        value = call('GET', wire['query_path'])
                        continue
                if service == 'gateway':
                    value = self.gateway_receipt(value, payload)
                nav_receipt = service == 'nav'
                if service != 'gateway':
                    value = self.operation_receipt(value, payload, service)
                if value.get('state') in {'failed', 'rejected', 'unconfirmed', 'unknown', 'blocked'}:
                    raise failed('HANDOFF_UNCONFIRMED', str(value.get('error') or value.get('blockers') or value['state']))
                if value.get('state') != 'pending' and (
                        value.get('completed') is True or value.get('state') in {'succeeded', 'completed'}):
                    if service == 'nav':
                        self._fresh(value, robot_required=not nav_receipt)
                    return value
                if until_registered and value.get('state') == 'pending' and value.get('registration'):
                    publisher = value['registration']
                    if (not isinstance(publisher.get('publisher_instance_id'), str)
                            or not re.fullmatch('[0-9a-f]{32}', str(publisher.get('input_session_id', '')))):
                        raise ControlError('INVALID_SESSION', 'NAV 登记身份无效')
                    return value
            if time.monotonic() >= deadline:
                raise failed('HANDOFF_UNCONFIRMED', '查询原交接请求超时；没有重新发送')
            time.sleep(self.poll)
            try:
                value = call('GET', wire['query_path'])
            except HttpContractError:
                value = None

    def command_hold(self, row, raw):
        body = row['request']['body']
        if (raw.get('task_id') != row['task_id'] or raw.get('command_id') != row['command_id']
                or raw.get('control_session_id') != row['control_session_id']
                or raw.get('command_seq') != row['command_seq']
                or raw.get('control_authorization') != {k:row[k] for k in ('lease_id','authority_id','ownership_epoch')}
                or raw.get('state') not in ('succeeded','failed','cancelled')
                or any(raw.get(k) != body.get(k) for k in ('contract_version','target_id','route_phase','leg_index'))):
            raise ControlError('IDENTITY_MISMATCH', '命令结束回执不属于本段')
        result, proof = raw.get('result') or {}, raw.get('current_hold') or {}
        expected = {k: row[k] for k in ('control_session_id','task_id','command_id','command_seq')}
        if proof.get('identity') != expected or proof.get('applicable') is not True:
            raise ControlError('COMMAND_STOP_UNCONFIRMED', '当前保持证明缺失或已被新命令取代')
        self.require(proof, 'command_stopped', 'motion_held', 'body_hold_verified', 'control_session_retained')
        if proof.get('session_released') is not False or proof.get('permission_state') != 'installed':
            raise ControlError('CONTROL_STATE_UNKNOWN', '当前控制会话未确认保留')
        for key in ('input_session_id','publisher_instance_id','controller_instance_id'):
            if proof.get(key) != row['gateway_binding'][key]:
                raise ControlError('IDENTITY_MISMATCH', '保持证明发布或接收实例不符')
        self.freshness(proof.get('freshness'), body_only=True)
        return deepcopy(proof)

    def navigation_finished(self, row):
        raw = self._call('nav', 'GET', '/v1/commands/' + quote(row['command_id'], safe=''))
        return self.command_hold(row, raw)

    def cancel_navigation(self, row, exchange):
        return exchange('nav-cancel', 'nav', '/v1/navigation/cancel',
                        dict(contract_version='fq/reception-lan/v1', coordination_revision=REVISION, permit_mode=PERMIT_MODE,
                             control_session_id=row['control_session_id'], control_authorization={k:row[k] for k in
                             ('lease_id','authority_id','ownership_epoch')}, task_id=row['task_id'],
                             command_id=row['command_id'], command_seq=row['command_seq'], cancel_scope='command'),
                        '/v1/navigation/handoff/requests/{request_id}', 'navigation_cancel')

    def yield_navigation(self, row, exchange):
        result = exchange('nav-yield', 'nav', '/v1/navigation/handoff/yield', self.payload(row, 'yield'),
                          '/v1/navigation/handoff/requests/{request_id}')
        self.require(result, 'navigation_stopped', 'output_fenced', 'hold_verified')
        return result

    def yield_vla(self, row, exchange):
        result = exchange('vla-yield', 'vla', '/v1/vla/handoff/yield', self.payload(row, 'yield'),
                          '/v1/vla/handoff/requests/{request_id}')
        self.require(result.get('evidence'), 'policy_stopped', 'publisher_stopped', 'output_fenced', 'hold_verified')
        return result

    def vla_business(self, row, prepare_request_id):
        """Collect business evidence once, before persisting the exact prepare/task body."""
        from datetime import datetime, timedelta
        from brain.adapters.vla_control import worksite, WORKSITE_SCHEMA
        from contracts.body_control import digest
        body = deepcopy(row['request']['body'])
        from contracts.vla import machine_id
        if (body.get('operation') != row['request']['skill']
                or body.get('contract_version') != 'fq/reception-lan/v1'
                or any(not machine_id(body.get(k)) for k in ('task_id','command_id','object_id','target_area'))
                or set(body) - {'contract_version','task_id','command_id','operation','object_id','target_area',
                                'navigation_proof','worksite_evidence'}):
            raise ControlError('INVALID_REQUEST', '操控请求字段或动作身份无效')
        status = self.status('gateway')
        controller = (status.get('native') or {}).get('controller_instance_id')
        proof = body.get('worksite_evidence')
        acknowledgment = row.get('worksite_confirmation')
        if proof is None and acknowledgment:
            if acknowledgment.get('target_area') != body['target_area']:
                raise ControlError('WORKSITE_IDENTITY_MISMATCH', '现场准备确认不属于当前作业点')
            observed = acknowledgment['observed_at']
            proof = dict(deepcopy(acknowledgment), schema=WORKSITE_SCHEMA, kind='operator_ready',
                **{k:body[k] for k in ('task_id','command_id','operation','object_id','target_area')},
                pose_reference=acknowledgment['reference'],
                valid_until=(datetime.fromisoformat(observed.replace('Z','+00:00'))+timedelta(seconds=300)).isoformat())
        if proof is None:
            from contracts.vla import validate_request
            validate_request(body)
            reference = body['navigation_proof']
            command = self._call('nav', 'GET', '/v1/commands/' + quote(reference['dream_command_id'], safe=''))
            nav = self.status('nav')
            result, pose = command.get('result') or {}, nav.get('proof_context') or {}
            if (command.get('contract_version') != body['contract_version']
                    or command.get('task_id') != body['task_id']
                    or command.get('command_id') != reference['dream_command_id']
                    or command.get('target_id') != body['target_area'] or command.get('state') != 'succeeded'
                    or any(result.get(k) is not True for k in ('success', 'reached', 'navigation_stopped'))
                    or nav.get('controller_instance_id') != controller or nav.get('robot_serial') != row['robot_serial']
                    or pose.get('valid') is not True or pose.get('pose_within_tolerance') is not True
                    or pose.get('dream_command_id') != command['command_id']
                    or pose.get('target_id') != body['target_area']
                    or not all(isinstance(pose.get(k), str) and pose[k] for k in
                               ('map_id', 'localization_epoch', 'navigation_epoch'))):
                raise ControlError('WORKSITE_NOT_READY', '作业点的原导航结果或当前位姿未核清')
            observed = nav['observed_at']
            try:
                completed = datetime.fromisoformat(command['completed_at'].replace('Z', '+00:00'))
                valid_time = completed.tzinfo is not None and completed <= datetime.fromisoformat(observed.replace('Z', '+00:00'))
            except (KeyError, TypeError, ValueError, AttributeError):
                valid_time = False
            if not valid_time:
                raise ControlError('WORKSITE_NOT_READY', '原导航完成时间未核清')
            until = datetime.fromisoformat(observed.replace('Z', '+00:00')) + timedelta(seconds=60)
            proof = dict(schema=WORKSITE_SCHEMA, kind='navigation_arrival', ready=True, pose_verified=True,
                **{k: body[k] for k in ('task_id','command_id','operation','object_id','target_area')},
                robot_serial=row['robot_serial'], controller_instance_id=controller,
                reference='brain-nav:'+digest(dict(command=command, observation=nav)),
                pose_reference='nav-pose:'+digest(nav), navigation_command_id=command['command_id'],
                navigation_result_reference='nav-result:'+digest(command),
                observed_at=observed, valid_until=until.isoformat())
            evidence = dict(command=command, observation=nav)
        else:
            evidence = {'worksite_evidence': deepcopy(proof)}
        body['worksite_evidence'] = worksite(proof, body, row['robot_serial'], controller, row['control_session_id'])
        body.pop('navigation_proof', None)
        body['control'] = dict(coordination_revision=REVISION, permit_mode=PERMIT_MODE,
            control_session_id=row['control_session_id'], control_authorization={k:row[k] for k in
                ('lease_id', 'authority_id', 'ownership_epoch')}, command_seq=1, prepare_request_id=prepare_request_id)
        return body, evidence


from brain.adapters.execution import BodyAdapter, interpret_observation


class ControlledBodyAdapter(BodyAdapter):
    """Existing Runtime body port; each command checks only its selected source."""
    requires_authoritative_handoff = True
    def __init__(self, dream, vla, authority):
        super().__init__(dream, vla)
        self.authority = authority
        for service, client in (('nav', dream), ('vla', vla)):
            if isinstance(getattr(client, 'http', None), HttpClient):
                client.http.auth_token_env = authority.driver.token_env(service)

    @property
    def gate_open(self):
        # MotionGate owns the configured enable switch. Remote admission belongs
        # to submit, where the command's source is known.
        return True

    def capabilities(self):
        # Planning metadata, not a live all-source readiness check.
        return dict(operations={k: {'configured': True} for k in ('open_fridge', 'grasp_cola')},
                    real_action_enabled=True, supported_sources=['navigation', 'vla'],
                    readiness_checked_at_dispatch=True)

    def submit(self, command_id, request):
        if request.get('skill') not in {'navigate', 'open_fridge', 'grasp_cola'}:
            raise ControlError('UNSUPPORTED_CONTROL_SKILL', '当前网关未接入该动作')
        body = deepcopy(request['body'])
        try:
            if request['skill'] != 'navigate':
                caps = self.vla.capabilities() or {}
                if (caps.get('real_action_enabled') is not True
                        or (caps.get('operations', {}).get(request['skill']) or {}).get('configured') is not True):
                    raise ControlError('VLA_UNAVAILABLE', '当前操控端未启用本动作')
            self.authority.prepare_command(request)
            body = self.authority.business_body(request)
            self.authority.record_submission(request, body)
            raw = self.dream.submit_navigation(body) if request['skill'] == 'navigate' else self.vla.submit_task(body)
            return dict(accepted=True, unclear=False, completed=False, command_id=command_id, raw=raw)
        except (ControlError, HttpContractError, ValueError) as exc:
            raw = exc.response() if isinstance(exc, ControlError) else getattr(exc, 'payload', None)
            return dict(accepted=False, unclear=True, command_id=command_id, error=str(exc), raw=raw)

    def _view(self, command_id, raw, request):
        released = self.authority.released(request['body']['task_id'], command_id)
        viewed = interpret_observation(command_id, raw, request, control_released=released)
        viewed['body_control_released'] = released
        viewed['resources_released'] = released
        facts = viewed.get('evidence') or {}
        if request.get('skill') != 'navigate':
            try:
                row = self.authority.command_row(request['body']['task_id'], command_id)
                self.authority.driver.same_control(raw, row)
                if raw.get('control_session_id') != row['control_session_id'] or raw.get('command_seq') != 1:
                    raise ControlError('IDENTITY_MISMATCH', '操控回执不属于本次会话')
                if (viewed.get('terminal') in {'succeeded', 'failed', 'cancelled'}
                        and facts.get('identity_ok') is True and facts.get('time_ok') is True
                        and viewed.get('stopped') is True and not released):
                    self.authority.release_control(request['body']['task_id'], command_id)
                    return self._view(command_id, raw, request)
            except (ControlError, HttpContractError) as exc:
                viewed.update(resources_released=False, error=str(exc))
                if getattr(exc, 'code', '') in {'IDENTITY_MISMATCH', 'STALE_AUTHORIZATION'}:
                    facts.update(identity_ok=False, supports=False)
            return viewed
        if (viewed.get('terminal') in {'succeeded','failed','cancelled'}
                and facts.get('identity_ok') is True and facts.get('time_ok') is True and not released):
            try:
                self.authority.settle_navigation(request['body']['task_id'], command_id, raw)
                # Runtime step resource means the command's execution slot;
                # the physical control session explicitly remains occupied.
                viewed.update(stopped=True, resources_released=True, control_session_retained=True)
                facts.update(resource_scope='command', body_control_released=False, control_session_retained=True)
            except (ControlError, HttpContractError) as exc:
                viewed.update(resources_released=False, error=str(exc))
        return viewed

    def query(self, command_id, request):
        if self.authority.business_not_submitted_after_cleanup(request['body']['task_id'], command_id):
            from contracts.submission import submission_refusal, refusal_observation
            raw = {'success': False, 'error': {'code': 'SAFETY_NOT_READY',
                'message': '原控制准备已核清退出；账本确认业务命令未提交，可由人工继续创建新尝试',
                'details': {'task_id': request['body']['task_id'], 'command_id': command_id,
                            'action_started': False, 'action_state_uncertain': False,
                            'source': 'brain_control_cleanup_before_business_submission'}}}
            receipt = submission_refusal(409, raw, command_id=command_id, task_id=request['body']['task_id'])
            return refusal_observation(receipt)
        base = super().query(command_id, request)
        return self._view(command_id, base.get('raw'), request) if base.get('raw') else base

    def wait(self, command_id, request):
        base = super().wait(command_id, request)
        return self._view(command_id, base['raw'], request) if base.get('raw') else base

    def effect_control(self, action, command_id, request, **kwargs):
        row = self.authority.command_row(request['body']['task_id'], command_id)
        raw = self.vla.task(command_id)
        self.authority.driver.same_control(raw, row)
        if raw.get('control_session_id') != row['control_session_id'] or raw.get('command_seq') != 1:
            raise ControlError('IDENTITY_MISMATCH', '效果确认未关联原操控会话')
        value = super().effect_control(action, command_id, request, **kwargs)
        if action == 'query':
            return interpret_observation(command_id, value['raw'], request,
                control_released=self.authority.released(request['body']['task_id'], command_id))
        return self._view(command_id, value['raw'], request)

    def cancel(self, command_id, request):
        try:
            if request.get('skill') != 'navigate':
                result = self.authority.release_control(request['body']['task_id'], command_id)
                return dict(accepted=True, completed=result.get('exit_confirmed') is True, raw=result)
            result = self.authority.cancel_navigation(request['body']['task_id'], command_id)
            return dict(accepted=True, completed=result.get('state') == 'completed', raw=result)
        except (ControlError, HttpContractError) as exc:
            return dict(accepted=True, completed=False, error=str(exc))

    def handoff_with_context(self, context):
        command, request = context.get('source_command_id'), context.get('source_request')
        answer = dict(available=True, confirmed=False, kind=context['kind'], task_id=context['task_id'],
                      source_command_id=command, reason='source_unavailable')
        if not command or not request:
            return answer
        source = self.query(command, request)
        answer['source'] = source
        evidence = source.get('evidence') or {}
        if (evidence.get('identity_ok') is not True or evidence.get('time_ok') is not True
                or source.get('stopped') is not True
                or (source.get('terminal') != 'succeeded' and not context.get('manual_skips'))):
            return answer
        try:
            if request.get('skill') == 'navigate':
                self.authority.settle_navigation(context['task_id'], command)
                if context['kind'] == 'to_vla':
                    self.authority.release_control(context['task_id'], command)
                answer.update(confirmed=True, reason='source_command_held')
            else:
                self.authority.release_control(context['task_id'], command)
                answer.update(confirmed=True, reason='source_exit_confirmed')
        except (ControlError, HttpContractError) as exc:
            answer.update(reason='handoff_unconfirmed', error=str(exc))
        return answer
