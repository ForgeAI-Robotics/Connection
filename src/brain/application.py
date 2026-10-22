"""Lifecycle owner for one brain process. Entries never instantiate this class."""
import fcntl
from functools import partial
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
        self.media = MediaService(service.status)
        self._lease = None
        self._started = False
        self._closing = False
        self._start_lock = threading.Lock()
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
        service.plan_call = lambda rt, options: self.owner.wait(self.planner.submit(service.planning.plan, rt, options))
        service.select_call = lambda task, options: self.owner.wait(self.planner.submit(service.reasoner.select, task, options))
        # Advice reads immutable evidence off-thread; only the owner applies its result.
        # It must never occupy the planner or the owner's cooperative wait path.
        service._advise = self._schedule_advice
        service._launch = lambda rt: self.owner.submit(service._drive, rt)
        service.refresh_advice = lambda rt: self.owner.submit(service._advise, rt)
        service.reflection = self.learning.enqueue

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
        emit(event, record)
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
            except Exception:
                self.close()
                raise
        return self

    def publish(self, task, task_id=None, **kwargs):
        self.start()
        if self._closing:
            raise Rejected('大脑正在关闭，停止接受新任务')
        return self.owner.call(self.service.publish, task, task_id, **kwargs)

    def control(self, action, *, task_id=None, step_id=None, expected_command_id=None):
        self.start()
        from shared.brain_journal import emit as journal
        control_id = uuid.uuid4().hex
        def log(event, **details):
            journal('CONTROL', event=event, inherit_task=False, action=action,
                    control_id=control_id, task_id=task_id, step_id=step_id,
                    command_id=expected_command_id, **details)
        log('received')
        def apply():
            log('processing')
            result = self.service.control(action, task_id=task_id,
                **({"step_id": step_id} if step_id is not None else {}),
                **({"expected_command_id": expected_command_id} if expected_command_id is not None else {}))
            runtime = self.service.runtime
            if not result.get('no_op') and runtime and runtime.state in {'succeeded', 'failed', 'cancelled', 'recovery_required'}:
                self.learning.enqueue(runtime.record)
            return result
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
        if result.get('task_id'):
            reflection = self.learning.result(result['task_id'])
            if reflection:
                result['reflection'] = {key: value for key, value in reflection.items()
                                        if key not in {'episode', 'path'}}
        return result

    def confirm_advice(self, task_id, advice_id, expected_revision):
        self.start()
        # Ordinary owner work keeps I/O cooperative: pause/cancel can preempt a query.
        return self.owner.call(self.service.confirm_advice, task_id, advice_id, expected_revision)

    def vla_effect(self, task_id, command_id, action, *, evidence=None, authorization=''):
        self.start()
        return self.owner.call(self.service.vla_effect, task_id, command_id, action,
                               evidence=evidence, authorization=authorization, control=True)

    def health(self):
        return {'ready': self._started and not self._closing,
                'state': 'stopping' if self._closing else 'healthy' if self._started else 'created',
                'runtime_thread': bool(self.owner.thread and self.owner.thread.is_alive()),
                'workers': [p.health() for p in (self.planner, self.adviser, self.io, self.controls)],
                'reflection_error': self.learning.last_error}

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
        timeout = float(timeout if timeout is not None else (self.config.get('brain') or {}).get('shutdown_timeout_sec', 10))
        deadline = time.monotonic() + timeout
        if self.owner.thread and self.owner.thread.is_alive():
            try:
                self.owner.submit(self._checkpoint, control=True).result(timeout=max(0, deadline-time.monotonic()))
            except Exception:
                pass  # Durable command identities remain unresolved; never fabricate a stop.
        remaining = lambda: max(0, deadline - time.monotonic())
        stopped = self.owner.close(remaining())
        stopped = self.learning.close(remaining()) and stopped
        self._advice_pending = None
        for pool in (self.planner, self.adviser, self.io, self.controls, self.media, *self.extra_workers):
            stopped = pool.close(remaining()) and stopped
        for transport in self.service.transports:
            transport.close(remaining())
        if stopped and self._lease is not None:
            fcntl.flock(self._lease, fcntl.LOCK_UN)
            os.close(self._lease)
            self._lease = None
        return stopped
