"""HTTP handoff operations. All writes are journaled by BodyControl before send."""
from copy import deepcopy
import time
import uuid
from urllib.parse import quote

from contracts.body_control import VERSION, REVISION, ControlError, fresh, digest, encode, stamp, nav_prepare_refused
from brain.adapters.http_client import HttpClient, HttpContractError
from shared.service_credentials import service_token


class ControlDriver:
    def __init__(self, config, *, clients=None):
        real = config['reception_real']
        self.settings = real['body_control']
        self.timeout = float(self.settings.get('operation_timeout_sec', 10))
        self.poll = float(self.settings.get('poll_interval_sec', .2))
        self.max_age = float(self.settings.get('evidence_max_age_sec', 10))
        if not (0 < self.timeout <= 60 and 0 < self.poll <= self.timeout and 0 < self.max_age <= 60):
            raise ValueError('Invalid bounded control timeout / evidence age')
        self.clients = clients or {
            name: HttpClient(url, timeout_sec=float(real.get('request_timeout_sec', 10)))
            for name, url in {'nav': real['dream_base_url'], 'vla': real['vla_base_url'],
                              'relay': self.settings['relay_base_url']}.items()}
        for service, client in self.clients.items():
            if isinstance(client, HttpClient):
                client.auth_token_env = self.token_env(service)

    def token_env(self, service):
        if service == 'nav' and self.settings.get('nav_token_env'):
            return self.settings['nav_token_env']
        return self.settings.get('token_env', 'BRAIN_CONTROL_TOKEN')

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
        path = {'nav': '/v1/status', 'vla': '/v1/vla/control/status', 'relay': '/v1/relay/status'}[service]
        try:
            return self._fresh(self._call(service, 'GET', path + '?observation_id=' + uuid.uuid4().hex))
        except ControlError as exc:
            raise ControlError(exc.code, service.upper() + ' 状态：' + str(exc), exc.status) from None

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
        return dict(contract_version=VERSION, coordination_revision=REVISION,
                    robot_serial=row['robot_serial'], action_endpoint=row['action_endpoint'],
                    task_id=row['task_id'], command_id=row['command_id'], lease_id=row['lease_id'],
                    authority_id=row['authority_id'], ownership_epoch=row['ownership_epoch'], action=action,
                    control_authorization={k: row[k] for k in ('lease_id', 'authority_id', 'ownership_epoch')})

    @staticmethod
    def operation_receipt(value, payload, service=None):
        """Read the peer's persisted request/result envelope without inventing facts."""
        if not isinstance(value, dict):
            raise ControlError('INVALID_RECEIPT', '控制回执必须为对象')
        view = deepcopy(value)
        if service == 'nav' and view.get('execution_authority') is False and isinstance(view.get('evidence'), dict):
            required = ('contract_version','coordination_revision','task_id','command_id','request_id','action','control_authorization')
            if any(encode(view.get(key)) != encode(payload[key]) for key in required):
                raise ControlError('IDENTITY_MISMATCH', 'NAV 回执的原命令／授权身份不匹配')
            for key in ('robot_serial','action_endpoint','lease_id','authority_id','ownership_epoch'):
                if key in view and encode(view[key]) != encode(payload[key]):
                    raise ControlError('IDENTITY_MISMATCH', 'NAV 平铺与嵌套身份冲突')
            if view.get('state') == 'completed':
                association = {key: payload[key] for key in ('task_id','command_id','control_authorization')}
                cleared = (payload['action'] == 'yield' and 'current_local_association' in view
                    and view['current_local_association'] is None and view.get('current_local_state') == 'yielded'
                    and view.get('effect_status') == 'confirmed'
                    and all(view['evidence'].get(k) is True for k in
                        ('navigation_stopped','output_fenced','mode_idle_hold_fenced','hold_verified')))
                if (view.get('historical') is not False
                        or (view.get('current_local_association') != association and not cleared)
                        or view.get('accepted') is not True):
                    raise ControlError('STALE_PARTICIPANT', '历史或重启后的 NAV 回执不能确认当前准备／退出')
                facts = view['evidence']
                if facts.get('observation_only') is True:
                    previous = facts.get('observed_association') or {}
                    old_ref = previous.get('control_authorization') or {}
                    new_ref = payload['control_authorization']
                    if (not cleared or facts.get('requested_association') != association
                            or facts.get('stop_command_sent') is not False
                            or facts.get('original_request_outcome') != 'unknown'
                            or facts.get('native_receiver_sealed') is not True
                            or old_ref.get('authority_id') != new_ref['authority_id']
                            or type(old_ref.get('ownership_epoch')) is not int
                            or not 0 <= old_ref['ownership_epoch'] < new_ref['ownership_epoch']):
                        raise ControlError('INVALID_RECEIPT', 'NAV 当前退出观察的原关联、请求关联或退出事实不完整')
                phase = 'prepared' if payload['action'] == 'prepare' else 'yielded'
                if view.get('current_local_state') != phase:
                    pending_phase = 'preparing' if payload['action'] == 'prepare' else 'yielding'
                    if view.get('current_local_state') != pending_phase:
                        raise ControlError('STALE_PARTICIPANT', 'NAV 本端阶段尚未核清')
                    # The peer publishes effect completion before its local
                    # transaction commits; wait for the persisted ready state.
                    view['state'] = 'pending'
            facts = view['evidence']
            for key in ('navigation_stopped','output_fenced','hold_verified','holding','navigation_not_started'):
                if key in facts:
                    if key in view and encode(view[key]) != encode(facts[key]):
                        raise ControlError('INVALID_RECEIPT', 'NAV 执行事实相互矛盾')
                    view.setdefault(key, deepcopy(facts[key]))
            if 'mode_confirmed' in facts:
                if 'mode_ready' in view and encode(view['mode_ready']) != encode(facts['mode_confirmed']):
                    raise ControlError('INVALID_RECEIPT', 'NAV 模式事实相互矛盾')
                view.setdefault('mode_ready', facts['mode_confirmed'])
            return view
        echoed = view.get('request')
        if echoed is not None:
            if encode(echoed) != encode(payload):
                raise ControlError('IDENTITY_MISMATCH', '对端保存的原请求与本次请求不一致')
            for key in ('contract_version', 'coordination_revision', 'robot_serial', 'action_endpoint',
                        'task_id', 'command_id', 'lease_id', 'authority_id', 'ownership_epoch',
                        'request_id', 'action', 'control_authorization'):
                if key in view and encode(view[key]) != encode(echoed[key]):
                    raise ControlError('IDENTITY_MISMATCH', '平铺身份与原请求冲突')
                view.setdefault(key, deepcopy(echoed[key]))
            if 'status' in view:
                if 'state' in view and view['state'] != view['status']:
                    raise ControlError('INVALID_RECEIPT', '控制结果状态相互矛盾')
                view['state'] = view['status']
            facts = view.get('exit_state') if payload['action'] == 'stop' else view.get('result')
            if isinstance(facts, dict):
                for key in ('policy_stopped', 'publisher_stopped', 'hands_stopped', 'output_fenced',
                            'action_port_released', 'hold_verified'):
                    if key in facts:
                        if key in view and encode(view[key]) != encode(facts[key]):
                            raise ControlError('INVALID_RECEIPT', '控制结果事实相互矛盾')
                        view.setdefault(key, facts[key])
        return view

    nav_prepare_refused = staticmethod(nav_prepare_refused)

    def exchange(self, wire, *, send, record=None, history=None):
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
                message = (f"{service.upper()} {payload.get('action', 'cancel')} POST HTTP {first['status_code']}: "
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
            if send:
                try:
                    call('POST', wire['path'], payload)
                except HttpContractError:
                    pass
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    raw = call('GET', wire['query_path'])
                    if raw.get('task_id') != payload['task_id'] or raw.get('command_id') != payload['command_id']:
                        raise ControlError('IDENTITY_MISMATCH', '导航停止查询不属于原任务／命令')
                    if (raw.get('state') in {'succeeded', 'failed', 'cancelled'}
                            and (raw.get('result') or {}).get('navigation_stopped') is True):
                        return raw
                except HttpContractError:
                    pass
                if time.monotonic() >= deadline:
                    raise failed('STOP_UNCONFIRMED', '原导航停止未确认；只查询原命令，不重发取消')
                time.sleep(self.poll)
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
                if service == 'vla' and payload['action'] == 'stop' and 'stop_requests' in value:
                    value = (value['stop_requests'] or {}).get(payload['request_id'])
                    if value is None:
                        if time.monotonic() >= deadline:
                            raise failed('STOP_UNCONFIRMED', '原停止请求没有可核查回执')
                        time.sleep(self.poll)
                        value = call('GET', wire['query_path'])
                        continue
                nav_receipt = (service == 'nav' and value.get('execution_authority') is False
                               and isinstance(value.get('evidence'), dict))
                value = self.operation_receipt(value, payload, service)
                if not nav_receipt and (any(encode(value.get(k)) != encode(payload[k]) for k in (
                        'contract_version', 'coordination_revision', 'robot_serial', 'action_endpoint',
                        'task_id', 'command_id', 'lease_id', 'authority_id', 'ownership_epoch', 'request_id'))
                        or encode(value.get('control_authorization')) != encode(payload['control_authorization'])
                        or value.get('action') != payload['action']):
                    raise ControlError('IDENTITY_MISMATCH', '交接回执身份不匹配')
                if value.get('state') in {'failed', 'rejected', 'unconfirmed', 'unknown', 'blocked'}:
                    raise failed('HANDOFF_UNCONFIRMED', str(value.get('error') or value.get('blockers') or value['state']))
                if value.get('state') != 'pending' and (
                        value.get('completed') is True or value.get('state') in {'succeeded', 'completed'}):
                    self._fresh(value, robot_required=not nav_receipt)
                    return value
            if time.monotonic() >= deadline:
                raise failed('HANDOFF_UNCONFIRMED', '查询原交接请求超时；没有重新发送')
            time.sleep(self.poll)
            try:
                value = call('GET', wire['query_path'])
            except HttpContractError:
                value = None

    def idle(self, pending=None):
        nav, vla, relay = (self.status(name) for name in ('nav', 'vla', 'relay'))
        vla_commands = ('', None, pending['command_id']) if pending and pending['target'] == 'vla' else ('', None)
        if ('active_command_id' not in nav or nav['active_command_id'] not in ('', None)
                or 'active_command_id' not in vla or vla['active_command_id'] not in vla_commands
                or vla.get('policy_running') is not False):
            raise ControlError('SOURCE_NOT_RELEASED', '现场仍有活动命令或策略状态未知')
        self.require(nav, 'output_fenced', 'hold_verified')
        self.require(vla, 'output_fenced', 'action_port_released', 'hold_verified')
        self._closed_relay(relay)
        return dict(nav=nav, vla=vla, relay=relay)

    def _closed_relay(self, relay):
        if (relay.get('bound') is not False or relay.get('forwarding_enabled') is not False
                or relay.get('action_port_released') is not True
                or relay.get('action_endpoint') != self.settings['action_endpoint']):
            raise ControlError('SOURCE_NOT_RELEASED', 'relay 尚未核清关闭与端口释放')

    def navigation_finished(self, row):
        raw = self._call('nav', 'GET', '/v1/commands/' + quote(row['command_id'], safe=''))
        body = row['request']['body']
        if (raw.get('task_id') != row['task_id'] or raw.get('command_id') != row['command_id']
                or raw.get('state') != 'succeeded'
                or any(raw.get(key) != body.get(key) for key in ('contract_version', 'target_id', 'route_phase', 'leg_index'))):
            raise ControlError('SOURCE_NOT_FINISHED', '原导航尚未成功；先处理原命令')
        self.require(raw.get('result'), 'navigation_stopped', 'reached')

    def yield_navigation(self, row, exchange, *, verify_current=False):
        yielded = exchange('nav-yield', 'nav', '/v1/navigation/handoff/yield', self.payload(row, 'yield'),
                           '/v1/navigation/handoff/requests/{request_id}')
        self.require(yielded, 'navigation_stopped', 'output_fenced', 'hold_verified')
        closed = exchange('relay-close', 'relay', '/v1/relay/control', self.payload(row, 'close'),
                          '/v1/relay/requests/{request_id}')
        self._closed_relay(closed)
        result = dict(exit_confirmed=True, hold_verified=True, navigation=yielded, relay=closed)
        if verify_current:
            current = self.status('relay')
            self.same_control(current, row)
            self._closed_relay(current)
            if (current.get('enabled') is not False or current.get('recovery_required') is not False
                    or current.get('local_subscriber_queue_closed') is not True):
                raise ControlError('RELAY_EXIT_UNCONFIRMED', 'NAV 已退出，relay 当前转发或队列关闭尚未核清')
            result['current_relay'] = current
        return result

    def cancel_navigation(self, row, exchange):
        return exchange('nav-cancel', 'nav', '/v1/navigation/cancel',
                        dict(task_id=row['task_id'], command_id=row['command_id'], reason='brain_task_cancelled'),
                        '/v1/commands/' + quote(row['command_id'], safe=''), 'navigation_cancel')

    def retire_relay_preparation(self, row, wire, exchange):
        """Retire only relay effects when no NAV prepare was ever journaled.

        Unknown enable requests cannot be treated as absent. A failed enable
        can already have closed its transport, leaving an older applied identity.
        Current closure is independent of readiness for a new physical action.
        """
        rejected = wire.get('nav_prepare_rejection')
        if rejected:
            nav = self.status('nav')
            self.require(nav, 'output_fenced', 'navigation_stopped')
            participant = nav.get('brain_control') or {}
            if ('active_command_id' not in nav or nav['active_command_id'] not in ('', None)
                    or participant.get('operation_pending') is not False):
                raise ControlError('SOURCE_NOT_RELEASED', 'NAV prepare 被拒绝，但当前 NAV 仍有活动或未知操作')
            association = participant.get('control_association') or {}
            if (association.get('task_id') == row['task_id'] and association.get('command_id') == row['command_id']):
                raise ControlError('IDENTITY_MISMATCH', 'NAV 当前已关联本轮，不能按未接管核销')
        payload = self.payload(row, 'enable_navigation')
        payload['request_id'] = wire['payload']['request_id']
        if wire['service'] != 'relay' or encode(wire['payload']) != encode(payload):
            raise ControlError('IDENTITY_MISMATCH', '原 relay 开启请求不属于本授权')
        original = self.operation_receipt(self._call('relay', 'GET', wire['query_path']), payload, 'relay')
        if any(encode(original.get(key)) != encode(value) for key, value in payload.items()):
            raise ControlError('IDENTITY_MISMATCH', '原 relay 开启回执身份不匹配')
        if original.get('state') not in {'failed', 'rejected', 'completed', 'succeeded'}:
            raise ControlError('RELAY_ENABLE_UNCONFIRMED', '原 relay 开启请求尚无明确终态；只查询，不重发')
        current = self.status('relay')
        if not current.get('relay_instance_id'):
            raise ControlError('IDENTITY_MISMATCH', 'relay 当前实例身份缺失')
        already_closed = False
        if original['state'] in {'failed', 'rejected'}:
            try:
                self._closed_relay(original)
                self._closed_relay(current)
                already_closed = (current.get('enabled') is False
                    and current.get('recovery_required') is False
                    and current.get('local_subscriber_queue_closed') is True)
            except ControlError:
                pass
        closed = None
        if not already_closed:
            # Never close a different live source using this failed grant.
            self.same_control(current, row)
            closed = exchange('relay-close', 'relay', '/v1/relay/control', self.payload(row, 'close'),
                              '/v1/relay/requests/{request_id}')
            self._closed_relay(closed)
        final = self.status('relay')
        self._closed_relay(final)
        if (final.get('enabled') is not False or final.get('recovery_required') is not False
                or final.get('local_subscriber_queue_closed') is not True):
            raise ControlError('RELAY_EXIT_UNCONFIRMED', 'relay 转发、恢复状态或原订阅队列尚未核清关闭')
        if (final.get('relay_instance_id') != current.get('relay_instance_id')
                or (already_closed and final.get('applied_authorization') != current.get('applied_authorization'))):
            raise ControlError('IDENTITY_MISMATCH', 'relay 核查期间实例或关联发生变化')
        if not already_closed:
            self.same_control(final, row)
        return dict(exit_confirmed=True, navigation_prepare_not_sent=not bool(rejected),
                    navigation_prepare_not_accepted=bool(rejected), original_nav_rejection=rejected,
                    original_relay_enable=original, relay_close=closed, relay=final,
                    physical_readiness_verified=False,
                    scope='relay preparation retired; NAV never accepted this preparation and business was not submitted')

    def reconcile_navigation(self, row, prepare_wire, exchange):
        """Failed preparation may have no usable yield. Require current exit facts.

        Never infer exit from a missing command, dead HTTP service or old success.
        Native effects without a confirmed yield remain outside this fallback.
        """
        nav = self.status('nav')
        self.require(nav, 'output_fenced', 'navigation_stopped')
        participant = nav.get('brain_control') or {}
        if (nav.get('active_command_id') not in ('', None) or 'active_command_id' not in nav
                or participant.get('operation_pending') is not False
                or not nav.get('controller_instance_id')):
            raise ControlError('SOURCE_NOT_RELEASED', 'NAV 原操作仍在进行或当前退出身份未知')
        proof = None
        restart_proof = None
        if prepare_wire:
            proof = self._call('nav', 'GET', prepare_wire['query_path'])
            payload = prepare_wire['payload']
            if any(encode(proof.get(k)) != encode(payload[k]) for k in
                   ('task_id', 'command_id', 'request_id', 'control_authorization')):
                raise ControlError('IDENTITY_MISMATCH', '原 prepare 查询身份不匹配')
            execution = proof.get('execution_query') or {}
            record = execution.get('record') or {}
            if proof.get('state') == 'pending' or execution.get('native_steps') != {}:
                raise ControlError('SOURCE_NOT_RELEASED', '原 native 事务尚未核清，需导航提供原退出回执')
            if record.get('controller_instance_id') != nav['controller_instance_id']:
                restart_proof = self._cross_boot_exit(record, proof, nav, payload)
        association = dict(task_id=row['task_id'], command_id=row['command_id'],
                           control_authorization={k:row[k] for k in ('lease_id','authority_id','ownership_epoch')})
        if participant.get('control_association') not in (None, association):
            raise ControlError('IDENTITY_MISMATCH', 'NAV 已关联其他控制源，拒绝核销旧授权')
        if restart_proof and 'business_submission' in row:
            raise ControlError('SOURCE_NOT_RELEASED', '已提交业务的授权不能按未执行准备核销')
        if not restart_proof:
            self.require(nav, 'hold_verified')
        closed = exchange('relay-close', 'relay', '/v1/relay/control', self.payload(row, 'close'),
                          '/v1/relay/requests/{request_id}')
        self._closed_relay(closed)
        if restart_proof:
            # Retirement of a dead boot's unexecuted NAV grant is not a claim
            # that the new VLA/O6 stack is ready. New grants still require idle().
            current_nav = self.status('nav')
            current_relay = self.status('relay')
            self.require(current_nav, 'output_fenced', 'navigation_stopped')
            self._closed_relay(current_relay)
            current_participant = current_nav.get('brain_control') or {}
            if (current_nav.get('controller_instance_id') != nav['controller_instance_id']
                    or 'active_command_id' not in current_nav or current_nav['active_command_id'] not in ('', None)
                    or current_participant.get('operation_pending') is not False
                    or current_participant.get('control_association') not in (None, association)):
                raise ControlError('IDENTITY_MISMATCH', '核销期间当前控制器、操作或控制源发生变化')
            return dict(exit_confirmed=True, reconciled=True, original_prepare=proof,
                        restart_exit_evidence=restart_proof, navigation=current_nav, relay=current_relay,
                        physical_readiness_verified=False,
                        scope='retired old-boot grant with no native effects or business submission; new motion requires fresh admission')
        current = self.idle()
        self.require(current['nav'], 'navigation_stopped')
        controller = current['vla'].get('controller_instance_id') or (current['vla'].get('execution_facts') or {}).get('controller_instance_id')
        if (current['nav'].get('controller_instance_id') != nav['controller_instance_id']
                or controller != nav['controller_instance_id']
                or (current['nav'].get('brain_control') or {}).get('operation_pending') is not False):
            raise ControlError('IDENTITY_MISMATCH', '退出核查期间控制器或 NAV 操作发生变化')
        return dict(exit_confirmed=True, hold_verified=True, reconciled=True,
                    original_prepare=proof, idle_evidence=current, relay=closed,
                    restart_exit_evidence=restart_proof)

    @staticmethod
    def _cross_boot_exit(record, original, current, payload):
        """Verify boot-bound process identity, not merely a different PID.

        Called only with no native steps, fresh stopped/fenced NAV, and no
        pending operation. The final check revalidates NAV and relay isolation.
        A controller restart within the same boot still needs explicit old-exit
        evidence; PID change by itself is insufficient.
        """
        def identity(value):
            if not isinstance(value, str):
                raise ValueError('missing process identity')
            boot, pid, ticks = value.split(':')
            if str(uuid.UUID(boot)) != boot or not pid.isdigit() or not ticks.isdigit() or int(pid) <= 0 or int(ticks) < 0:
                raise ValueError('invalid boot/PID/start identity')
            return boot
        try:
            old_boot = identity(record.get('controller_instance_id'))
            owner_boot = identity(record.get('owner_instance'))
            current_boot = identity(current.get('controller_instance_id'))
            recorded = record.get('request') or {}
            if (old_boot != owner_boot or current_boot == old_boot or original.get('historical') is not True
                    or any(encode(recorded.get(k)) != encode(payload[k]) for k in
                           ('task_id','command_id','request_id','control_authorization'))):
                raise ValueError('original process/request or reboot not verified')
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ControlError('SOURCE_NOT_RELEASED', '控制器变化但旧进程退出证据不足；同一 boot 换 PID 不能直接核销') from None
        return dict(basis='verified_boot_change_and_current_output_isolation',
                    old_controller_instance_id=record['controller_instance_id'],
                    old_owner_instance=record['owner_instance'], current_controller_instance_id=current['controller_instance_id'],
                    old_boot_id=old_boot, current_boot_id=current_boot,
                    old_boot_processes_cannot_remain=True, original_requests_preserved=True,
                    native_steps_empty=True)

    def prepare_navigation(self, row, exchange):
        # Opening the relay must not wait for NAV's first new preparation frame.
        enabled = exchange('relay-enable', 'relay', '/v1/relay/control', self.payload(row, 'enable_navigation'),
                           '/v1/relay/requests/{request_id}')
        self.require(enabled, 'bound', 'forwarding_enabled')
        payload = self.payload(row, 'prepare')
        held = row['request'].get('required_holding')
        if held:
            payload['required_holding'] = held
        prepared = exchange('nav-prepare', 'nav', '/v1/navigation/handoff/prepare', payload,
                            '/v1/navigation/handoff/requests/{request_id}')
        self.require(prepared, 'mode_ready', 'hold_verified')
        if (prepared.get('navigation_motion_authorized') is True or
                (prepared.get('navigation_motion_authorized') is not False and prepared.get('navigation_not_started') is not True)):
            raise ControlError('PREPARE_STARTED_MOTION', '导航准备不得启动业务行走')
        if held and prepared.get('holding') != held:
            raise ControlError('HOLD_UNCONFIRMED', '导航准备未确认原持物目标')
        relay = self.status('relay')
        self.same_control(relay, row)
        self.require(relay, 'bound', 'forwarding_enabled', 'upstream_fresh')
        return dict(navigation=prepared, relay=relay, prepared=True)

    def stop_vla(self, row, exchange):
        path = '/v1/vla/tasks/' + quote(row['command_id'], safe='')
        stopped = exchange('vla-stop', 'vla', path + '/stop', self.payload(row, 'stop'), path)
        self.require(stopped, 'policy_stopped', 'publisher_stopped', 'output_fenced', 'action_port_released')
        return stopped

    def vla_prerequisites(self, row):
        proof = row['request']['body']['navigation_proof']
        raw = self._call('nav', 'GET', '/v1/commands/' + quote(proof['dream_command_id'], safe=''))
        if (raw.get('task_id') != row['task_id'] or raw.get('command_id') != proof['dream_command_id']
                or raw.get('target_id') != proof['target_id'] or raw.get('state') != 'succeeded'):
            raise ControlError('NAVIGATION_PROOF_INVALID', '原导航命令不匹配')
        self.require(raw.get('result'), 'success', 'reached', 'navigation_stopped')
        nav, relay = self.status('nav'), self.status('relay')
        self.require(nav, 'output_fenced', 'hold_verified')
        if nav.get('active_command_id') not in ('', None) or 'active_command_id' not in nav:
            raise ControlError('NAVIGATION_BUSY', '导航仍有活动命令')
        context = nav.get('proof_context') or {}
        self.require(context, 'valid', 'pose_within_tolerance')
        if (context.get('dream_command_id') != proof['dream_command_id']
                or context.get('target_id') != proof['target_id']):
            raise ControlError('NAVIGATION_PROOF_INVALID', '当前站位证明未绑定原导航')
        if any(not isinstance(context.get(k), str) or not context[k] for k in
               ('map_id', 'localization_epoch', 'navigation_epoch')) or not nav.get('controller_instance_id'):
            raise ControlError('NAVIGATION_PROOF_INVALID', '地图、定位或控制器实例未核清')
        self._closed_relay(relay)
        return dict(navigation_stopped=True, action_port_released=True,
                    navigation_proof=deepcopy(proof), proof_context=deepcopy(context),
                    controller_instance_id=nav.get('controller_instance_id'), navigation=nav, relay=relay)

    def vla_exit(self, row, request):
        raw = self._call('vla', 'GET', '/v1/vla/tasks/' + quote(row['command_id'], safe=''))
        self._fresh(raw)
        self.same_control(raw, row)
        exit_state = raw.get('exit_state')
        if exit_state != request['exit_state'] or digest(exit_state) != request['exit_state_sha256']:
            raise ControlError('EXIT_STATE_MISMATCH', '退出证明与原命令当前记录不一致')
        self.require(exit_state, 'policy_stopped', 'publisher_stopped', 'output_fenced',
                     'action_port_released', 'hold_verified')
        status = self.status('vla')
        execution = status.get('execution_facts') or {}
        controller = status.get('controller_instance_id', execution.get('controller_instance_id'))
        if ('controller_instance_id' in status and 'controller_instance_id' in execution
                and status['controller_instance_id'] != execution['controller_instance_id']):
            raise ControlError('IDENTITY_MISMATCH', '当前 SONIC 实例证明相互矛盾')
        if controller != row['evidence'].get('controller_instance_id'):
            raise ControlError('IDENTITY_MISMATCH', '当前 SONIC 实例已变化，退出保持需重新核清')
        if (status.get('policy_running') is not False or 'active_command_id' not in status
                or status['active_command_id'] not in ('', None, row['command_id'])):
            raise ControlError('SOURCE_NOT_RELEASED', 'VLA 策略或活动任务未核清')
        self.require(status, 'output_fenced', 'action_port_released', 'hold_verified')
        relay = self.status('relay')
        self._closed_relay(relay)
        return dict(exit_confirmed=True, hold_verified=True, exit_state=deepcopy(exit_state),
                    exit_state_sha256=request['exit_state_sha256'], manipulation=status, relay=relay)


from brain.adapters.execution import BodyAdapter, interpret_observation
from contracts.vla import NATIVE_OPERATIONS, validate_request


class ControlledBodyAdapter(BodyAdapter):
    """The existing body adapter with brain-owned resource checks enabled."""
    requires_authoritative_handoff = True

    def __init__(self, dream, vla, authority):
        super().__init__(dream, vla)
        self.authority = authority
        for service, client in (('nav', dream), ('vla', vla)):
            if isinstance(getattr(client, 'http', None), HttpClient):
                client.http.auth_token_env = authority.driver.token_env(service)

    @property
    def gate_open(self):
        # Navigation transport may deliberately be closed before preparation.
        return self.dream.status().get('interface_online') is True

    def submit(self, command_id, request):
        body, skill = deepcopy(request['body']), request.get('skill')
        control_attempted = self.authority.submission_attempted(body['task_id'], command_id)
        business_attempted = False
        holding_status = None
        try:
            if skill == 'navigate':
                if request.get('requires_vla_clearance'):
                    status = self.vla.control_status()
                    if (status.get('recovery_required') is not False
                            or status.get('policy_state_uncertain') is not False
                            or status.get('policy_running') is not False
                            or status.get('active_command_id') not in ('', None)
                            or 'active_command_id' not in status):
                        raise ControlError('VLA_NOT_READY', 'VLA 原动作、恢复或持物状态未核清')
                if request.get('required_holding'):
                    holding_status = self.authority.driver.status('vla')
                    self.authority.check_holding(holding_status, body['task_id'], request['required_holding'])
                control_attempted = True
                body['control_authorization'] = self.authority.prepare_navigation(request)
                if request.get('required_holding'):
                    # Preparation can take time. Recheck immediately before the
                    # business goal; expiration does not undo preparation effects.
                    holding_status = self.authority.driver.status('vla')
                    self.authority.check_holding(holding_status, body['task_id'], request['required_holding'])
                self.authority.record_submission(request, body)
                business_attempted = True
                raw = self.dream.submit_navigation(body)
            elif skill in NATIVE_OPERATIONS:
                validate_request(body)
                caps = self.capabilities() or {}
                status = self.vla.control_status()
                if (caps.get('real_action_enabled') is not True
                        or (caps.get('operations', {}).get(skill) or {}).get('configured') is not True
                        or status.get('recovery_required') is not False
                        or status.get('policy_state_uncertain') is not False):
                    raise ControlError('VLA_NOT_READY', 'VLA 执行或恢复条件不满足')
                control_attempted = True
                self.authority.register(request)
                self.authority.record_submission(request, body)
                business_attempted = True
                raw = self.vla.submit_task(body)
            else:
                raise ControlError('UNSUPPORTED_CONTROL_SKILL', '联合模式不派发未接入的动作')
            return dict(accepted=True, unclear=False, completed=False, command_id=command_id, raw=raw)
        except (ControlError, HttpContractError, ValueError) as exc:
            from contracts.submission import submission_refusal
            refusal = None
            if ((not control_attempted and not business_attempted)
                    or self.authority.business_not_submitted_after_cleanup(body['task_id'], command_id)):
                payload = {'success': False, 'error': {'code': 'SAFETY_NOT_READY', 'message': str(exc),
                    'details': {'task_id': body['task_id'], 'command_id': command_id,
                                'action_started': False, 'action_state_uncertain': False,
                                'holding_status': holding_status}}}
                refusal = submission_refusal(409, payload, command_id=command_id, task_id=body['task_id'])
            elif business_attempted and isinstance(exc, HttpContractError):
                refusal = submission_refusal(exc.status_code, exc.payload,
                                             command_id=command_id, task_id=body['task_id'])
                if refusal:
                    if skill == 'navigate':
                        try:
                            self.authority.settle_navigation(body['task_id'], command_id)
                        except (ControlError, HttpContractError):
                            refusal = None  # Preparation effects are still unresolved.
                    elif not self.authority.close_unacquired(request):
                        refusal = None
            if refusal:
                return dict(accepted=False, unclear=False, command_id=command_id,
                            submission_rejected=refusal, raw=refusal['raw'], error=str(exc))
            # Control preparation may have moved a mode or bound a port. Do not
            # classify it as "nothing happened" and allow an automatic retry.
            raw = exc.response() if isinstance(exc, ControlError) else getattr(exc, 'payload', None)
            if holding_status is not None and isinstance(raw, dict):
                raw.setdefault('error', {}).setdefault('details', {})['holding_status'] = holding_status
            return dict(accepted=False, unclear=True, command_id=command_id, error=str(exc), raw=raw)

    def _view(self, command_id, raw, request):
        released = self.authority.released(request['body']['task_id'], command_id)
        viewed = interpret_observation(command_id, raw, request, control_released=released)
        if request['skill'] == 'navigate':
            viewed['resources_released'] = released
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
        return self._view(command_id, base.get('raw'), request) if base.get('raw') else base

    def effect_control(self, action, command_id, request, **kwargs):
        result = super().effect_control(action, command_id, request, **kwargs)
        return self._view(command_id, result.get('raw'), request)

    def cancel(self, command_id, request):
        result = super().cancel(command_id, request)
        if result.get('accepted') and request['skill'] == 'navigate':
            try:
                self.authority.settle_navigation(request['body']['task_id'], command_id)
            except ControlError as exc:
                result.update(completed=False, error=str(exc))
        elif result.get('accepted') and request['skill'] in NATIVE_OPERATIONS:
            if not self.authority.released(request['body']['task_id'], command_id):
                stopped = self.authority.stop_vla(request['body']['task_id'], command_id)
                if stopped['state'] != 'stopped':
                    result.update(completed=False, error=str(stopped.get('error')))
        return result

    def handoff_with_context(self, context):
        answer = dict(available=True, confirmed=False, kind=context['kind'], task_id=context['task_id'],
                      source_command_id=context.get('source_command_id'), reason='source_unavailable')
        command, request = context.get('source_command_id'), context.get('source_request')
        if not command or not request:
            return answer
        source = self.query(command, request)
        answer['source'] = source
        evidence = source.get('evidence') or {}
        if evidence.get('identity_ok') is not True or evidence.get('time_ok') is not True:
            answer['reason'] = 'source_receipt_invalid'
            return answer
        if source.get('stopped') is not True:
            answer['reason'] = 'source_stop_unconfirmed'
            return answer
        if source.get('terminal') != 'succeeded' and not context.get('manual_skips'):
            answer['reason'] = 'source_success_unconfirmed'
            return answer
        try:
            if request['skill'] == 'navigate':
                self.authority.settle_navigation(context['task_id'], command)
            if not self.authority.released(context['task_id'], command):
                raise ControlError('SOURCE_NOT_RELEASED', '原授权退出尚未核清')
            answer['idle_evidence'] = self.authority.driver.idle()
            held = request.get('required_holding')
            if request['skill'] == 'grasp_cola':
                held = request['body']['object_id']
            if held:
                self.authority.check_holding(answer['idle_evidence']['vla'], context['task_id'], held)
            answer.update(confirmed=True, reason='source_exit_confirmed_next_grant_required')
        except (ControlError, HttpContractError) as exc:
            answer.update(reason='handoff_unconfirmed', error=str(exc))
        return answer
