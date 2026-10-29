"""Source selection and cancellation in the existing control journal.

Runtime owns tasks. This adapter only journals peer I/O; no background replay,
physical readiness checks, leases or second task scheduler.
"""
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import threading
import time
import uuid

from brain.adapters.source_gate import SourceGateClient
from brain.adapters.dream_client import DreamClient
from brain.adapters.vla_client import VlaClient
from brain.adapters.execution import interpret_observation
from brain.adapters.http_client import HttpContractError
from brain.storage.body_control import ControlStore
from brain.storage.tasks import load_existing
from contracts.body_control import ControlError
from contracts.vla import NATIVE_OPERATIONS
from contracts.vla_direct import business_request


class SourceGateControl:
    mode = 'source_gate'

    def __init__(self, config, root, executor, *, client=None):
        self.config, self.root, self.executor = config, Path(root), executor
        self.settings = config['reception_real']['body_control']
        self.client = client or SourceGateClient(self.settings)
        self.store = ControlStore(root)
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._futures = {}
        self._fd = None
        self._closed = False
        self.timeout = float(self.settings.get('operation_timeout_sec', 10))
        self.poll = float(self.settings.get('poll_interval_sec', .05))
        if not (0 < self.timeout <= 60 and 0 < self.poll <= self.timeout):
            raise ValueError('Invalid source gate timeouts')
        self.binding = {'gateway': self.settings['gateway_base_url'],
                        'dream': config['reception_real']['dream_base_url']}

    def start(self):
        with self._start_lock:
            if self._fd is not None:
                return
            if self._closed:
                raise ControlError('CONTROL_CLOSED', '接收源管理已关闭')
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.root / 'body_control.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.store.writer():
                    state = self.store.read() or dict(schema=3, epoch=0, revision=0, leases={}, requests={})
                    gate = state.setdefault('source_gate', dict(binding=self.binding, commands={}, tasks={}, operations={}))
                    if gate['binding'] != self.binding:
                        raise ControlError('CONTROL_TARGET_CHANGED', '原接收源管理端点已变化')
                    # Preserve all legacy leases, requests and unknown outcomes unchanged.
                    self.store.write(state)
                self._fd = fd
            except Exception:
                os.close(fd)
                raise

    def _read(self):
        self.start()
        with self.store.writer():
            return deepcopy(self.store.read()['source_gate'])

    def _update(self, change):
        self.start()
        with self.store.writer():
            state = self.store.read()
            change(state['source_gate'])
            state['revision'] = state.get('revision', 0) + 1
            self.store.write(state)

    @staticmethod
    def _key(request):
        body = request['body']
        return uuid.uuid5(uuid.NAMESPACE_URL, body['task_id'] + ':' + body['command_id']).hex

    def _eligible(self, request, *, effect=False):
        record = load_existing(self.root) or {}
        body = request['body']
        target = record.get('execution_target') or {}
        if (self._closed or record.get('task_id') != body['task_id']
                or record.get('open_command_id') != body['command_id']
                or record.get('dispatch_closed') or record.get('control_request')
                or record.get('state') not in ({'running', 'verifying', 'waiting_human'} if effect else {'running', 'verifying'})
                or record.get('execution_backend') not in {'body_real', 'reception_real', 'reception_real_sim_manipulation', 'reception_sim_navigation_real'}
                or (target.get('body_control') or {}).get('mode') != self.mode
                or (target.get('body_control') or {}).get('gateway_base_url') != self.binding['gateway']
                or self.config['reception_real'].get('kernel_enabled') is not True):
            raise ControlError('NOT_SCHEDULED', '当前 Runtime 未允许此执行命令')
        nav = request.get('skill') == 'navigate'
        if not nav and request.get('skill') not in NATIVE_OPERATIONS:
            raise ControlError('UNSUPPORTED_CONTROL_SKILL', '只支持导航、开门和抓可乐')
        endpoint = 'dream' if nav else 'vla'
        if target.get(endpoint) != self.config['reception_real'][endpoint + '_base_url']:
            raise ControlError('CONTROL_TARGET_CHANGED', '执行端与任务绑定地址不符')

    def check_effect(self, request):
        with self.store.writer():
            self._eligible(request, effect=True)

    def was_submitted(self, request):
        return bool(self._read()['commands'].get(self._key(request), {}).get('submitted'))

    def _observe_operation(self, key, *, first=None):
        op = self._read()['operations'][key]
        body = op['request']
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                result = first if first is not None else self.client.query(body['request_id'])
                first = None
                self._update(lambda g: g['operations'][key].update(receipt=deepcopy(result)))
                if (result.get('request_id') != body['request_id'] or result.get('source') != body['source']
                        or result.get('receiver_instance_id') != body['receiver_instance_id']):
                    raise ControlError('SOURCE_GATE_IDENTITY', '选源回执身份不匹配')
                if result.get('state') == 'applied':
                    if (result.get('effective_source') != body['source']
                            or type(result.get('revision')) is not int or result['revision'] < 0):
                        raise ControlError('SOURCE_GATE_STATE', '选源生效回执无效')
                    self._update(lambda g: g['operations'][key].update(resolved=True))
                    return result
                if result.get('state') in {'failed', 'rejected'}:
                    self._update(lambda g: g['operations'][key].update(resolved=True))
                    raise ControlError('SOURCE_GATE_REJECTED', 'SONIC 未应用选源', details=result)
            except HttpContractError as exc:
                self._update(lambda g: g['operations'][key].update(error=str(exc)))
            if time.monotonic() >= deadline:
                raise ControlError('SOURCE_GATE_UNKNOWN', '选源结果未确认；保留原 request_id，仅查询不重发')
            time.sleep(self.poll)

    def _select(self, source, key):
        current = self.client.current()
        # A delayed old selection must not overtake a newer close/select operation.
        for previous_key, op in self._read()['operations'].items():
            if op.get('resolved') or op.get('retired_receiver'):
                continue
            if op['request']['receiver_instance_id'] != current['receiver_instance_id']:
                self._update(lambda g: g['operations'][previous_key].update(retired_receiver=True))
                continue  # Preserve the unknown old receipt; never replay into the new receiver.
            self._observe_operation(previous_key)
        current = self.client.current()
        if current['pending_requests']:
            raise ControlError('SOURCE_GATE_PENDING', 'SONIC 仍有未完成选源请求')
        self._update(lambda g: g.update(observation=deepcopy(current)))
        op = self._read()['operations'].get(key)
        if op:
            receipt = op.get('receipt') or {}
            if (not op.get('resolved') or receipt.get('state') != 'applied'
                    or receipt.get('receiver_instance_id') != current['receiver_instance_id']
                    or current['effective_source'] != source or current['revision'] != receipt.get('revision')):
                raise ControlError('SOURCE_GATE_CHANGED', '原选源结果不代表当前状态；未重放旧操作')
            return current
        if current['effective_source'] == source:
            return current  # Same source: no PUT, no reset, no reconnect.
        body = dict(request_id=uuid.uuid4().hex, source=source,
                    receiver_instance_id=current['receiver_instance_id'])
        self._update(lambda g: g['operations'].__setitem__(key, dict(request=body, resolved=False)))
        try:
            first = self.client.set(body)
        except HttpContractError as exc:
            self._update(lambda g: g['operations'][key].update(error=str(exc)))
            first = None
        receipt = self._observe_operation(key, first=first)
        current = self.client.current()
        if (current['receiver_instance_id'] != receipt['receiver_instance_id']
                or current['effective_source'] != source or current['revision'] != receipt['revision']
                or current['pending_requests']):
            raise ControlError('SOURCE_GATE_CHANGED', '选源回执之后 SONIC 状态已变化')
        self._update(lambda g: g.update(observation=deepcopy(current)))
        return current

    def submit_navigation(self, dream, request):
        if request.get('skill') != 'navigate':
            raise ControlError('NAV_ONLY', '导航适配器不能发送操控动作')
        return self._submit(dream, request, 'navigation')

    def submit_vla(self, vla, request):
        if request.get('skill') not in NATIVE_OPERATIONS:
            raise ControlError('UNSUPPORTED_CONTROL_SKILL', '直连 VLA 动作未支持')
        return self._submit(vla, request, 'vla')

    def _submit(self, client, request, source):
        key = self._key(request)
        query = client.command if source == 'navigation' else client.task
        wire = request['body'] if source == 'navigation' else business_request(request['body'])
        endpoint = self.config['reception_real']['dream_base_url' if source == 'navigation' else 'vla_base_url']
        with self._lock:
            with self.store.writer():
                self._eligible(request)
            previous = self._read()['commands'].get(key)
            if previous and previous['request'] != request:
                raise ControlError('REQUEST_CONFLICT', '原导航命令请求不能变化')
            if self.was_submitted(request):
                return query(request['body']['command_id'])
            # Historical cancellation outcomes are evidence, not admission gates.
            # The current receiver decides whether the new selection is applied;
            # NAV decides whether it can accept this new business command.
            selected = self._select(source, ('nav:' if source == 'navigation' else 'vla:') + key)
            with self.store.writer():
                self._eligible(request)  # Cancellation during GET/PUT cannot dispatch a late goal.
                self._update(lambda g: g['commands'].__setitem__(key,
                    dict(request=deepcopy(request), submitted=True, selection=deepcopy(selected),
                         source=source, endpoint=endpoint, wire_body=deepcopy(wire))))
            # Exact original business body: never attach joint-control fields.
            raw = client.submit_navigation(wire) if source == 'navigation' else client.submit_task(wire)
            self._update(lambda g: g['commands'][key].update(submission=deepcopy(raw)))
            return raw

    def cancel_navigation(self, dream, request):
        return self._cancel_command(dream, request)

    def cancel_vla(self, vla, request):
        return self._cancel_command(vla, request, direct=True)

    def _cancel_command(self, client, request, *, direct=False):
        key = self._key(request)
        with self._lock:
            row = self._read()['commands'].get(key)
            if not row or not row.get('submitted'):
                return {'stopped': True, 'not_submitted': True}
            if not row.get('cancel_sent'):
                self._update(lambda g: g['commands'][key].update(cancel_sent=True))
                try:
                    raw = client.cancel(request['body']['task_id'], request['body']['command_id'])
                    self._update(lambda g: g['commands'][key].update(cancel_receipt=deepcopy(raw)))
                except HttpContractError as exc:
                    self._update(lambda g: g['commands'][key].update(cancel_error=str(exc)))
            raw = client.task(request['body']['command_id']) if direct else client.command(request['body']['command_id'])
            viewed = interpret_observation(request['body']['command_id'], raw, request,
                                           vla_execution_mode='direct' if direct else None)
            stopped = (viewed.get('terminal') in {'succeeded', 'failed', 'cancelled'}
                       and viewed.get('stopped') is True
                       and (viewed.get('evidence') or {}).get('identity_ok') is True
                       and (viewed.get('evidence') or {}).get('time_ok') is True)
            self._update(lambda g: g['commands'][key].update(stop_observation=deepcopy(raw), stopped=stopped))
            return dict(stopped=stopped, raw=raw)

    def cancel_task(self, task_id):
        self.start()
        with self.store.writer():
            gate = self._read()
            if task_id not in gate['tasks']:
                record = load_existing(self.root) or {}
                if record.get('task_id') != task_id or record.get('state') != 'cancelled':
                    raise ControlError('NOT_CANCELLED', '只为原 Runtime 已取消任务收尾')
                # No gate changes for a task that never bound this execution mode.
                if (record.get('execution_target', {}).get('body_control') or {}).get('mode') != self.mode:
                    return self.recovery_status()
                self._update(lambda g: g['tasks'].__setitem__(task_id, dict(task_id=task_id, completed=False)))
            if self._read()['tasks'][task_id].get('completed'):
                return self.recovery_status()
            if task_id not in self._futures or self._futures[task_id].done():
                self._futures[task_id] = self.executor.submit(self._cleanup, task_id)
        return self.recovery_status()

    def _cleanup(self, task_id):
        with self._lock:
            # A queued or explicitly rechecked old cleanup must never cancel or
            # close inputs after another task has taken over. This shares the
            # submit lock, so an already-running cleanup finishes before a new
            # goal can be sent; late jobs only update the old journal entry.
            record = load_existing(self.root) or {}
            if record.get('task_id') != task_id or record.get('state') != 'cancelled':
                self._update(lambda g: g['tasks'][task_id].update(cleanup_superseded=True))
                return
            real = self.config['reception_real']
            dream = DreamClient(real['dream_base_url'], contract_version=real.get('contract_version', 'fq/reception-lan/v1'),
                                request_timeout_sec=float(real.get('request_timeout_sec', 10)))
            dream.http.auth_token_env = self.settings.get('nav_token_env') or self.settings.get('token_env', 'BRAIN_CONTROL_TOKEN')
            errors, stopped = [], True
            source_stops, seen_sources = {}, set()
            # Source-specific cancel remains necessary; closing the gate is not a stop receipt.
            for row in self._read()['commands'].values():
                if row['request']['body']['task_id'] != task_id:
                    continue
                direct = row['request']['skill'] in NATIVE_OPERATIONS
                source = 'vla' if direct else 'navigation'
                seen_sources.add(source)
                try:
                    if direct:
                        # Use the recorded VLA endpoint; never redirect an old cancel.
                        client = VlaClient(row['endpoint'], contract_version=real.get('contract_version', 'fq/reception-lan/v1'),
                                           request_timeout_sec=float(real.get('request_timeout_sec', 10)))
                        client.http.auth_token_env = self.settings.get('vla_token_env') or self.settings.get('token_env', 'BRAIN_CONTROL_TOKEN')
                        deadline = time.monotonic() + self.timeout
                        outcome = self.cancel_vla(client, row['request'])
                        while not outcome['stopped'] and time.monotonic() < deadline:
                            time.sleep(self.poll)
                            outcome = self.cancel_vla(client, row['request'])  # GET only after the original cancel.
                    else:
                        outcome = self.cancel_navigation(dream, row['request'])
                    stopped = outcome['stopped'] and stopped
                    source_stops[source] = outcome['stopped'] and source_stops.get(source, True)
                except (HttpContractError, ControlError) as exc:
                    errors.append(str(exc))
                    stopped = False
                    source_stops[source] = False
            closed = False
            try:
                self._select('none', 'cancel:' + task_id)
                closed = True
            except (HttpContractError, ControlError) as exc:
                errors.append(str(exc))
            self._update(lambda g: g['tasks'][task_id].update(completed=closed and stopped,
                gate_closed=closed, navigation_stopped=source_stops.get('navigation'),
                vla_stopped=source_stops.get('vla'),
                navigation_not_submitted='navigation' not in seen_sources,
                vla_not_submitted='vla' not in seen_sources,
                error='；'.join(errors) or ('' if stopped else '原执行命令停止结果未确认')))

    def request_cleanup(self, task_id, expected_lease_id, *, new_exit_check=False):
        if expected_lease_id != task_id or task_id not in self._read()['tasks']:
            raise ControlError('IDENTITY_MISMATCH', '收尾须指定原任务标识')
        return self.cancel_task(task_id)  # Existing request IDs only; no repeat PUT/cancel.

    def assert_new_task_allowed(self):
        self.start()  # Local publication is separate from downstream dispatch readiness.

    def recovery_status(self):
        gate = self._read()
        unresolved = [dict(row, lease_id=task, cleanup_error=row.get('error', ''))
                   for task, row in gate['tasks'].items() if not row.get('completed')]
        current = (load_existing(self.root) or {}).get('task_id')
        pending = [row for row in unresolved if row['task_id'] == current and not row.get('cleanup_superseded')]
        return dict(enabled=True, mode=self.mode, blocked=False, occupied=False, pending=bool(pending),
                    unresolved=unresolved, observation=gate.get('observation'),
                    supported_sources=['navigation', 'vla'], vla_contract_revision='20261028-direct-v1',
                    physical_readiness_verified=False, previous_exit_unconfirmed=len(unresolved))

    def stop_accepting(self):
        self._closed = True

    def close(self):
        self.stop_accepting()
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
