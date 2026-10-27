"""Single brain authority for physical handoff; Runtime remains task authority.

Mutations are durable before I/O. Historical GETs never run a transition. Robot
operations are injected, and startup never guesses an old owner's physical exit.
"""
from copy import deepcopy
import fcntl
import os
import threading
import time
import uuid
from pathlib import Path

from contracts.body_control import (VERSION, REVISION, IDENTITY, ControlError, digest,
                                    encode, identifier, lease_identity, stamp, nav_prepare_refused)
from brain.storage.body_control import ControlStore


class BodyControl:
    def __init__(self, config, root, driver, executor=None):
        self.config, self.root, self.driver, self.executor = config, Path(root), driver, executor
        real = config.get('reception_real') or {}
        self.settings = real.get('body_control') or {}
        self.robot = self.settings['robot_serial']
        self.endpoint = self.settings['action_endpoint']
        self.binding = dict(robot_serial=self.robot, action_endpoint=self.endpoint,
                            dream=real['dream_base_url'], vla=real['vla_base_url'],
                            relay=self.settings['relay_base_url'])
        self.authority_id = uuid.uuid4().hex
        self.store = None
        self._fd = None
        self._start_lock = threading.Lock()
        self._closed = False
        self._cleanup_lock = threading.Lock()
        self._cleanup_future = None

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
                        state = dict(schema=1, binding=self.binding, epoch=0, revision=0,
                                     current=None, busy=None, leases={}, requests={})
                    if state['binding'] != self.binding:
                        raise ControlError('CONTROL_TARGET_CHANGED', '控制端点改变，须先核清原控制记录')
                    # Leave all source identities and receipts intact. No replay.
                    for row in state['leases'].values():
                        if row['state'] not in {'released', 'registered'}:
                            row['state'] = 'recovery_required'
                    for op in state['requests'].values():
                        if op['receipt']['state'] == 'pending':
                            op['receipt'].update(state='unconfirmed', error={'code': 'AUTHORITY_RESTARTED',
                                                                          'message': '重启后仅查询原操作'})
                    state.update(authority_id=self.authority_id, busy=None)
                    # Older versions reserved a NAV lease even when the first
                    # idle observation failed before publishing any grant.
                    for row in state['leases'].values():
                        if self._legacy_unissued_nav(state, row):
                            self._retire_unissued_nav(state, row)
                    state['revision'] += 1
                    store.write(state)
                self._fd, self.store = fd, store
            except Exception:
                os.close(fd)
                raise

    def close(self):
        with self._start_lock:
            self._closed = True
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = None

    def stop_accepting(self):
        self._closed = True

    def _save(self, state):
        if state.get('authority_id') != self.authority_id:
            raise ControlError('STALE_AUTHORITY', '大脑控制实例已变化，未写入迟到结果')
        state['revision'] += 1
        state['updated_at'] = stamp()
        self.store.write(state)

    @staticmethod
    def _legacy_unissued_nav(state, row):
        if (row['state'] == 'released' or row['target'] != 'navigation'
                or row.get('grant_published') is True or 'business_submission' in row):
            return False
        ops = [op for op in state['requests'].values() if op['body'].get('lease_id') == row['lease_id']]
        acquires = [op for op in ops if op['body']['action'] == 'acquire']
        if (len(acquires) != 1 or acquires[0]['children']
                or acquires[0]['receipt'].get('state') != 'unconfirmed'
                or (acquires[0]['receipt'].get('error') or {}).get('code') != 'STALE_EVIDENCE'):
            return False
        # STALE_EVIDENCE with no acquire children can only come from idle(),
        # before the grant/current transition. Preserve any failed stop-only check.
        return all(op['body']['action'] == 'acquire' or (
            op['body']['action'] == 'release' and set(op['children']) <= {'nav-yield'}
            and all(child.get('state') != 'confirmed' for child in op['children'].values())) for op in ops)

    @staticmethod
    def _retire_unissued_nav(state, row):
        row.update(state='released', updated_at=stamp(), evidence=dict(exit_confirmed=True,
            not_acquired=True, cleanup=True, business_not_submitted=True, physical_readiness_verified=False,
            scope='local NAV intent retired: failed before grant or peer writes; original failure preserved'))
        row.pop('cleanup_error', None)
        if state['current'] == row['lease_id']:
            state['current'] = None

    def _eligible(self, request):
        """Called under the same directory writer lock as Runtime cancellation."""
        from brain.storage.tasks import load_existing
        record = load_existing(self.root) or {}
        body = request['body']
        if (record.get('task_id') != body.get('task_id')
                or record.get('open_command_id') != body.get('command_id')
                or record.get('dispatch_closed') or record.get('control_request')
                or record.get('state') not in {'running', 'verifying', 'waiting_human'}
                or record.get('execution_backend') not in {'body_real', 'reception_real'}
                or (self.config.get('reception_real') or {}).get('kernel_enabled') is not True):
            raise ControlError('NOT_SCHEDULED', '当前 Runtime 未允许该命令申请控制')
        target = record.get('execution_target') or {}
        if target.get('dream') != self.binding['dream'] or target.get('vla') != self.binding['vla']:
            raise ControlError('CONTROL_TARGET_CHANGED', '任务绑定的执行端与控制端不符')
        for bucket in (record.get('steps') or {}).values():
            for attempt in bucket.get('attempts') or []:
                if attempt.get('command_id') == body['command_id'] and attempt.get('request') == request:
                    return
        raise ControlError('NOT_SCHEDULED', '请求与原 Runtime 尝试不一致')

    def register(self, request):
        self.start()
        request = deepcopy(request)
        body = request['body']
        if request.get('skill') not in {'navigate', 'open_fridge', 'grasp_cola'}:
            raise ControlError('UNSUPPORTED_CONTROL_SKILL', '联合控制只支持已登记的 NAV 与 native VLA')
        if not all(identifier(body.get(k)) for k in ('task_id', 'command_id')):
            raise ControlError('INVALID_IDENTITY', '任务／命令身份无效', 400)
        lease = lease_identity(self.robot, body['task_id'], body['command_id'])
        with self.store.writer():
            self._eligible(request)
            state = self.store.read()
            row = state['leases'].get(lease)
            if row:
                if row['request'] != request:
                    raise ControlError('REQUEST_CONFLICT', '同一命令的请求内容改变')
                return deepcopy(row)
            row = dict(contract_version=VERSION, coordination_revision=REVISION,
                       robot_serial=self.robot, action_endpoint=self.endpoint,
                       task_id=body['task_id'], command_id=body['command_id'], lease_id=lease,
                       authority_id=self.authority_id, ownership_epoch=None,
                       target='navigation' if request['skill'] == 'navigate' else 'vla',
                       state='registered', request=request, evidence={}, updated_at=stamp())
            state['leases'][lease] = row
            self._save(state)
            return deepcopy(row)

    def record_submission(self, request, wire_body):
        """Persist the exact business wire request, checking cancellation before dispatch."""
        self.start()
        body = request['body']
        lease = lease_identity(self.robot, body['task_id'], body['command_id'])
        with self.store.writer():
            self._eligible(request)
            state = self.store.read()
            row = state['leases'].get(lease)
            if row is None or row['authority_id'] != self.authority_id:
                raise ControlError('STALE_AUTHORIZATION', '派发没有本实例登记的授权')
            if 'business_submission' in row:
                raise ControlError('SUBMISSION_ALREADY_ATTEMPTED', '原业务提交已落盘，必须查询原命令')
            if row['target'] == 'navigation' and (
                    row['state'] != 'authorized' or state['current'] != lease
                    or row['evidence'].get('prepared') is not True):
                raise ControlError('NAVIGATION_NOT_PREPARED', '导航准备尚未核清')
            row['business_submission'] = {'body': deepcopy(wire_body), 'recorded_at': stamp()}
            self._save(state)

    def close_unacquired(self, request):
        """Seal a definitively refused VLA request before any acquire can race it."""
        self.start()
        body = request['body']
        lease = lease_identity(self.robot, body['task_id'], body['command_id'])
        with self.store.writer():
            state = self.store.read()
            row = state['leases'].get(lease)
            if row is None or row['state'] != 'registered':
                return False
            row.update(state='released', evidence={'exit_confirmed': True, 'not_acquired': True})
            self._save(state)
            return True

    @staticmethod
    def reference(row):
        return {key: row[key] for key in ('lease_id', 'authority_id', 'ownership_epoch')}

    def identity(self, row):
        return {key: row[key] for key in IDENTITY}

    def capabilities(self):
        self.start()
        return dict(contract_version=VERSION, coordination_revision=REVISION, authority='brain',
                    authority_id=self.authority_id, robot_serial=self.robot, action_endpoint=self.endpoint,
                    operations=['acquire', 'release', 'query'],
                    features=['brain-owned-control', 'release-without-navigation-restore',
                              'persistent-fenced-owner', 'async-query'], ready=True,
                    physical_readiness_verified=False)

    def recovery_status(self):
        """Local resource facts only; polling never performs a recovery."""
        if self.store is None:
            return dict(enabled=True, blocked=False, pending=False, source=None, unresolved=[])
        with self.store.writer():
            state = self.store.read()
            rows = [row for row in state['leases'].values() if row['state'] != 'released']
            fields = ('task_id','command_id','lease_id','target','state','ownership_epoch','cleanup_error')
            source = state['leases'].get(state['current'])
            return dict(enabled=True, blocked=bool(rows or state['busy']),
                pending=bool(state['busy'] or (self._cleanup_future and not self._cleanup_future.done())),
                source={key:deepcopy(source.get(key)) for key in fields} if source else None,
                unresolved=[{key:deepcopy(row.get(key)) for key in fields} for row in rows])

    def assert_new_task_allowed(self):
        self.start()
        status = self.recovery_status()
        if status['blocked']:
            source = status['source'] or (status['unresolved'] or [{}])[0]
            raise ControlError('SOURCE_NOT_RELEASED', '旧控制尚未核清退出：' +
                str(source.get('command_id') or '交接处理中') + '；请等待取消收尾或点击“核清旧控制”，不要重复发布')

    def request_cleanup(self, task_id, expected_lease_id, *, new_exit_check=False):
        """Explicit operator cleanup (also requested by task cancellation).

        Revoke first, then use the existing workers and original stop/yield IDs.
        Task state is untouched; historical receipts are never deleted.
        """
        from brain.storage.tasks import load_existing
        if type(new_exit_check) is not bool:
            raise ControlError('INVALID_REQUEST', 'new_exit_check 必须为布尔值', 400)
        self.start()
        with self._cleanup_lock:
            with self.store.writer():
                state = self.store.read()
                task = load_existing(self.root) or {}
                if (task.get('task_id') != task_id or task.get('state') not in
                        {'cancelled','failed','succeeded','recovery_required','paused'}):
                    raise ControlError('TASK_NOT_QUIESCENT', '请先取消或暂停当前任务，再核清旧控制')
                if state['current'] != expected_lease_id:
                    raise ControlError('STALE_AUTHORIZATION', '当前控制源已变化，请刷新后重查')
                if self._cleanup_future and not self._cleanup_future.done():
                    return self.recovery_status()
                leases = [key for key,row in state['leases'].items() if row['state'] != 'released']
                source = state['leases'].get(state['current'])
                if source:
                    source['state'] = 'recovery_required'  # A cancelled task cannot keep output permission.
                    source.pop('cleanup_error', None)
                    self._save(state)
            if self.executor is None:
                raise ControlError('CONTROL_WORKER_UNAVAILABLE', '控制收尾工作器不可用', 503)
            self._cleanup_future = self.executor.submit(self._cleanup, expected_lease_id, leases, new_exit_check)
        return self.recovery_status()

    def _seal_unused(self, state, leases):
        for key in leases:
            row = state['leases'][key]
            if key == state['current'] or row['state'] == 'released' or 'business_submission' in row:
                continue
            operations = [op for op in state['requests'].values() if op['body'].get('lease_id') == key]
            if all(op['body']['action'] == 'acquire' and not op['children']
                   and op['receipt'].get('state') == 'unconfirmed'
                   and (op['receipt'].get('error') or {}).get('code') == 'SOURCE_NOT_RELEASED' for op in operations):
                # A request rejected behind the old owner never issued any peer write.
                row.update(state='released', evidence=dict(exit_confirmed=True, not_acquired=True,
                    cleanup=True, business_not_submitted=True), updated_at=stamp())

    def _reserve_cleanup_release(self, row, new_exit_check=False):
        suffix = ':current-exit-release-v1' if new_exit_check else ':release'
        rid = uuid.uuid5(uuid.NAMESPACE_URL, row['lease_id'] + suffix).hex
        body = dict(self.identity(row), action='release', request_id=rid,
                    authority_id=row['authority_id'], ownership_epoch=row['ownership_epoch'])
        receipt, created = self._begin(body, 'navigation')
        with self.store.writer():
            state = self.store.read()
            if state['current'] != row['lease_id'] or self._closed:
                raise ControlError('STALE_AUTHORIZATION', '原控制源已变化，未继续收尾')
            if not created:
                if state['busy'] or receipt['state'] != 'unconfirmed':
                    raise ControlError('CONTROL_BUSY', '原退出请求仍在处理，请查询原请求')
                op = state['requests'][rid]
                op.setdefault('recovery_checks', []).append(deepcopy(op['receipt']))
                op['receipt'].update(state='pending', updated_at=stamp())
                state['busy'] = rid
                state['leases'][row['lease_id']]['state'] = 'releasing'
            state['requests'][rid]['cleanup'] = True
            if new_exit_check:
                state['requests'][rid]['current_exit_check'] = True
            self._save(state)
        return rid

    @staticmethod
    def _relay_only_prepare(state, row):
        """Journal proves NAV never accepted this preparation.

        A persisted child counts as possibly sent even without a reply. Older
        cleanup attempts may contain stop-only NAV yields; keep those intact.
        """
        if row['target'] != 'navigation' or 'business_submission' in row:
            return None
        ops = [op for op in state['requests'].values()
               if op['body'].get('lease_id') == row['lease_id']]
        acquires = [op for op in ops if op['body']['action'] == 'acquire']
        if len(acquires) != 1:
            return None
        children = acquires[0]['children']
        rejected_prepare = None
        if set(children) == {'relay-enable', 'nav-prepare'}:
            child = children['nav-prepare']
            if child.get('state') == 'confirmed':
                return None
            rejected_prepare = (child.get('observations') or {}).get('first_post')
            if not nav_prepare_refused(child['wire'], rejected_prepare):
                return None
        elif set(children) != {'relay-enable'}:
            return None
        if any(op['body']['action'] != 'acquire' and (
                op['body']['action'] != 'release'
                or set(op['children']) - {'nav-yield', 'relay-close'}) for op in ops):
            return None
        wire = deepcopy(children['relay-enable']['wire'])
        if rejected_prepare:
            wire['nav_prepare_rejection'] = deepcopy(rejected_prepare)
        return wire

    def _cleanup(self, lease, leases, new_exit_check=False):
        try:
            deadline = time.monotonic() + 30
            while True:
                with self.store.writer():
                    state = self.store.read()
                    busy = state['busy']
                if not busy:
                    break
                if self._closed or time.monotonic() >= deadline:
                    raise ControlError('CONTROL_BUSY', '原交接仍在收尾；稍后点击“核清旧控制”查询，不要重发任务')
                time.sleep(.05)
            with self.store.writer():
                state = self.store.read()
                row = deepcopy(state['leases'].get(lease))
                if state['current'] != lease:
                    if row and row['state'] == 'released':
                        row = None
                    else:
                        raise ControlError('STALE_AUTHORIZATION', '控制源变化，未操作新控制源')
            if row and row['target'] == 'navigation':
                rid = self._reserve_cleanup_release(row, new_exit_check)
                self._run(rid)
                with self.store.writer():
                    relay_only = self._relay_only_prepare(self.store.read(), row) is not None
                if (not self.released(row['task_id'], row['command_id'])
                        and 'business_submission' not in row and not relay_only and not new_exit_check):
                    # A failed prepare may not have an active NAV execution to yield.
                    # Retry only GETs for recorded effects, then reconcile current exit facts.
                    rid = self._reserve_cleanup_release(row)
                    try:
                        with self.store.writer():
                            state = self.store.read()
                            prepare = next((op['children']['nav-prepare']['wire'] for op in state['requests'].values()
                                if op['body'].get('lease_id') == lease and 'nav-prepare' in op['children']), None)
                        proof = self.driver.reconcile_navigation(row, prepare, lambda *args:self.exchange(rid, *args))
                        self._finish(rid, proof, None)
                    except Exception as exc:
                        self._finish(rid, None, exc)
                if not self.released(row['task_id'], row['command_id']):
                    receipt = self.query(lease, request_id=rid)
                    raise ControlError('SOURCE_NOT_RELEASED', str(receipt.get('error') or '原 NAV 退出未确认'))
            elif row:
                stopped = self.stop_vla(row['task_id'], row['command_id'])
                if not self.released(row['task_id'], row['command_id']):
                    raise ControlError('SOURCE_NOT_RELEASED', 'VLA 原停止／release 尚未核清：' + str(stopped.get('error') or stopped['state']))
            with self.store.writer():
                state = self.store.read()
                if state.get('authority_id') != self.authority_id:
                    return
                if lease and state['leases'][lease]['state'] == 'released':
                    ended = state['leases'][lease]
                    ended['evidence'].update(cleanup=True, business_not_submitted='business_submission' not in ended)
                    ended.pop('cleanup_error', None)
                self._seal_unused(state, leases)
                self._save(state)
        except Exception as exc:
            with self.store.writer():
                state = self.store.read()
                if state.get('authority_id') != self.authority_id:
                    return
                if lease in state['leases'] and state['leases'][lease]['state'] != 'released':
                    state['leases'][lease]['cleanup_error'] = dict(code=getattr(exc,'code','CONTROL_CLEANUP_UNCONFIRMED'),
                        message=str(exc), checked_at=stamp())
                    self._save(state)

    def business_not_submitted_after_cleanup(self, task_id, command_id):
        self.start()
        with self.store.writer():
            row = self.store.read()['leases'].get(lease_identity(self.robot, task_id, command_id))
            return bool(row and row['state'] == 'released' and 'business_submission' not in row
                        and row['evidence'].get('cleanup') is True
                        and row['evidence'].get('business_not_submitted') is True)

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
            owner = (active['target'] if active.get('state') == 'authorized'
                     and active.get('authority_id') == self.authority_id else 'unknown' if active else 'none')
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
                if target == 'vla' and body.get('navigation_proof') != row['request']['body'].get('navigation_proof'):
                    raise ControlError('IDENTITY_MISMATCH', '原导航证明与派发请求不一致')
                if target == 'vla' and 'business_submission' not in row:
                    raise ControlError('NOT_SCHEDULED', 'VLA 业务提交意图尚未落盘')
                state['epoch'] += 1
                row.update(ownership_epoch=state['epoch'], state='acquiring')
            else:
                if (state['current'] != row['lease_id'] or row['state'] not in {'authorized', 'recovery_required'}
                        or body.get('authority_id') != row['authority_id']
                        or type(body.get('ownership_epoch')) is not int
                        or body['ownership_epoch'] != row['ownership_epoch']):
                    raise ControlError('STALE_AUTHORIZATION', '只能核清对应的原授权')
                if target == 'vla':
                    exit_state = body.get('exit_state')
                    if (not isinstance(exit_state, dict) or body.get('policy_stopped') is not True
                            or body.get('exit_state_sha256') != digest(exit_state)):
                        raise ControlError('INVALID_EXIT_STATE', '退出状态或摘要无效', 400)
                    controller = row['evidence'].get('controller_instance_id')
                    if not controller or ('controller_instance_id' in body and body['controller_instance_id'] != controller):
                        raise ControlError('IDENTITY_MISMATCH', '退出请求的 SONIC 实例不匹配原接管证明')
                row['state'] = 'releasing'
            receipt = dict(self.identity(row), request_id=body['request_id'], action=action,
                           authority='brain', authority_id=row['authority_id'],
                           ownership_epoch=row['ownership_epoch'], accepted=True, state='pending',
                           updated_at=stamp())
            state['busy'] = body['request_id']
            state['requests'][body['request_id']] = dict(body=body, receipt=receipt, children={})
            self._save(state)
            return deepcopy(receipt), True

    def submit(self, body):
        receipt, created = self._begin(body, 'vla')
        if created:
            try:
                if self.executor is None:
                    raise RuntimeError('Control worker is unavailable')
                self.executor.submit(self._run, body['request_id'])
            except Exception as exc:
                self._finish(body['request_id'], None, exc)
        return self.query(body['lease_id'], request_id=body['request_id'])

    def _internal(self, row, action):
        # Fixed operation identity: after an uncertain result, a second invocation
        # reads the original result; it never sends a new operation blindly.
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, row['lease_id'] + ':' + action).hex
        body = dict(self.identity(row), action=action, request_id=request_id)
        if action == 'release':
            body.update(authority_id=row['authority_id'], ownership_epoch=row['ownership_epoch'])
        receipt, created = self._begin(body, 'navigation')
        if created:
            self._run(request_id)
        result = self.query(row['lease_id'], request_id=request_id)
        if result['state'] not in {'authorized', 'released'}:
            error = result.get('error') or {}
            raise ControlError(error.get('code', 'HANDOFF_UNCONFIRMED'),
                error.get('message', str(result['state'])), details=deepcopy(error.get('details')))
        return result

    def prepare_navigation(self, request):
        row = self.register(request)
        result = self._internal(row, 'acquire')
        # A historical acquire receipt cannot authorize a replay after restart/release.
        observed = self.query(row['lease_id'], observation_id=uuid.uuid4().hex)
        if observed['authorization_current'] is not True:
            raise ControlError('STALE_AUTHORIZATION', '导航授权已经失效')
        return self.reference(result)

    def settle_navigation(self, task_id, command_id):
        self.start()
        lease = lease_identity(self.robot, task_id, command_id)
        with self.store.writer():
            row = deepcopy(self.store.read()['leases'].get(lease))
        if row is None:
            raise ControlError('UNKNOWN_LEASE', '没有可核清的原导航授权')
        if row['state'] == 'released':
            return deepcopy(row['evidence'])
        return self._internal(row, 'release')

    def released(self, task_id, command_id):
        self.start()
        with self.store.writer():
            row = self.store.read()['leases'].get(lease_identity(self.robot, task_id, command_id))
            return bool(row and row['state'] == 'released' and row['evidence'].get('exit_confirmed') is True)

    def submission_attempted(self, task_id, command_id):
        self.start()
        with self.store.writer():
            row = self.store.read()['leases'].get(lease_identity(self.robot, task_id, command_id))
            return bool(row and 'business_submission' in row)

    def check_holding(self, status, task_id, object_id):
        """Bind current object evidence to this Runtime's released grasp grant."""
        from contracts.vla import validate_current_holding
        self.driver._fresh(status)
        proof = status.get('holding_evidence') or {}
        if not isinstance(proof, dict):
            raise ControlError('HOLD_UNCONFIRMED', '当前持物证明格式无效')
        command = proof.get('command_id')
        if not isinstance(command, str):
            raise ControlError('HOLD_UNCONFIRMED', '当前持物证明缺少原抓取命令')
        self.start()
        with self.store.writer():
            row = self.store.read()['leases'].get(lease_identity(self.robot, task_id, command))
            if (not row or row['state'] != 'released' or row['evidence'].get('exit_confirmed') is not True
                    or type(row.get('ownership_epoch')) is not int
                    or row['request'].get('skill') != 'grasp_cola'
                    or row['request']['body'].get('object_id') != object_id):
                raise ControlError('HOLD_UNCONFIRMED', '持物证明未关联本任务已退出的原抓取授权')
            association = dict(task_id=task_id, command_id=command, owner='vla', **self.reference(row))
        try:
            return validate_current_holding(status, object_id, robot_serial=self.robot,
                                            task_id=task_id, source_association=association)
        except (ValueError, TypeError, KeyError) as exc:
            raise ControlError('HOLD_UNCONFIRMED', str(exc)) from None

    def _snapshot(self, request_id):
        with self.store.writer():
            state = self.store.read()
            op = state['requests'][request_id]
            return deepcopy(state['leases'][op['body']['lease_id']]), deepcopy(op)

    def exchange(self, request_id, label, service, path, payload, query_path, kind=None):
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
            if op['body']['action'] == 'acquire':
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
                history=deepcopy((existing or {}).get('observations', {})))
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
            child.update(state='confirmed', receipt=deepcopy(result))
            self._save(state)
        return result

    def stop_vla(self, task_id, command_id):
        """An independent stop must not hold the lock needed by VLA's release callback."""
        self.start()
        lease = lease_identity(self.robot, task_id, command_id)
        request_id = uuid.uuid5(uuid.NAMESPACE_URL, lease + ':stop').hex
        with self.store.writer():
            state = self.store.read()
            row = deepcopy(state['leases'].get(lease))
            if not row or row['target'] != 'vla' or state['current'] not in (None, lease):
                raise ControlError('STALE_AUTHORIZATION', '停止目标不再是原控制任务')
            old = state['requests'].get(request_id)
            if old and old['receipt']['state'] == 'stopped':
                return deepcopy(old['receipt'])
            if old:
                receipt = deepcopy(old['receipt'])  # Existing child causes GET-only recovery.
            else:
                body = dict(self.identity(row), action='stop', request_id=request_id,
                            authority_id=row['authority_id'], ownership_epoch=row['ownership_epoch'])
                receipt = dict(body, accepted=True, state='pending', updated_at=stamp())
                state['requests'][request_id] = dict(body=body, receipt=receipt, children={})
                self._save(state)
        try:
            result = self.driver.stop_vla(row, lambda *args: self.exchange(request_id, *args))
            receipt.update(state='stopped', evidence=result)
        except Exception as exc:
            receipt.update(state='unconfirmed', error={'code': getattr(exc, 'code', 'STOP_UNCONFIRMED'),
                                                       'message': str(exc)})
        with self.store.writer():
            state = self.store.read()
            receipt['updated_at'] = stamp()
            state['requests'][request_id]['receipt'] = receipt
            self._save(state)
        return deepcopy(receipt)

    def _retire_navigation(self, row, request_id):
        with self.store.writer():
            state = self.store.read()
            if state['current'] != row['lease_id']:
                raise ControlError('STALE_AUTHORIZATION', '导航让出目标已经变化')
            state['leases'][row['lease_id']]['state'] = 'releasing'
            relay_only = self._relay_only_prepare(state, row)
            current_exit_check = state['requests'][request_id].get('current_exit_check') is True
            self._save(state)
        exchange = lambda *args: self.exchange(request_id, *args)
        result = (self.driver.retire_relay_preparation(row, relay_only, exchange)
                  if relay_only is not None else self.driver.yield_navigation(row, exchange,
                      verify_current=current_exit_check))
        with self.store.writer():
            state = self.store.read()
            current = state['leases'][row['lease_id']]
            if state['current'] != row['lease_id']:
                raise ControlError('STALE_AUTHORIZATION', '导航让出返回时授权已经变化')
            current.update(state='released', evidence=deepcopy(result), updated_at=stamp())
            state['current'] = None
            self._save(state)
        return result

    def _run(self, request_id):
        try:
            row, op = self._snapshot(request_id)
            exchange = lambda *args: self.exchange(request_id, *args)
            if op['body']['action'] == 'release':
                if op.get('cleanup') and row['target'] == 'navigation' and 'business_submission' in row:
                    self.driver.cancel_navigation(row, exchange)
                evidence = (self.driver.vla_exit(row, op['body']) if row['target'] == 'vla'
                            else self._retire_navigation(row, request_id))
            else:
                with self.store.writer():
                    state = self.store.read()
                    previous = deepcopy(state['leases'].get(state['current']))
                if previous:
                    if previous['target'] != 'navigation' or previous['state'] != 'authorized':
                        raise ControlError('SOURCE_NOT_RELEASED', '旧控制源尚未核清退出')
                    self.driver.navigation_finished(previous)
                    self._retire_navigation(previous, request_id)
                else:
                    self.driver.idle(row)  # The requesting VLA may be accepted but must not yet output.
                with self.store.writer():
                    state = self.store.read()
                    self._eligible(row['request'])
                    if state['current'] is not None or self._closed:
                        raise ControlError('CONTROL_BUSY', '现场或服务状态已经变化')
                    stored = state['leases'][row['lease_id']]
                    # NAV may perform its own mode preparation under this grant;
                    # no business goal is sent until prepare_navigation returns.
                    stored['state'] = 'authorized' if row['target'] == 'navigation' else 'preparing'
                    if row['target'] == 'navigation':
                        stored['grant_published'] = True
                    state['current'] = row['lease_id']
                    self._save(state)
                evidence = (self.driver.prepare_navigation(row, exchange) if row['target'] == 'navigation'
                            else self.driver.vla_prerequisites(row))
            self._finish(request_id, evidence, None)
        except Exception as exc:
            self._finish(request_id, None, exc)

    def _finish(self, request_id, evidence, error):
        with self.store.writer():
            state = self.store.read()
            if state.get('authority_id') != self.authority_id:
                return  # Another authority owns the ledger; this result is late.
            op = state['requests'][request_id]
            row = state['leases'][op['body']['lease_id']]
            if self._closed and error is None:
                error = ControlError('CONTROL_CLOSED', '控制服务关闭后不确认新的授权或释放')
            if op['body']['action'] == 'acquire' and not error:
                try:
                    self._eligible(row['request'])
                    if self._closed or row['authority_id'] != self.authority_id:
                        raise ControlError('STALE_AUTHORIZATION', '授权服务已经变化')
                except Exception as exc:
                    error = exc
            receipt = op['receipt']
            if error:
                unissued = (op['body']['action'] == 'acquire' and row['target'] == 'navigation'
                            and row['state'] == 'acquiring' and row.get('grant_published') is not True
                            and not op['children'] and 'business_submission' not in row)
                row['state'] = 'recovery_required'
                # A partially completed operation may still own hardware. Reserve
                # it only if no older unresolved source still occupies the resource.
                if state['current'] is None:
                    state['current'] = row['lease_id']
                detail = (deepcopy(error.response()['error']) if isinstance(error, ControlError)
                          else {'code': 'HANDOFF_UNCONFIRMED', 'message': str(error)})
                receipt.update(state='unconfirmed', error=detail)
                if unissued:
                    self._retire_unissued_nav(state, row)
            else:
                ended = op['body']['action'] == 'release'
                row.update(state='released' if ended else 'authorized', evidence=deepcopy(evidence))
                state['current'] = None if ended else row['lease_id']
                receipt.update(state=row['state'], owner='none' if ended else row['target'], evidence=deepcopy(evidence))
                receipt.pop('error', None)  # Previous failed checks remain in recovery_checks.
                for key in ('navigation_stopped', 'action_port_released', 'navigation_proof',
                            'proof_context', 'controller_instance_id'):
                    if key in evidence:
                        receipt[key] = deepcopy(evidence[key])
                if ended:
                    receipt.update(authorization_released=True, exit_state_sha256=op['body'].get('exit_state_sha256'))
            row['updated_at'] = stamp()
            receipt['updated_at'] = stamp()
            if state['busy'] == request_id:
                state['busy'] = None
            self._save(state)
