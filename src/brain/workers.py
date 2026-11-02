"""Bounded workers and a single Runtime owner with cooperative I/O waits."""
from concurrent.futures import Future
from contextvars import copy_context
import queue
import threading
import time
import uuid
from contracts.tasks import Rejected
from shared.diagnostic_context import (context_fields, diagnostic_context,
    diagnostic_event, report_exception)


WAIT_NOTICE_SEC = 30.


def _job(future, fn, args, kwargs, worker):
    metadata = {**context_fields(), 'job_id': uuid.uuid4().hex, 'worker': worker,
                'operation': getattr(fn, '__qualname__', type(fn).__name__),
                'queued_at_monotonic': time.monotonic(),
                'quiet_poll': getattr(fn, '__name__', '') in {'query', 'status', 'current', 'health'}}
    future._brain_diagnostic = metadata
    return future, fn, args, kwargs, copy_context(), metadata


def _run_job(item):
    future, fn, args, kwargs, context, metadata = item
    if not future.set_running_or_notify_cancel():
        return

    def run():
        started = time.monotonic()
        metadata['started_at_monotonic'] = started
        with diagnostic_context(job_id=metadata['job_id']):
            details = {'worker': metadata['worker'], 'operation': metadata['operation'],
                       'queue_wait_s': round(started - metadata['queued_at_monotonic'], 6)}
            if not metadata['quiet_poll']:
                diagnostic_event('WORKER', 'started', **details, _human_suppressed=True)
            try:
                value = fn(*args, **kwargs)
            except BaseException as exc:
                report_exception(exc, component=metadata['worker'], operation=metadata['operation'])
                diagnostic_event('WORKER', 'cancelled' if isinstance(exc, Rejected) else 'failed',
                    **details, duration_s=round(time.monotonic() - started, 6),
                    _human_suppressed=True)
                future.set_exception(exc)
            else:
                elapsed = time.monotonic() - started
                if not metadata['quiet_poll'] or elapsed >= 2.:
                    diagnostic_event('WORKER', 'completed', **details,
                        duration_s=round(elapsed, 6), _human_suppressed=True)
                future.set_result(value)

    context.run(run)


class WorkerPool:
    def __init__(self, name, size, capacity=16):
        self.name, self.size = name, size
        self.queue = queue.Queue(capacity)
        self.threads = []
        self.closing = threading.Event()
        self._guard = threading.Lock()
        self._active_guard = threading.Lock()
        self._active = {}

    def start(self):
        with self._guard:
            if self.threads:
                return
            if self.closing.is_set():
                raise Rejected('工作器正在关闭')
            for number in range(self.size):
                thread = threading.Thread(target=self._run, name=f'{self.name}-{number}', daemon=True)
                self.threads.append(thread)
                thread.start()

    def submit(self, fn, *args, **kwargs):
        self.start()
        future = Future()
        item = _job(future, fn, args, kwargs, self.name)
        with self._guard:
            if self.closing.is_set():
                raise Rejected('工作器正在关闭')
            try:
                self.queue.put_nowait(item)
            except queue.Full:
                diagnostic_event('WORKER', 'queue_full', worker=self.name, level='WARNING')
                raise Rejected('工作队列已满，请稍后查询原任务') from None
        if not item[-1]['quiet_poll']:
            diagnostic_event('WORKER', 'queued', **item[-1], _human_suppressed=True)
        return future

    def _run(self):
        while not self.closing.is_set():
            try:
                item = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                with self._active_guard:
                    self._active[threading.get_ident()] = item[-1]
                _run_job(item)
            finally:
                with self._active_guard:
                    self._active.pop(threading.get_ident(), None)
                self.queue.task_done()

    def close(self, timeout=2):
        # start() appends before Thread.start(); snapshot under the same guard
        # so shutdown cannot join a thread that has not started yet.
        with self._guard:
            self.closing.set()
            threads = tuple(self.threads)
        while True:
            try:
                future, *_ = self.queue.get_nowait()
                future.cancel()
                self.queue.task_done()
            except queue.Empty:
                break
        end = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0, end - time.monotonic()))
        return not any(thread.is_alive() for thread in threads)

    def health(self):
        with self._active_guard:
            active = [_job_health(meta) for meta in self._active.values()]
        return {'name': self.name, 'threads': len(self.threads),
                'alive': sum(t.is_alive() for t in self.threads), 'queued': self.queue.qsize(),
                'closing': self.closing.is_set(), 'active': active}


def _job_health(metadata):
    result = {key: metadata[key] for key in ('job_id', 'operation', 'task_id', 'command_id',
                                           'step_id', 'attempt_id', 'trace_id') if key in metadata}
    started = metadata.get('started_at_monotonic')
    if started is not None:
        result['elapsed_s'] = round(max(0., time.monotonic() - started), 3)
        result['queue_wait_s'] = round(max(0., started - metadata['queued_at_monotonic']), 6)
    return result


