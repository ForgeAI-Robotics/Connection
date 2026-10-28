"""Single body authority. Reuses Runtime identities, directory lock and request journal."""
from copy import deepcopy
import fcntl
import os
import threading
import time
import uuid
from pathlib import Path

from contracts.body_control import (VERSION, REVISION, PERMIT_MODE, IDENTITY, GATEWAY_BINDING, ControlError,
                                    encode, identifier, lease_identity, stamp)
from brain.storage.body_control import ControlStore
from brain.workers import WorkerPool


class BodyControl:
    def __init__(self, config, root, driver, executor=None):
        self.config, self.root, self.driver, self.executor = config, Path(root), driver, executor
        real = config['reception_real']
        self.settings = real['body_control']
        self.robot, self.endpoint = self.settings['robot_serial'], self.settings['action_endpoint']
        self.resource = self.settings['control_resource']
        if self.resource != 'g1:' + self.robot + ':body':
            raise ValueError('control_resource must name this robot body')
        self.binding = dict(robot_serial=self.robot, control_resource=self.resource, action_endpoint=self.endpoint,
                            dream=real['dream_base_url'], gateway=self.settings['gateway_base_url'])
        self.authority_id = uuid.uuid4().hex
        self.store = self._fd = None
        self._start_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._permit_lock = threading.Lock()
        self._cleanup_future = None
        self._cleanup_futures = {}
        self._closed = False
        self._stop = threading.Event()
        # Independent bounded stop lanes cannot queue behind a long prepare.
        self._exit_pool = WorkerPool('brain-body-exit', 2)
        if self.settings.get('permit_mode') != PERMIT_MODE:
            raise ValueError('Control session configuration required; migrate before switching')

    def start(self):
        with self._start_lock:
            if self._closed:
                raise ControlError('CONTROL_CLOSED', '控制服务已关闭', 503)
            if self.store is not None:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.root / 'body_control.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                store = ControlStore(self.root)
                with store.writer():
                    state = store.read()
                    if state is None:
                        state = dict(schema=3, binding=self.binding, epoch=0, revision=0,
                                     current=None, busy=None, leases={}, requests={})
                    if state['schema'] != 3:
                        raise ControlError('CONTROL_MIGRATION_REQUIRED', '旧控制账本必须显式迁移到 schema=3，不能恢复旧执行权')
                    if state['binding'] != self.binding:
                        raise ControlError('CONTROL_TARGET_CHANGED', '控制端点改变，须先核清原控制记录')
                    # Leave all source identities and receipts intact. No replay.
                    for row in state['leases'].values():
                        if row['state'] != 'released':
                            if (row.get('target') == 'vla' and row.get('source_url') !=
                                    self.config['reception_real']['vla_base_url']):
                                raise ControlError('CONTROL_TARGET_CHANGED', '原 VLA 控制记录尚未核清，不能改用新端点')
                            row['state'] = 'recovery_required'
                    for op in state['requests'].values():
                        if (op['receipt']['state'] == 'pending' and not
                                state['leases'].get(op['body'].get('lease_id'), {}).get('legacy_control')):
                            op['receipt'].update(state='unconfirmed', error={'code': 'AUTHORITY_RESTARTED',
                                                                          'message': '重启后仅查询原操作'})
                    for row in state['leases'].values():
                        operations = [op for op in state['requests'].values()
                                      if op['body'].get('lease_id') == row['lease_id']]
                        if (row['state'] != 'released' and not row.get('legacy_control') and 'business_submission' not in row
                                and not any(op['children'] for op in operations)):
                            row.update(state='released', evidence=dict(exit_confirmed=True, not_acquired=True,
                                cleanup=True, business_not_submitted=True, physical_readiness_verified=False,
                                scope='restart: original journal proves no peer writes'))
                            if state['current'] == row['lease_id']:
                                state['current'] = None
                    if state['current'] is None:
                        state['current'] = next((r['lease_id'] for r in state['leases'].values()
                                                 if r['state'] != 'released' and not r.get('task_cancelled')), None)
                    state.update(authority_id=self.authority_id, busy=None)
                    state['revision'] += 1
                    store.write(state)
                self._fd, self.store = fd, store
            except Exception:
                os.close(fd)
                raise

    def stop_accepting(self):
        self._closed = True
        self._stop.set()

    def close(self):
        self.stop_accepting()
        futures = list(self._cleanup_futures.values())
        for future in futures:
            if not future.done():
                try:
                    future.result(timeout=self.driver.timeout + 1)
                except Exception:
                    pass
        stopped = self._exit_pool.close(self.driver.timeout + 1)
        if not stopped or any(not future.done() for future in futures):
            return False
        with self._start_lock:
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = None
        return True

    def _save(self, state):
        if state.get('authority_id') != self.authority_id:
            raise ControlError('STALE_AUTHORITY', '大脑控制实例已变化，未写入迟到结果')
        state['revision'] += 1
        state['updated_at'] = stamp()
        self.store.write(state)

    def _eligible(self, request):
        """Called under the same directory writer lock as Runtime cancellation."""
        from brain.storage.tasks import load_existing
        record = load_existing(self.root) or {}
        body = request['body']
        if (record.get('task_id') != body.get('task_id')
                or record.get('open_command_id') != body.get('command_id')
                or record.get('dispatch_closed') or record.get('control_request')
                or record.get('state') not in {'running', 'verifying', 'waiting_human'}
                or record.get('execution_backend') not in {'body_real', 'reception_real', 'reception_real_sim_manipulation', 'reception_sim_navigation_real'}
                or (self.config.get('reception_real') or {}).get('kernel_enabled') is not True):
            raise ControlError('NOT_SCHEDULED', '当前 Runtime 未允许该命令申请控制')
        target = record.get('execution_target') or {}
        service = 'dream' if request.get('skill') == 'navigate' else 'vla'
        if target.get(service) != self.config['reception_real'][service + '_base_url']:
            raise ControlError('CONTROL_TARGET_CHANGED', '任务绑定的执行端与控制端不符')
        if 'body_control' in target and target['body_control'] != {
                key:self.settings[key] for key in ('robot_serial','control_resource','action_endpoint','gateway_base_url')}:
            raise ControlError('CONTROL_TARGET_CHANGED', '任务绑定的网关资源已变化')
        for bucket in (record.get('steps') or {}).values():
            for attempt in bucket.get('attempts') or []:
                if attempt.get('command_id') == body['command_id'] and attempt.get('request') == request:
                    return record['execution_backend']
        raise ControlError('NOT_SCHEDULED', '请求与原 Runtime 尝试不一致')

    def register(self, request):
        self.start()
        request = deepcopy(request)
        body = request['body']
        if request.get('skill') not in {'navigate', 'open_fridge', 'grasp_cola'}:
            raise ControlError('UNSUPPORTED_CONTROL_SKILL', '当前网关未接入该动作')
        target = 'navigation' if request['skill'] == 'navigate' else 'vla'
        if not all(identifier(body.get(k)) for k in ('task_id', 'command_id')):
            raise ControlError('INVALID_IDENTITY', '任务／命令身份无效', 400)
        with self.store.writer():
            backend = self._eligible(request)
            if body.get('required_holding') or (request.get('required_holding')
                    and backend != 'reception_real_sim_manipulation'):
                raise ControlError('UNSUPPORTED_CONTROL_SKILL', '身体独立模式不接受真实携物要求')
            state = self.store.read()
            old = self._command_row(state, body['task_id'], body['command_id'])
            if old:
                if old['request'] != request:
                    raise ControlError('REQUEST_CONFLICT', '同一命令的请求内容改变')
                return old
            # command_id is globally unique; never retarget an existing command.
            if any(body['command_id'] in r.get('commands', {}) for r in state['leases'].values()):
                raise ControlError('REQUEST_CONFLICT', 'command_id 已被其他任务使用')
            row = state['leases'].get(state['current'])
            if row:
                if (row['state'] != 'authorized' or row.get('revoked')
                        or row['authority_id'] != self.authority_id or row.get('active_command_id')
                        or row['target'] != target or target == 'vla'):
                    raise ControlError('SOURCE_NOT_RELEASED', '当前命令或控制退出尚未核清')
            else:
                if any(r['state'] != 'released' and not r.get('task_cancelled') for r in state['leases'].values()):
                    raise ControlError('SOURCE_NOT_RELEASED', '原身体占用尚未核清')
                state['epoch'] += 1
                session = uuid.uuid4().hex
                lease = lease_identity(self.robot, 'control_session', session)
                row = dict(contract_version=VERSION, coordination_revision=REVISION,
                    permit_mode=PERMIT_MODE, control_session_id=session,
                    robot_serial=self.robot, control_resource=self.resource, action_endpoint=self.endpoint,
                    lease_id=lease, authority_id=self.authority_id, ownership_epoch=state['epoch'],
                    target=target, state='registered', evidence={}, commands={}, command_seq=0,
                    updated_at=stamp())
                if target == 'vla':
                    row['source_url'] = self.config['reception_real']['vla_base_url']
                state['leases'][lease] = row
            row.pop('business_submission', None)
            row['command_seq'] += 1
            row.update(task_id=body['task_id'], command_id=body['command_id'], request=request,
                       active_command_id=body['command_id'],
                       holding_scope='simulated' if backend == 'reception_real_sim_manipulation' else 'physical')
            if backend == 'reception_sim_navigation_real':
                from brain.storage.tasks import load_existing
                record = load_existing(self.root) or {}
                row['worksite_confirmation'] = deepcopy((record.get('selection') or {}).get('worksite_confirmation'))
                if not isinstance(row['worksite_confirmation'], dict) or row['worksite_confirmation'].get('ready') is not True:
                    raise ControlError('WORKSITE_EVIDENCE_REQUIRED', '导航模拟模式需要本任务的现场作业确认')
            row['commands'][body['command_id']] = dict(task_id=body['task_id'], command_id=body['command_id'],
                command_seq=row['command_seq'], request=request, state='registered')
            self._save(state)
            return deepcopy(row)

    @staticmethod
    def _command_row(state, task_id, command_id):
        for row in state['leases'].values():
            command = row.get('commands', {}).get(command_id)
            if command and command['task_id'] == task_id:
                value = deepcopy(row)
                value.pop('business_submission', None)
                value.update(deepcopy(command))
                # Command state and ownership state are distinct.
                value['command_state'] = command['state']
                value['state'] = row['state']
                return value
        return None

    def command_row(self, task_id, command_id):
        self.start()
        with self.store.writer():
            row = self._command_row(self.store.read(), task_id, command_id)
            if row is None:
                raise ControlError('UNKNOWN_COMMAND', '没有本命令控制关联')
            return row

    def record_submission(self, request, wire_body):
        """Persist the exact business wire request, checking cancellation before dispatch."""
        self.start()
        body = request['body']
        with self.store.writer():
            self._eligible(request)
            state = self.store.read()
            snapshot = self._command_row(state, body['task_id'], body['command_id'])
            if snapshot is None:
                raise ControlError('STALE_AUTHORIZATION', '派发没有登记的会话')
            row = state['leases'][snapshot['lease_id']]
            command = row['commands'][body['command_id']]
            if row['target'] == 'vla' and wire_body != row.get('prepared_business'):
                raise ControlError('REQUEST_CONFLICT', '操控执行请求必须与原准备请求完全一致')
            if 'business_submission' in command:
                raise ControlError('SUBMISSION_ALREADY_ATTEMPTED', '原业务已落盘，必须查询原命令')
            if row.get('active_command_id') != body['command_id'] or row['state'] != 'authorized':
                raise ControlError('NAVIGATION_NOT_PREPARED', '当前控制会话或业务命令未就绪')
            self._require_live(row, 'execute')
            submitted = {'body': deepcopy(wire_body), 'recorded_at': stamp()}
            command.update(business_submission=submitted, state='submitted')
            row['business_submission'] = submitted
            self._save(state)

    def business_body(self, request):
        body = deepcopy(request['body'])
        row = self.command_row(body['task_id'], body['command_id'])
        if row['target'] == 'vla':
            return deepcopy(row['prepared_business'])
        body.update(control_authorization=self.reference(row), control_session_id=row['control_session_id'],
                    command_seq=row['command_seq'], coordination_revision=REVISION, permit_mode=PERMIT_MODE,
                    dispatch_policy='when_idle',
                    request_id=uuid.uuid5(uuid.NAMESPACE_URL, row['lease_id']+':goal:'+body['command_id']).hex)
        return body

    @staticmethod
    def reference(row):
        return {key: row[key] for key in ('lease_id', 'authority_id', 'ownership_epoch')}

    def identity(self, row):
        return {key: row[key] for key in IDENTITY}

    def recovery_status(self):
        """Local resource facts only; polling never performs a recovery."""
        if self.store is None:
            return dict(enabled=True, blocked=False, pending=False, source=None, unresolved=[])
        with self.store.writer():
            state = self.store.read()
            rows = [row for row in state['leases'].values() if row['state'] != 'released' and not row.get('task_cancelled')]
            fields = ('control_session_id','active_command_id','command_seq','permit_mode','task_id','command_id','lease_id','target','state','ownership_epoch','cleanup_error',
                      'control_resource','gateway_binding','permit_phase','permit_seq','revoked','legacy_control')
            source = state['leases'].get(state['current'])
            source_view = {key:deepcopy(source.get(key)) for key in fields} if source else None
            blocked = any(r['state'] != 'authorized' or r.get('revoked') or
                          r['authority_id'] != self.authority_id for r in rows)
            return dict(enabled=True, blocked=bool(blocked or state['busy']),
                occupied=bool(rows), permit_mode=PERMIT_MODE, renew_required=False,
                pending=bool(state['busy'] or (state['current'] in self._cleanup_futures
                    and not self._cleanup_futures[state['current']].done())),
                previous_exit_unconfirmed=sum(r['state'] != 'released' and bool(r.get('task_cancelled'))
                                              for r in state['leases'].values()),
                source=source_view,
                unresolved=[{key:deepcopy(row.get(key)) for key in fields} for row in rows if row['state'] != 'authorized' or row.get('revoked')
                            or row['authority_id'] != self.authority_id])

    def assert_new_task_allowed(self):
        self.start()
        status = self.recovery_status()
        if status['blocked'] or (status.get('source') or {}).get('active_command_id'):
            source = status['source'] or (status['unresolved'] or [{}])[0]
            raise ControlError('SOURCE_NOT_RELEASED', '当前控制尚未结束：' +
                str(source.get('command_id') or '交接处理中') + '；取消整轮任务后可重新发布，原会话退出在后台独立核查')

    def released(self, task_id, command_id):
        self.start()
        with self.store.writer():
            row = self._command_row(self.store.read(), task_id, command_id)
            return bool(row and row['state'] == 'released' and row['evidence'].get('exit_confirmed') is True)

    def submission_attempted(self, task_id, command_id):
        self.start()
        with self.store.writer():
            row = self._command_row(self.store.read(), task_id, command_id)
            return bool(row and 'business_submission' in row)

    def business_not_submitted_after_cleanup(self, task_id, command_id):
        self.start()
        with self.store.writer():
            row = self._command_row(self.store.read(), task_id, command_id)
            return bool(row and row['state'] == 'released' and 'business_submission' not in row
                        and row['evidence'].get('cleanup') is True)

    def capabilities(self):
        self.start()
        return dict(contract_version=VERSION, coordination_revision=REVISION, authority='brain',
                    authority_id=self.authority_id, robot_serial=self.robot, control_resource=self.resource,
                    action_endpoint=self.endpoint, operations=['query'], supported_sources=['navigation', 'vla'],
                    hand_policy='per_command', supported_hand_modes=['not_required', 'hold', 'execute'],
                    features=['gateway-push', 'persistent-fenced-owner'], ready=True,
                    physical_readiness_verified=False, permit_mode=PERMIT_MODE, renew_required=False)

    def submit(self, body):
        raise ControlError('SOURCE_NOT_ENABLED', '执行端不再 acquire/release；许可由大脑直推网关', 409)

    def _require_live(self, row, phase=None):
        if (self._closed or row.get('revoked') or row['authority_id'] != self.authority_id
                or not row.get('permit') or row.get('permit_mode') != PERMIT_MODE
                or (phase and row.get('permit_phase') != phase)):
            raise ControlError('PERMIT_NOT_CURRENT', '当前会话许可尚未确认或已撤销')

    def query(self, lease, *, request_id=None, observation_id=None):
        self.start()
        if bool(request_id) == bool(observation_id) or not identifier(request_id or observation_id):
            raise ControlError('INVALID_QUERY', '必须指定一个 request_id 或新的 observation_id', 400)
        with self.store.writer():
            state = self.store.read()
            row = state['leases'].get(lease)
            if row is None:
                raise ControlError('UNKNOWN_LEASE', '授权不存在；不表示物理动作未发生', 404)
            if request_id:
                op = state['requests'].get(request_id)
                if op is None or op['body']['lease_id'] != lease:
                    raise ControlError('UNKNOWN_REQUEST', '原请求不存在；不可推断未执行', 404)
                return deepcopy(op['receipt'])
            active = state['leases'].get(state['current']) or {}
            current = (row['state'] == 'authorized' and row['authority_id'] == self.authority_id
                       and state['current'] == lease and not self._closed)
            try:
                self._require_live(row, 'execute')
            except ControlError:
                current = False
            owner = (active['target'] if active.get('state') == 'authorized'
                     and active.get('authority_id') == self.authority_id and current else 'unknown' if active else 'none')
            return dict(self.identity(row), authority='brain', authority_id=self.authority_id,
                        issued_authority_id=row['authority_id'], ownership_epoch=row['ownership_epoch'],
                        owner=owner, state=row['state'], authorization_current=current,
                        observation_id=observation_id, state_revision=state['revision'], updated_at=stamp(),
                        blockers=[] if current or row['state'] == 'released' else ['CONTROL_NOT_CURRENT'])

    def _begin(self, body, target):
        self.start()
        body = deepcopy(body)
        if (not isinstance(body, dict) or body.get('action') not in {'acquire', 'release'}
                or not identifier(body.get('request_id'))):
            raise ControlError('INVALID_REQUEST', '控制请求无效', 400)
        allowed = set(IDENTITY) | {'action', 'request_id', 'authority_id', 'ownership_epoch',
            'navigation_proof', 'requirements', 'controller_instance_id', 'policy_stopped',
            'reference_policy', 'hand_target_policy', 'exit_state', 'exit_state_sha256'}
        if set(body) - allowed:
            raise ControlError('INVALID_REQUEST', '未知控制请求字段', 400)
        encode(body)  # Reject non-finite / non-JSON input before persisting it.
        with self.store.writer():
            state = self.store.read()
            row = state['leases'].get(body.get('lease_id'))
            if row is None or any(body.get(k) != row[k] for k in IDENTITY):
                raise ControlError('IDENTITY_MISMATCH', '请求不匹配已登记授权')
            if row['target'] != target:
                raise ControlError('NOT_SCHEDULED', '不能为另一执行端申请控制')
            old = state['requests'].get(body['request_id'])
            if old:
                if old['body'] != body:
                    raise ControlError('REQUEST_CONFLICT', 'request_id 已被不同请求使用')
                return deepcopy(old['receipt']), False
            if state['busy']:
                raise ControlError('CONTROL_BUSY', '原交接尚未核清，请查询原请求')
            action = body['action']
            if action == 'acquire':
                if 'authority_id' in body and body['authority_id'] != self.authority_id:
                    raise ControlError('STALE_AUTHORIZATION', '申请引用旧大脑实例')
                self._eligible(row['request'])
                if row['state'] != 'registered' or row['authority_id'] != self.authority_id:
                    raise ControlError('STALE_AUTHORIZATION', '原命令授权已使用或属于旧实例')
                row.update(state='acquiring')
            else:
                if (state['current'] != row['lease_id'] or row['state'] not in {'authorized', 'recovery_required'}
                        or body.get('authority_id') != row['authority_id']
                        or type(body.get('ownership_epoch')) is not int
                        or body['ownership_epoch'] != row['ownership_epoch']):
                    raise ControlError('STALE_AUTHORIZATION', '只能核清对应的原授权')
                row['state'] = 'releasing'
            receipt = dict(self.identity(row), request_id=body['request_id'], action=action,
                           authority='brain', authority_id=row['authority_id'],
                           ownership_epoch=row['ownership_epoch'], accepted=True, state='pending',
                           updated_at=stamp())
            state['busy'] = body['request_id']
            state['requests'][body['request_id']] = dict(body=body, receipt=receipt, children={})
            self._save(state)
            return deepcopy(receipt), True

    def _internal(self, row, action):
        # Fixed operation identity: after an uncertain result, a second invocation
        # reads the original result; it never sends a new operation blindly.
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, row['lease_id'] + ':' + action).hex
        body = dict(self.identity(row), action=action, request_id=request_id)
        if action == 'release':
            body.update(authority_id=row['authority_id'], ownership_epoch=row['ownership_epoch'])
        receipt, created = self._begin(body, row['target'])
        if created:
            self._run(request_id)
        result = self.query(row['lease_id'], request_id=request_id)
        if result['state'] not in {'authorized', 'released'}:
            error = result.get('error') or {}
            raise ControlError(error.get('code', 'HANDOFF_UNCONFIRMED'),
                error.get('message', str(result['state'])), details=deepcopy(error.get('details')))
        return result

    def prepare_navigation(self, request):
        return self.prepare_command(request)

    def prepare_command(self, request):
        # A source change or VLA's one-command session uses the original exit path.
        # Same-source NAV continues reusing its existing session.
        self.start()
        target = 'navigation' if request.get('skill') == 'navigate' else 'vla'
        with self.store.writer():
            self._eligible(request)
            state = self.store.read()
            current = deepcopy(state['leases'].get(state['current']))
            known = self._command_row(state, request['body']['task_id'], request['body']['command_id'])
        if (not known and current and (current['target'] != target or target == 'vla')):
            if current.get('active_command_id'):
                raise ControlError('SOURCE_NOT_RELEASED', '原动作仍在执行，不能交接')
            self.release_control(current['task_id'], current['command_id'])
        row = self.register(request)
        if row['state'] == 'authorized':
            # Admission only: a lost observation does not revoke existing control.
            self.driver.verify_current_permit(row, self.driver.status('gateway'), 'execute', require_hold=True)
        else:
            self._internal(row, 'acquire')
        row = self.command_row(request['body']['task_id'], request['body']['command_id'])
        self._require_live(row, 'execute')
        return self.reference(row)

    def _snapshot(self, request_id):
        with self.store.writer():
            state = self.store.read()
            op = state['requests'][request_id]
            return deepcopy(state['leases'][op['body']['lease_id']]), deepcopy(op)

    def exchange(self, request_id, label, service, path, payload, query_path, kind=None, *, until_registered=False):
        """Persist exact wire request before POST; recover only by original GET."""
        child_id = uuid.uuid5(uuid.NAMESPACE_URL, request_id + ':' + label).hex
        payload = dict(deepcopy(payload), request_id=child_id)
        wire = dict(service=service, path=path, payload=payload, query_path=query_path.format(request_id=child_id))
        if kind:
            wire['kind'] = kind
        with self.store.writer():
            if self._closed:
                raise ControlError('CONTROL_CLOSED', '关闭期间不再发送控制写请求', 503)
            state = self.store.read()
            op = state['requests'][request_id]
            if op['body']['action'] in {'acquire','grant','allow_execute'}:
                row = state['leases'][op['body']['lease_id']]
                if row.get('revoked'):
                    raise ControlError('CONTROL_REVOKED', '本轮已撤销，禁止后到控制写入')
                self._eligible(state['leases'][op['body']['lease_id']]['request'])
            existing = op['children'].get(label)
            if existing and existing['wire'] != wire:
                raise ControlError('REQUEST_CONFLICT', '控制子请求改变')
            if not existing:
                op['children'][label] = dict(wire=wire, state='pending')
                self._save(state)
            elif existing.get('state') == 'confirmed':
                return deepcopy(existing['receipt'])
        def record(observation):
            with self.store.writer():
                state = self.store.read()
                if state.get('authority_id') != self.authority_id:
                    raise ControlError('STALE_AUTHORITY', '旧实例回包未写入新控制账本')
                child = state['requests'][request_id]['children'][label]
                facts = child.setdefault('observations', {})
                if observation['method'] == 'POST':
                    facts.setdefault('first_post', deepcopy(observation))
                else:
                    facts['last_query'] = deepcopy(observation)
                    facts['query_count'] = facts.get('query_count', 0) + 1
                self._save(state)
        try:
            result = self.driver.exchange(wire, send=existing is None, record=record,
                history=deepcopy((existing or {}).get('observations', {})), until_registered=until_registered)
        except ControlError as exc:
            with self.store.writer():
                state = self.store.read()
                if state.get('authority_id') == self.authority_id:
                    child = state['requests'][request_id]['children'][label]
                    child['last_error'] = deepcopy(exc.response()['error'])
                    self._save(state)
            raise
        with self.store.writer():
            state = self.store.read()
            child = state['requests'][request_id]['children'][label]
            child.update(state='registered' if result.get('state') == 'pending' else 'confirmed', receipt=deepcopy(result))
            self._save(state)
        return result

    def _permit(self, lease, operation, *, request_id=None):
        if operation not in {'grant','allow_execute','revoke'}:
            raise ControlError('RENEW_NOT_SUPPORTED', '持续控制会话不接受续期')
        # Revoke never waits for this lock: it persists a higher sequence and a tombstone first.
        lock = self._permit_lock if operation != 'revoke' else threading.Lock()
        with lock:
            with self.store.writer():
                state = self.store.read()
                row = deepcopy(state['leases'][lease])
                if operation != 'revoke':
                    self._eligible(row['request'])
                    if row.get('revoked') or row['authority_id'] != self.authority_id or self._closed:
                        raise ControlError('CONTROL_REVOKED', '许可已撤销')
                    if operation != 'grant':
                        self._require_live(row)
            status = ({'gateway_instance_id': row['gateway_binding']['gateway_instance_id']}
                      if operation == 'revoke' and row.get('gateway_binding')
                      else self.driver.status('gateway'))
            binding = row.get('gateway_binding') or dict(robot_serial=self.robot, control_resource=self.resource,
                control_session_id=row['control_session_id'], source_id=row['target'],
                control_authorization=self.reference(row), gateway_instance_id=status['gateway_instance_id'])
            if binding['gateway_instance_id'] != status['gateway_instance_id']:
                raise ControlError('GATEWAY_INSTANCE_CHANGED', '网关实例变化，需当前退出核验')
            phase = 'execute' if operation == 'allow_execute' else row.get('permit_phase', 'prepare')
            if operation == 'allow_execute':
                self.driver.verify_current_permit(row, status, row.get('permit_phase', 'prepare'),
                                                  require_hold=operation == 'allow_execute')
            rid = request_id or uuid.uuid4().hex
            with self.store.writer():
                state = self.store.read()
                current = state['leases'][lease]
                if operation != 'revoke':
                    self._eligible(current['request'])
                    if current.get('revoked') or self._closed:
                        raise ControlError('CONTROL_REVOKED', '申请锚点期间已撤销')
                old = state['requests'].get(rid)
                if old:
                    payload = deepcopy(old['body']['permit_request'])
                else:
                    current['permit_seq'] = current.get('permit_seq', 0) + 1
                    payload = dict(binding, contract_version=VERSION, coordination_revision=REVISION, permit_mode=PERMIT_MODE,
                                   operation=operation, permit_seq=current['permit_seq'])
                    if operation != 'revoke':
                        hand_mode = ('execute' if phase == 'execute' else 'hold') if row['target'] == 'vla' else 'not_required'
                        payload.update(phase=phase, hand_mode=hand_mode, hold_reference_id=binding.get('hold_reference_id'))
                    if operation == 'grant':
                        if 'brain-takeover-v1' not in status.get('features', []):
                            raise ControlError('TAKEOVER_UNSUPPORTED', '网关尚未支持大脑接管授权')
                        payload['takeover'] = True
                    state['requests'][rid] = dict(body=dict(self.identity(current), action=operation,
                        permit_request=deepcopy(payload)), receipt=dict(request_id=rid, state='pending'), children={})
                    self._save(state)
            result = self.exchange(rid, 'gateway-permit', 'gateway', '/v1/body-input/permits', payload,
                                   '/v1/body-input/requests/{request_id}')
            with self.store.writer():
                state = self.store.read()
                state['requests'][rid]['receipt'] = deepcopy(result)
                current = state['leases'][lease]
                if operation != 'revoke':
                    self._eligible(current['request'])
                    if current.get('revoked') or self._closed or current['authority_id'] != self.authority_id:
                        self._save(state)
                        raise ControlError('CONTROL_REVOKED', '保留迟到许可回包，但不恢复执行权')
                    permit = result['evidence']['permit']
                    current.update(permit_phase=phase, permit=deepcopy(permit))
                self._save(state)
            return result

    def _run(self, request_id):
        row, op = self._snapshot(request_id)
        try:
            # NAV owns business admission; the gateway owns old-flow retirement.
            status = self.driver.status('gateway')
            if 'brain-takeover-v1' not in status.get('features', []):
                raise ControlError('TAKEOVER_UNSUPPORTED', '网关尚未支持大脑接管授权')
            vla = row['target'] == 'vla'
            if vla:
                from brain.adapters.vla_control import capabilities
                capabilities(status)
            elif (status.get('hand_policy') == 'body_only'
                    and (status.get('native') or {}).get('old_hand_task_pending') is True):
                # A body-only takeover cannot fence an unresolved hand action.
                raise ControlError('OLD_HAND_TASK_UNRESOLVED', '旧手部动作未退出，身体独立网关不能接管')
            with self.store.writer():
                state = self.store.read()
                self._eligible(row['request'])
                current = state['leases'][row['lease_id']]
                if state['current'] is not None or current.get('revoked'):
                    raise ControlError('SOURCE_NOT_RELEASED', '身体资源未释放或本轮已取消')
                current.update(state='preparing', grant_published=True)
                state['current'] = row['lease_id']
                self._save(state)
            payload = self.driver.payload(row, 'prepare')
            service = 'vla' if vla else 'nav'
            prefix = '/v1/vla' if vla else '/v1/navigation'
            if vla:
                rid = uuid.uuid5(uuid.NAMESPACE_URL, request_id + ':vla-prepare').hex
                business, worksite = self.driver.vla_business(row, rid)
                with self.store.writer():
                    state = self.store.read()
                    current = state['leases'][row['lease_id']]
                    current.update(prepared_business=business, worksite_observation=worksite)
                    self._save(state)
                payload['business_request'] = business
            args = (request_id, service+'-prepare', service, prefix+'/handoff/prepare', payload,
                    prefix+'/handoff/requests/{request_id}')
            registered = self.exchange(*args, until_registered=True)
            publisher = registered.get('registration') or {}
            status = self.driver.status('gateway')
            controller = publisher.get('controller_instance_id')
            if not isinstance(controller, str) or not controller:
                raise ControlError('IDENTITY_MISMATCH', '执行端登记缺少目标 SONIC 实例')
            native = status.get('native') or {}
            if native.get('controller_instance_id') not in (None, controller):
                raise ControlError('CONTROLLER_INSTANCE_CHANGED', '执行端登记与网关目标实例不符')
            binding = dict(robot_serial=self.robot, control_resource=self.resource, action_endpoint=self.endpoint,
                control_session_id=row['control_session_id'], control_authorization=self.reference(row),
                source_id=row['target'], gateway_instance_id=status['gateway_instance_id'],
                controller_instance_id=controller, hold_reference_id=None,
                publisher_instance_id=publisher['publisher_instance_id'], input_session_id=publisher['input_session_id'])
            if vla:
                if any(publisher.get(k) != binding[k] for k in
                       ('robot_serial','control_resource','control_session_id','control_authorization','source_id','gateway_instance_id')):
                    raise ControlError('IDENTITY_MISMATCH', 'VLA 登记与本次控制请求不符')
                binding.update(action_endpoint=publisher['action_endpoint'], o6_instance_id=publisher['o6_instance_id'],
                               hold_reference_id=publisher['hold_reference_id'])
                if (status.get('o6') or {}).get('receiver_instance_id') != binding['o6_instance_id']:
                    raise ControlError('IDENTITY_MISMATCH', 'VLA 登记与网关手部实例不符')
            with self.store.writer():
                state = self.store.read()
                current = state['leases'][row['lease_id']]
                # Preserve late registration as evidence, but never grant after cancellation.
                current['gateway_binding'] = binding
                self._save(state)
                if current.get('revoked'):
                    raise ControlError('CONTROL_REVOKED', '登记返回前已取消')
            self._permit(row['lease_id'], 'grant')
            prepared = self.exchange(*args)
            if vla:
                if prepared['registration'] != registered['registration']:
                    raise ControlError('IDENTITY_MISMATCH', 'VLA 准备完成的登记身份改变')
                self.driver.verify_current_permit(dict(row, gateway_binding=binding),
                    prepared.get('current_observation') or {}, 'prepare', require_hold=True)
            else:
                self.driver.require(prepared, 'mode_ready', 'hold_verified', 'navigation_not_started')
                facts = prepared.get('evidence') or {}
                self.driver.require(facts, 'fresh_preparation_output')
                if facts.get('hand_participation') != 'not_required' or facts.get('hold_scope') != 'body':
                    raise ControlError('HAND_POLICY_MISMATCH', 'NAV prepare 未明确核验身体独立模式')
                if any(facts.get('publisher', {}).get(k) != binding[k] for k in ('publisher_instance_id','input_session_id')):
                    raise ControlError('IDENTITY_MISMATCH', '准备完成的 publisher/session 改变')
            self._permit(row['lease_id'], 'allow_execute')
            if vla:
                self.driver.verify_current_permit(dict(row, gateway_binding=binding),
                    self.driver.status('gateway'), 'execute', require_hold=True)
            with self.store.writer():
                state = self.store.read()
                current = state['leases'][row['lease_id']]
                self._eligible(current['request'])
                self._require_live(current, 'execute')
                evidence = dict(prepared=True, **{row['target']: prepared}, controller_instance_id=binding['controller_instance_id'])
                current.update(state='authorized', evidence=evidence)
                state['requests'][request_id]['receipt'].update(state='authorized', evidence=evidence,
                    authority_id=current['authority_id'], ownership_epoch=current['ownership_epoch'])
                self._save(state)
        except Exception as exc:
            uncertain = (getattr(exc, 'code', None) in {'HANDOFF_UNCONFIRMED','CONTROL_STATE_UNKNOWN'}
                         or exc.__class__.__name__ == 'HttpContractError')
            if not uncertain:
                self._mark_revoked(row['lease_id'], exc)
            with self.store.writer():
                state = self.store.read()
                current = state['leases'][row['lease_id']]
                if uncertain:
                    current.update(state='recovery_required', observation_error=self._error(exc))
                state['requests'][request_id]['receipt'].update(state='unconfirmed', error=self._error(exc))
                if not state['requests'][request_id]['children']:
                    current.update(state='released', evidence=dict(exit_confirmed=True, not_acquired=True,
                        cleanup=True, business_not_submitted=True, physical_readiness_verified=False))
                    if state['current'] == row['lease_id']:
                        state['current'] = None
                self._save(state)
        finally:
            with self.store.writer():
                state = self.store.read()
                if state['busy'] == request_id:
                    state['busy'] = None
                    self._save(state)
                needs_exit = state['leases'][row['lease_id']].get('revoked') and state['leases'][row['lease_id']]['state'] != 'released'
            if needs_exit and not self._closed:
                self._schedule_exit(row['lease_id'])

    @staticmethod
    def _error(exc):
        return deepcopy(exc.response()['error']) if isinstance(exc, ControlError) else dict(
            code='CONTROL_UNCONFIRMED', message=str(exc))

    def _mark_revoked(self, lease, error=None, *, cancel_business=True):
        with self.store.writer():
            state = self.store.read()
            row = state['leases'][lease]
            if row['state'] == 'released':
                return
            row.update(revoked=True, state='recovery_required')
            row['cancel_business'] = row.get('cancel_business', False) or cancel_business
            if error:
                row['cleanup_error'] = self._error(error)
                row.setdefault('revocation_reason', self._error(error))
                row.setdefault('revoked_at', stamp())
            if state['current'] is None and not row.get('task_cancelled'):
                state['current'] = lease
            self._save(state)

    def _schedule_exit(self, lease, new_exit_check=False):
        with self._cleanup_lock:
            future = self._cleanup_futures.get(lease)
            if future and not future.done():
                return future
            if self.executor is None:
                raise ControlError('CONTROL_WORKER_UNAVAILABLE', '控制收尾工作器不可用')
            future = self.executor.submit(self._cleanup, lease, new_exit_check)
            self._cleanup_futures[lease] = self._cleanup_future = future
            return future

    def cancel_task(self, task_id):
        """Detach the cancelled Runtime task immediately; retire its remote session separately."""
        from brain.storage.tasks import load_existing
        self.start()
        targets = []
        with self.store.writer():
            state, task = self.store.read(), load_existing(self.root) or {}
            if task.get('task_id') != task_id or task.get('state') != 'cancelled':
                raise ControlError('TASK_NOT_CANCELLED', '先由 Runtime 取消指定任务')
            for lease, row in state['leases'].items():
                if (row.get('task_id') != task_id and state['current'] != lease) or row['state'] == 'released':
                    continue
                if row.get('task_cancelled'):
                    continue  # Repeated button presses do not restart cleanup effects.
                row.update(task_cancelled=True, cancelled_by_task_id=task_id, revoked=True, state='recovery_required', cancel_business=True,
                           detached_at=stamp(), physical_exit_confirmed=False)
                targets.append(lease)
                if state['current'] == lease:
                    state['current'] = None
                busy = state['requests'].get(state.get('busy')) or {}
                if busy.get('body', {}).get('lease_id') == lease:
                    state['busy'] = None
            self._save(state)
        for lease in targets:
            try:
                self._schedule_exit(lease)
            except Exception as exc:
                # Cancellation remains durable even if a background worker cannot start.
                self._mark_revoked(lease, exc)
        return self.recovery_status()

    def request_cleanup(self, task_id, expected_lease_id, *, new_exit_check=False):
        from brain.storage.tasks import load_existing
        self.start()
        if type(new_exit_check) is not bool:
            raise ControlError('INVALID_REQUEST', 'new_exit_check 必须为布尔值', 400)
        with self.store.writer():
            state, task = self.store.read(), load_existing(self.root) or {}
            if task.get('task_id') != task_id or task.get('state') not in {
                    'cancelled','failed','succeeded','recovery_required','paused'}:
                raise ControlError('TASK_NOT_QUIESCENT', '先取消或暂停原任务')
            if state['current'] != expected_lease_id or not expected_lease_id:
                raise ControlError('STALE_AUTHORIZATION', '旧控制目标已变化')
        self._mark_revoked(expected_lease_id)
        self._schedule_exit(expected_lease_id, new_exit_check)
        return self.recovery_status()

    def release_control(self, task_id, command_id):
        """Explicit whole-session release, distinct from cancelling one command."""
        row = self.command_row(task_id, command_id)
        if row['state'] == 'released':
            return row['evidence']
        with self.store.writer():
            current = self.store.read()['leases'][row['lease_id']]
            if current.get('command_id') != command_id or current.get('task_id') != task_id:
                raise ControlError('STALE_AUTHORIZATION', '旧业务命令不能释放新命令的控制会话')
        self._mark_revoked(row['lease_id'])
        self._schedule_exit(row['lease_id']).result()
        with self.store.writer():
            row = self.store.read()['leases'][row['lease_id']]
            if row['state'] != 'released':
                raise ControlError('EXIT_UNCONFIRMED', str(row.get('cleanup_error')))
            return deepcopy(row['evidence'])

    def cancel_navigation(self, task_id, command_id):
        row = self.command_row(task_id, command_id)
        rid = uuid.uuid5(uuid.NAMESPACE_URL, row['lease_id']+':cancel:'+command_id).hex
        with self.store.writer():
            state = self.store.read()
            if rid not in state['requests']:
                state['requests'][rid] = dict(body=dict(self.identity(row), action='cancel_command'),
                    receipt=dict(state='pending', request_id=rid), children={})
                self._save(state)
        result = self.driver.cancel_navigation(row, lambda *args:self.exchange(rid,*args))
        with self.store.writer():
            state = self.store.read()
            state['requests'][rid]['receipt'] = deepcopy(result)
            self._save(state)
        # A stale cancel completes without affecting the newer active command.
        if result.get('stale_command') is True:
            return result
        self.settle_navigation(task_id, command_id)
        return result

    def settle_navigation(self, task_id, command_id, raw=None):
        row = self.command_row(task_id, command_id)
        if row['state'] == 'released':
            return row['evidence']
        proof = self.driver.command_hold(row, raw) if raw is not None else self.driver.navigation_finished(row)
        with self.store.writer():
            state = self.store.read()
            current = state['leases'][row['lease_id']]
            command = current['commands'][command_id]
            command.update(state='held', hold_evidence=proof)
            if current.get('active_command_id') == command_id:
                current['active_command_id'] = None
            self._save(state)
        return proof

    def _cleanup(self, lease, new_exit_check=False):
        rid = uuid.uuid5(uuid.NAMESPACE_URL, lease + (':current-exit' if new_exit_check else ':exit')).hex
        try:
            with self.store.writer():
                state = self.store.read()
                row = deepcopy(state['leases'][lease])
                if (state['current'] != lease and not row.get('task_cancelled')) or row['state'] == 'released':
                    return
                if row.get('legacy_control'):
                    raise ControlError('LEGACY_EXIT_EVIDENCE_REQUIRED',
                        '旧链路占用已保留，需关联原命令的当前退出证明；不调用旧 relay，也不将迁移当作释放')
                had_exit_request = any(o['body'].get('lease_id') == lease and o['body'].get('action') == 'exit'
                                       for o in state['requests'].values())
                if rid not in state['requests']:
                    state['requests'][rid] = dict(body=dict(self.identity(row), action='exit'),
                        receipt=dict(state='pending', request_id=rid), children={})
                    self._save(state)
            if row.get('gateway_binding') and (had_exit_request or row['authority_id'] != self.authority_id):
                # Reconcile a completed exit before addressing an old instance.
                # This is an explicit recovery request, not an effectful status poll.
                try:
                    evidence = self.driver.verify_exit(row, {'observation_only': True}, self.driver.status('gateway'))
                except Exception:
                    pass  # No proof: preserve the normal targeted stop/revoke path.
                else:
                    self._complete_exit(lease, rid, evidence)
                    return
            exchange = lambda *args: self.exchange(rid, *args)
            def stop_source():
                return (self.driver.yield_vla if row['target'] == 'vla' else self.driver.yield_navigation)(row, exchange)
            stop = self._exit_pool.submit(stop_source)
            revoke = self._exit_pool.submit(self._permit, lease, 'revoke', request_id=rid+'-revoke')
            source = None
            try:
                revoke.result()
            finally:
                try:
                    source = stop.result()
                except Exception:
                    pass  # The receive-side proof below remains mandatory.
            with self.store.writer():
                state = self.store.read()
                row = deepcopy(state['leases'][lease])
            binding = row.get('gateway_binding')
            if not binding:
                # No grant intent means this session could never enter the gateway.
                # A real source yield plus current isolated/held receiver is still required.
                with self.store.writer():
                    state = self.store.read()
                    granted = any(op['body'].get('lease_id') == lease and op['body']['action'] == 'grant'
                                  for op in state['requests'].values())
                if granted or not source:
                    raise ControlError('EXIT_UNCONFIRMED', '登记未核清，保留旧请求并等待会话／退出证明')
                status = self.driver.status('gateway')
                self.driver.native_facts(status, isolated=True, vla=row['target'] == 'vla')
                if row['target'] == 'vla':
                    raise ControlError('EXIT_UNCONFIRMED', '缺少 VLA 登记身份，无法关联手部退出证明')
                if 'permit' not in status or status['permit'] is not None:
                    raise ControlError('EXIT_UNCONFIRMED', '当前网关仍有许可')
                evidence = dict(exit_confirmed=True, hold_verified=True, source_output_fenced=True,
                    navigation=source, current_gateway=status,
                    scope='NAV source yielded; journal proves grant never issued; target revoked')
            else:
                payload = {k:binding[k] for k in GATEWAY_BINDING}
                if row['target'] == 'vla':
                    payload.update(o6_instance_id=binding['o6_instance_id'],
                                   hold_reference_id=binding['hold_reference_id'])
                payload.update(contract_version=VERSION, coordination_revision=REVISION, permit_mode=PERMIT_MODE, operation='fence')
                try:
                    fence = exchange('gateway-fence', 'gateway', '/v1/body-input/operations', payload,
                                     '/v1/body-input/requests/{request_id}')
                except ControlError as exc:
                    # Keep the failed/unknown operation intact; a separately
                    # verified current exit may still retire this occupancy.
                    fence = dict(original_outcome='unknown', error=self._error(exc))
                evidence = self.driver.wait_exit(row, fence, source)
            self._complete_exit(lease, rid, evidence)
        except Exception as exc:
            with self.store.writer():
                state = self.store.read()
                if state.get('authority_id') != self.authority_id:
                    return
                row = state['leases'][lease]
                if row['state'] != 'released':
                    row['cleanup_error'] = self._error(exc)
                    if rid in state['requests']:
                        state['requests'][rid]['receipt'].update(state='unknown', error=self._error(exc))
                    self._save(state)


    def _complete_exit(self, lease, rid, evidence):
        # Only this session's prepare can delay its release, never a newer task.
        deadline = time.monotonic() + self.driver.timeout
        while True:
            with self.store.writer():
                state = self.store.read()
                busy = (state['requests'].get(state.get('busy')) or {}).get('body', {}).get('lease_id') == lease
            if not busy or self._closed or time.monotonic() >= deadline:
                break
            time.sleep(.01)
        with self.store.writer():
            state = self.store.read()
            row = state['leases'][lease]
            busy = (state['requests'].get(state.get('busy')) or {}).get('body', {}).get('lease_id') == lease
            if busy or self._closed or (state['current'] != lease and not row.get('task_cancelled')):
                raise ControlError('CONTROL_BUSY', '本会话准备仍在收尾或实例已变化')
            evidence.update(cleanup=True, business_not_submitted='business_submission' not in row)
            row.update(state='released', evidence=evidence, physical_exit_confirmed=True)
            row.pop('cleanup_error', None)
            if state['current'] == lease:
                state['current'] = None
            state['requests'][rid]['receipt'].update(state='released', evidence=evidence)
            self._save(state)
