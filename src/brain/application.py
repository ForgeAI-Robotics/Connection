"""Lifecycle owner for one brain process. Entries never instantiate this class."""
import fcntl
from functools import partial
import json
import os
from pathlib import Path
import threading
import time
import uuid
from concurrent.futures import TimeoutError
from contracts.tasks import Rejected
from brain.workers import RuntimeOwner, WorkerPool, CooperativePort
from brain.learning.worker import ReflectionWorker
from brain.service_support import runtime_dir
from brain.storage.tasks import load_existing
from brain.observability.events import emit
from shared.diagnostic_context import diagnostic_context, task_fields, report_exception


class BrainApplication:
    def __init__(self, service, *, reflection_processor=None):
        self.service = service
        self.config = service.config
        settings = self.config.get('brain') or {}
        self.owner = RuntimeOwner(int(settings.get('queue_limit', 32)))
        self.io = WorkerPool('brain-io', max(2, int(settings.get('io_workers', 4))))
        self.controls = WorkerPool('brain-control', 2)
        self.planner = WorkerPool('brain-planner', 1, capacity=1)
        self.adviser = WorkerPool('brain-advice', 1, capacity=1)
        self._advice_inflight = None
        self._advice_pending = None
        root = Path(runtime_dir(self.config))
        self.learning = ReflectionWorker(root / 'learning', self.config,
                                         processor=reflection_processor, emit=emit)
        self.extra_workers = []
        self._execution_ports = []
        from brain.api.media import MediaService
        self.media = MediaService(service.status, self.config)
        self._lease = None
        self._started = False
        self._closing = False
        self._start_lock = threading.Lock()
        self.body_control = None
        control_settings = (self.config.get('reception_real') or {}).get('body_control') or {}
        from brain.adapters.source_gate import source_gate_enabled
        if control_settings.get('mode', 'unified') not in {'unified', 'source_gate'}:
            raise ValueError('Unknown body_control.mode')
        if source_gate_enabled(self.config):
            from brain.kernel.source_gate import SourceGateControl
            self.body_control = SourceGateControl(self.config, root, self.controls)
        elif control_settings.get('enabled') is True:
            from brain.adapters.body_control import ControlDriver
            from brain.kernel.body_control import BodyControl
            for key in ('robot_serial', 'control_resource', 'action_endpoint', 'gateway_base_url', 'gateway_ca_file'):
                if not isinstance(control_settings.get(key), str) or not control_settings[key].strip():
                    raise ValueError('body_control requires ' + key)
            self.body_control = BodyControl(self.config, root, ControlDriver(self.config), self.io)
        service.body_control = self.body_control
        from brain.observability.diagnostic_runs import DiagnosticRuns
        from brain.adapters.diagnostic_peer_summaries import DiagnosticPeerSummaryReader
        self.diagnostic_runs = DiagnosticRuns(self.config,
            control_records=self._diagnostic_control_records
            if getattr(self.body_control, 'mode', None) == 'source_gate' else None,
            peer_summary_reader=DiagnosticPeerSummaryReader(self.config))
        factory = service.port_factory
        runtime_factory = service.runtime_factory

        def port_factory(backend):
            port = factory(backend)
            underlying = getattr(port, 'port', port)
            if hasattr(underlying, 'executor'):
                underlying.executor = self.io
            self._execution_ports.append(port)
            return CooperativePort(port, self.owner, self.io, self.controls)

        def runtime(config, port, **kwargs):
            result = runtime_factory(config, port, **kwargs)
            result.event_sink = self._event
            return result

        service.port_factory = port_factory
        service.runtime_factory = runtime
        service.plan_call = lambda rt, options: self.owner.wait(
            self._submit_runtime(self.planner, service.planning.plan, rt, options))
        service.select_call = lambda task, options: self.owner.wait(self.planner.submit(service.reasoner.select, task, options))
        # Advice reads immutable evidence off-thread; only the owner applies its result.
        # It must never occupy the planner or the owner's cooperative wait path.
        service._advise = self._schedule_advice
        service._launch = lambda rt: self._submit_runtime(self.owner, service._drive, rt)
        service.refresh_advice = lambda rt: self._submit_runtime(self.owner, service._advise, rt)
        service.reflection = self.learning.enqueue
        self._door_observer = None
        self._door_future = None
        self._next_vla_observation = 0.
        self.owner.on_idle = self._idle
        self.owner.on_wait_tick = self.diagnostic_runs.tick

    def _diagnostic_control_records(self):
        """Bounded off-owner identity lookup; never change the control ledger reader."""
        path = self.body_control.store.path
        if not path.exists():
            return None
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('诊断选源索引超过读取预算，原控制账本保持不变')
        with path.open('rb') as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('诊断选源索引超过读取预算，原控制账本保持不变')
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get('source_gate'), dict):
            raise ValueError('诊断选源索引结构无效')
        return value

    def _idle(self):
        if self._closing or not self._started:
            return
        self._diagnostic_call('tick')
        self._observe_waiting_vla()

    def _diagnostic_call(self, method, *args):
        target = getattr(self, 'diagnostic_runs', None)
        call = getattr(target, method, None)
        if not callable(call):
            return None
        try:
            return call(*args)
        except Exception as exc:
            report_exception(exc, component='brain.diagnostic_runs', operation=method)
            return {'error': 'diagnostic_transport_unavailable'} if method == 'health' else None

    @staticmethod
    def _submit_runtime(pool, fn, runtime, *args, **kwargs):
        with diagnostic_context(**task_fields(runtime.record)):
            return pool.submit(fn, runtime, *args, **kwargs)

    def _observe_waiting_vla(self):
        """One read-only observation on the existing owner, with cooperative I/O."""
        if self._closing or not self._started or time.monotonic() < self._next_vla_observation:
            return
        self._next_vla_observation = time.monotonic() + 1.
        try:
            record = load_existing(runtime_dir(self.config)) or {}
            if record.get('state') != 'waiting_human' or not record.get('vla_confirmation'):
                return
            with diagnostic_context(trace_id=uuid.uuid4().hex, **task_fields(record)):
                runtime = self.service.attach()
                if runtime.observe_waiting_vla():
                    if runtime.state == 'recovery_required':
                        self.learning.enqueue(runtime.record)
                        self._schedule_advice(runtime)
                self._observe_open_door(runtime)
        except Rejected:
            pass  # A concurrent control or changed task owns the next decision.
        finally:
            self._next_vla_observation = time.monotonic() + 1.

    def _observe_open_door(self, runtime):
        """Reuse the owner/I/O pool: one bounded observation, never a second task loop."""
        context = runtime.door_observation_context()
        if not context:
            return
        # An interrupted HTTP call may still finish off-thread; do not overlap it.
        if self._door_future is not None and not self._door_future.done():
            return
        if self._door_observer is None:
            from brain.adapters.door_verification import DoorObserver
            self._door_observer = DoorObserver(self.config, runtime_dir(self.config))
        camera = getattr(self._door_observer, 'camera', None)
        if context.get('vla_url') and isinstance(getattr(getattr(camera, 'http', None), 'base_url', None), str):
            if camera.http.base_url != context['vla_url'].rstrip('/'):
                raise Rejected('视觉相机端点与原任务 VLA 不一致')
        self._door_observer.options.update(context['options'])
        self._door_future = self.io.submit(self._door_observer.sample, context)
        try:
            result = self.owner.wait(self._door_future,
                timeout_sec=context['options']['vlm_timeout_sec'] + 2.)
        except TimeoutError:
            from brain.adapters.door_verification import SCHEMA, GRASP_SCHEMA, CLOSE_SCHEMA
            result = dict(schema=GRASP_SCHEMA if context.get('operation') == 'grasp_cola' else CLOSE_SCHEMA if context.get('operation') in {'close_fridge','close_fridge_test'} else SCHEMA, status='unknown', reason='observation_deadline_exceeded',
                          vlm_calls=1)
        if result is None:
            return
        # Requery the original command after inference. Failure/stop wins over vision.
        runtime.observe_waiting_vla()
        from shared.service_credentials import service_token
        token = (service_token(context['options']['verifier_token_env'])
                 if context['options']['mode'] == 'auto' and result.get('status') == 'pass' else '')
        try:
            runtime.apply_door_observation(context, result,
                                          authorization=('Bearer ' + token) if token else '')
        finally:
            if runtime.pending_vla_completion():
                self.service._launch(runtime)

    def _advice_log(self, event, context, **details):
        from shared.brain_journal import emit as journal
        journal('ADVICE', event=event, inherit_task=False, task_id=context.get('task_id'),
                step_id=context.get('phase'), command_id=context.get('open_command_id'),
                revision=context.get('revision'), **details)

    def _schedule_advice(self, runtime):
        if self._closing:
            return
        context = self.service.advice_context(runtime)
        if context is None:
            return
        key = (context['task_id'], context['revision'])
        if self._advice_inflight == key:
            return
        if self._advice_inflight is not None:
            # One latest pending snapshot, never an unbounded backlog of old failures.
            self._advice_pending = (runtime, context)
            self._advice_log('queued', context)
            return
        self._advice_inflight = key
        self._advice_log('started', context)
        with diagnostic_context(**task_fields(context)):
            future = self.adviser.submit(self.service.compute_advice, context)

        def completed(result):
            if not self._closing:
                try:
                    self.owner.submit(self._finish_advice, runtime, context, result, control=True)
                except Rejected:
                    pass  # Closing: no off-thread write and no replay on restart.
        future.add_done_callback(completed)

    def _finish_advice(self, runtime, context, future):
        self._advice_inflight = None
        if self._closing:
            self._advice_pending = None
            return
        advice = future.result()
        applied = runtime.record_advice(context, advice)
        self._advice_log('completed' if applied else 'discarded', context,
                         error=advice.get('error'), source=advice.get('source'))
        pending, self._advice_pending = self._advice_pending, None
        if pending:
            pending_runtime, snapshot = pending
            try:
                current = pending_runtime.snapshot()
            except Rejected:
                return
            if all(current.get(k) == snapshot.get(k) for k in
                   ('task_id', 'revision', 'state', 'open_command_id')):
                self._schedule_advice(pending_runtime)

    def _event(self, event, record):
        if event == 'task_cancelled':
            self._advice_pending = None
            self.owner.interrupt_waits()
            for port in self._execution_ports:
                abandon = getattr(port, 'abandon_waits', None)
                if callable(abandon):
                    abandon()
            self._execution_ports.clear()
            # All cancellation paths (including confirmed advice) emit this
            # Runtime event after the cancelled record is durable.
            if self.body_control is not None:
                try:
                    self.body_control.cancel_task(record['task_id'])
                except Exception as exc:
                    emit('control_cleanup_unconfirmed', record, error=str(exc))
        emit(event, record)
        self._diagnostic_call('event', event, record)
        if (self.config.get("brain") or {}).get("capture_timeline", True):
            self.media.event(event, record)

    def start(self):
        with self._start_lock:
            if self._closing:
                raise Rejected('大脑已进入关闭流程')
            if self._started:
                return self
            root = Path(runtime_dir(self.config))
            root.mkdir(parents=True, exist_ok=True)
            fd = os.open(root / 'brain.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise Rejected('同一本账已有大脑进程持有') from None
            self._lease = fd
            try:
                if self.body_control is not None:
                    self.body_control.start()
                self.owner.start()
                self.learning.start()
                record = load_existing(root) or {}
                from contracts.tasks import NON_TERMINAL_STATES, TERMINAL_STATES
                if record and record.get('state') not in NON_TERMINAL_STATES | TERMINAL_STATES:
                    raise ValueError('任务账本状态无效；启动已停止')
                if record.get('state') in {'succeeded', 'failed', 'cancelled', 'recovery_required'}:
                    self.learning.enqueue(record)
                self._started = True
                if record.get('task_id'):
                    emit('task_restored', record)
                    self._diagnostic_call('event', 'task_restored', record)
                    if record.get('state') == 'waiting_human' and record.get('vla_confirmation'):
                        self.owner.submit(self._resume_vla_confirmation)
            except Exception:
                self.close()
                raise
        return self

    def _resume_vla_confirmation(self):
        runtime = self.service.attach()
        if runtime.pending_vla_completion():
            self.service._drive(runtime)  # Query persisted intent only; no finish replay.

    def publish(self, task, task_id=None, **kwargs):
        self.start()
        if self._closing:
            raise Rejected('大脑正在关闭，停止接受新任务')
        # A request without a task identity stays unbound until the original
        # service admission allocates it; later events bind the saved snapshot.
        with diagnostic_context(task_id=task_id, step_id=None, attempt_id=None, command_id=None):
            return self.owner.call(self.service.publish, task, task_id, **kwargs)

    def control(self, action, *, task_id=None, step_id=None, expected_command_id=None):
        self.start()
        from shared.brain_journal import emit as journal
        control_id = uuid.uuid4().hex
        def log(event, **details):
            journal('CONTROL', event=event, inherit_task=False, action=action,
                    control_id=control_id, task_id=task_id, step_id=step_id,
                    command_id=expected_command_id, **details)
        def apply():
            log('processing')
            result = self.service.control(action, task_id=task_id,
                **({"step_id": step_id} if step_id is not None else {}),
                **({"expected_command_id": expected_command_id} if expected_command_id is not None else {}))
            runtime = self.service.runtime
            if not result.get('no_op') and runtime and runtime.state in {'succeeded', 'failed', 'cancelled', 'recovery_required'}:
                self.learning.enqueue(runtime.record)
            if action == 'cancel' and result.get('accepted') and self.body_control is not None:
                result['control_recovery'] = self.body_control.cancel_task(result.get('task_id'))
                result['message'] = ('整轮任务已取消；执行端停止与接收源全关分别记录，历史取消不阻止新任务'
                    if getattr(self.body_control, 'mode', '') == 'source_gate' else
                    '整轮任务已取消，可发布新任务；旧会话停止与撤权独立收尾')
            return result
        with diagnostic_context(task_id=task_id, step_id=step_id, command_id=expected_command_id,
                                attempt_id=None, control_id=control_id):
            log('received')
            try:
                result = self.owner.call(apply, control=True)
            except Exception as exc:
                log('rejected', error=str(exc))
                raise
            log('accepted' if result.get('accepted') else 'rejected', state=result.get('state'),
                detail=result.get('message'), error=result.get('error'))
            return result

    def status(self):
        # Atomic read only: health/status do not wait behind any remote call.
        result = self.service.status()
        if self.body_control is not None:
            result['control_recovery'] = self.body_control.recovery_status()
        if result.get('task_id'):
            reflection = self.learning.result(result['task_id'])
            if reflection:
                result['reflection'] = {key: value for key, value in reflection.items()
                                        if key not in {'episode', 'path'}}
        return result

    def recover_control(self, task_id, expected_lease_id, *, new_exit_check=False):
        self.start()
        with diagnostic_context(task_id=task_id, step_id=None, attempt_id=None, command_id=None):
            return self.body_control.request_cleanup(task_id, expected_lease_id,
                **({'new_exit_check': True} if new_exit_check else {}))

    def confirm_advice(self, task_id, advice_id, expected_revision):
        self.start()
        # Ordinary owner work keeps I/O cooperative: pause/cancel can preempt a query.
        with diagnostic_context(task_id=task_id, step_id=None, attempt_id=None, command_id=None):
            return self.owner.call(self.service.confirm_advice, task_id, advice_id, expected_revision)

    def vla_effect(self, task_id, command_id, action, *, evidence=None, authorization='', auto_advance=False):
        self.start()
        with diagnostic_context(task_id=task_id, command_id=command_id, step_id=None, attempt_id=None):
            return self.owner.call(self.service.vla_effect, task_id, command_id, action,
                                   evidence=evidence, authorization=authorization, auto_advance=auto_advance, control=True)

    def health(self):
        from shared.brain_journal import get_journal
        from shared.diagnostic_io import redact
        return {'ready': self._started and not self._closing,
                'state': 'stopping' if self._closing else 'healthy' if self._started else 'created',
                'runtime_thread': bool(self.owner.thread and self.owner.thread.is_alive()),
                'runtime_work': self.owner.health(),
                'workers': [p.health() for p in (self.planner, self.adviser, self.io, self.controls)],
                'reflection_error': redact(self.learning.last_error),
                'media': self.media.health() if callable(getattr(self.media, 'health', None)) else {},
                'diagnostic_runs': self._diagnostic_call('health') or {},
                'diagnostics': get_journal().health()}

    def _checkpoint(self):
        if not (load_existing(runtime_dir(self.config)) or {}).get('task_id'):
            return
        runtime = self.service.attach()
        if (runtime.state == 'running' or (runtime.state == 'waiting_human'
                and runtime.public_status().get('vla_confirmation'))):
            runtime.request_pause()
        elif runtime.state == 'verifying':
            runtime.fail_closed('process_shutdown')
        # Paused/waiting/recovery retain their exact original command identity.

    def close(self, timeout=None):
        self._closing = True
        if self.body_control is not None:
            self.body_control.stop_accepting()
        timeout = float(timeout if timeout is not None else (self.config.get('brain') or {}).get('shutdown_timeout_sec', 10))
        deadline = time.monotonic() + timeout
        if self.owner.thread and self.owner.thread.is_alive():
            try:
                self.owner.submit(self._checkpoint, control=True).result(timeout=max(0, deadline-time.monotonic()))
            except Exception as exc:
                report_exception(exc, component='brain.shutdown', operation='checkpoint')
                # Durable command identities remain unresolved; never fabricate a stop.
        remaining = lambda: max(0, deadline - time.monotonic())
        stopped = self.owner.close(remaining())
        stopped = self.learning.close(remaining()) and stopped
        self._advice_pending = None
        for pool in (self.planner, self.adviser, self.io, self.controls, self.media, *self.extra_workers):
            stopped = pool.close(remaining()) and stopped
        for transport in self.service.transports:
            transport.close(remaining())
        # Diagnostic transport cannot keep the business lease alive, and close
        # never sends recorder start/stop or robot controls.
        self._diagnostic_call('close', remaining())
        if stopped and self._lease is not None:
            if self.body_control is not None:
                self.body_control.close()
            fcntl.flock(self._lease, fcntl.LOCK_UN)
            os.close(self._lease)
            self._lease = None
        from shared.brain_journal import get_journal
        # A flush timeout is a diagnostic health issue, not evidence of an unsafe
        # task transition; do not change the existing worker-stop return contract.
        get_journal().close(timeout=remaining())
        return stopped