class RuntimeOwner:
    """Only this thread calls mutating Runtime methods in the application.

    While waiting for I/O, service control/read messages, never a second task
    admission or drive. The Runtime's identity and phase checks reject late data.
    """
    def __init__(self, capacity=32):
        self.work = queue.Queue(capacity)
        self.control = queue.Queue(capacity)
        self.thread = None
        self.stopping = threading.Event()
        self._guard = threading.Lock()
        self._control_depth = 0
        self._wait_generation = 0
        self.on_idle = None
        self.on_wait_tick = None
        self._active_guard = threading.Lock()
        self._active = []

    def interrupt_waits(self):
        """Release the task owner from abandoned I/O without sending remote controls."""
        self._wait_generation += 1

    def start(self):
        with self._guard:
            if self.thread:
                return
            self.thread = threading.Thread(target=self._run, name='brain-runtime', daemon=True)
            self.thread.start()

    def submit(self, fn, *args, control=False, **kwargs):
        if self.stopping.is_set():
            raise Rejected('大脑正在关闭')
        self.start()
        future = Future()
        item = _job(future, fn, args, kwargs, 'brain-runtime-control' if control else 'brain-runtime')
        try:
            (self.control if control else self.work).put_nowait(item)
        except queue.Full:
            diagnostic_event('WORKER', 'queue_full', worker='brain-runtime', level='WARNING')
            raise Rejected('大脑请求队列已满') from None
        if not item[-1]['quiet_poll']:
            diagnostic_event('WORKER', 'queued', **item[-1], _human_suppressed=True)
        return future

    def call(self, fn, *args, control=False, **kwargs):
        if threading.current_thread() is self.thread:
            return fn(*args, **kwargs)
        return self.submit(fn, *args, control=control, **kwargs).result()

    def _execute(self, item, *, control=False):
        try:
            self._control_depth += int(control)
            with self._active_guard:
                self._active.append(item[-1])
            _run_job(item)
        finally:
            with self._active_guard:
                self._active.pop()
            self._control_depth -= int(control)

    def health(self):
        with self._active_guard:
            active = [_job_health(meta) for meta in self._active]
        return {'name': 'brain-runtime', 'alive': bool(self.thread and self.thread.is_alive()),
                'queued': self.work.qsize(), 'control_queued': self.control.qsize(),
                'closing': self.stopping.is_set(), 'active': active}

    def wait(self, future):
        if threading.current_thread() is not self.thread or self._control_depth:
            return future.result()
        generation = self._wait_generation
        started = time.monotonic()
        notice_at = started + WAIT_NOTICE_SEC
        diagnostic_tick_at = started
        diagnostic_error_reported = False
        while not future.done():
            try:
                self._execute(self.control.get(timeout=.02), control=True)
            except queue.Empty:
                pass
            if time.monotonic() >= notice_at:
                metadata = getattr(future, '_brain_diagnostic', {})
                diagnostic_event('WORKER', 'waiting', level='INFO',
                    waiting_for=metadata.get('operation', 'background result'),
                    waiting_job_id=metadata.get('job_id'),
                    worker=metadata.get('worker'), elapsed_s=round(time.monotonic() - started, 3))
                notice_at = time.monotonic() + WAIT_NOTICE_SEC
            if generation != self._wait_generation:
                future.cancel()
                raise Rejected('本轮任务已取消，结束本地等待')
            if self.on_wait_tick is not None and time.monotonic() >= diagnostic_tick_at:
                diagnostic_tick_at = time.monotonic() + .25
                try:
                    self.on_wait_tick()
                    diagnostic_error_reported = False
                except Exception as exc:
                    if not diagnostic_error_reported:
                        report_exception(exc, component='brain-runtime', operation='diagnostic_wait_tick')
                        diagnostic_error_reported = True
        return future.result()

    def _run(self):
        while not self.stopping.is_set():
            try:
                item = self.control.get_nowait()
                control = True
            except queue.Empty:
                try:
                    item = self.work.get(timeout=.02)
                    control = False
                except queue.Empty:
                    if self.on_idle is not None:
                        try:
                            self.on_idle()
                        except Exception as exc:
                            report_exception(exc, component='brain-runtime', operation='idle_observation')
                    continue
            self._execute(item, control=control)

    def close(self, timeout=2):
        self.stopping.set()
        for jobs in (self.work, self.control):
            while True:
                try:
                    future, *_ = jobs.get_nowait()
                    future.cancel()
                except queue.Empty:
                    break
        if self.thread:
            self.thread.join(timeout)
        return self.thread is None or not self.thread.is_alive()


class CooperativePort:
    """Offload blocking calls while keeping state changes on the Runtime owner."""
    CALLS = {'submit', 'wait', 'wait_completion', 'query', 'cancel', 'handoff', 'handoff_with_context', '_read_world', '_read_zones', 'observe_recovery', 'capabilities', 'effect_control'}

    def __init__(self, port, owner, io_pool, controls):
        self.port, self.owner, self.io, self.controls = port, owner, io_pool, controls

    def __getattr__(self, name):
        if name == 'gate_open':
            return self.owner.wait(self.io.submit(getattr, self.port, name))
        value = getattr(self.port, name)
        if name not in self.CALLS or not callable(value):
            return value
        pool = self.controls if name in {'cancel', 'query', 'handoff', 'handoff_with_context', 'effect_control'} else self.io
        def invoke(*args, **kwargs):
            request = kwargs.get('request')
            if not isinstance(request, dict):
                request = next((arg for arg in args if isinstance(arg, dict)
                                and isinstance(arg.get('body'), dict)), None)
            identities = {}
            if isinstance(request, dict):
                body = request.get('body') or {}
                identities = {key: request.get(key) or body.get(key)
                              for key in ('task_id', 'step_id', 'command_id')}
                if not identities['command_id'] and name in {'submit', 'wait', 'wait_completion', 'query', 'cancel'} and args:
                    identities['command_id'] = args[0] if isinstance(args[0], str) else None
                old = context_fields()
                if identities['command_id'] != old.get('command_id'):
                    identities['attempt_id'] = request.get('attempt_id')
                if not identities['step_id'] and identities['command_id'] == old.get('command_id'):
                    identities.pop('step_id')
            with diagnostic_context(**identities):
                return self.owner.wait(pool.submit(value, *args, **kwargs))
        return invoke
