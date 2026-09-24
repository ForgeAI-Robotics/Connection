"""Bounded workers and a single Runtime owner with cooperative I/O waits."""
from concurrent.futures import Future
import queue
import threading
import time
from contracts.tasks import Rejected


class WorkerPool:
    def __init__(self, name, size, capacity=16):
        self.name, self.size = name, size
        self.queue = queue.Queue(capacity)
        self.threads = []
        self.closing = threading.Event()
        self._guard = threading.Lock()

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
        with self._guard:
            if self.closing.is_set():
                raise Rejected('工作器正在关闭')
            try:
                self.queue.put_nowait((future, fn, args, kwargs))
            except queue.Full:
                raise Rejected('工作队列已满，请稍后查询原任务') from None
        return future

    def _run(self):
        while not self.closing.is_set():
            try:
                future, fn, args, kwargs = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(*args, **kwargs))
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                self.queue.task_done()

    def close(self, timeout=2):
        self.closing.set()
        while True:
            try:
                future, *_ = self.queue.get_nowait()
                future.cancel()
                self.queue.task_done()
            except queue.Empty:
                break
        end = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0, end - time.monotonic()))
        return not any(thread.is_alive() for thread in self.threads)

    def health(self):
        return {'name': self.name, 'threads': len(self.threads),
                'alive': sum(t.is_alive() for t in self.threads), 'queued': self.queue.qsize(),
                'closing': self.closing.is_set()}


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
        try:
            (self.control if control else self.work).put_nowait((future, fn, args, kwargs))
        except queue.Full:
            raise Rejected('大脑请求队列已满') from None
        return future

    def call(self, fn, *args, control=False, **kwargs):
        if threading.current_thread() is self.thread:
            return fn(*args, **kwargs)
        return self.submit(fn, *args, control=control, **kwargs).result()

    def _execute(self, item, *, control=False):
        future, fn, args, kwargs = item
        if not future.set_running_or_notify_cancel():
            return
        try:
            self._control_depth += int(control)
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            self._control_depth -= int(control)

    def wait(self, future):
        if threading.current_thread() is not self.thread or self._control_depth:
            return future.result()
        while not future.done():
            try:
                self._execute(self.control.get(timeout=.02), control=True)
            except queue.Empty:
                pass
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
    CALLS = {'submit', 'wait', 'query', 'cancel', 'handoff', '_read_world', '_read_zones'}

    def __init__(self, port, owner, io_pool, controls):
        self.port, self.owner, self.io, self.controls = port, owner, io_pool, controls

    def __getattr__(self, name):
        if name == 'gate_open':
            return self.owner.wait(self.io.submit(getattr, self.port, name))
        value = getattr(self.port, name)
        if name not in self.CALLS or not callable(value):
            return value
        pool = self.controls if name in {'cancel', 'query', 'handoff'} else self.io
        return lambda *args, **kwargs: self.owner.wait(pool.submit(value, *args, **kwargs))
