import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.app import create_service
from brain.application import BrainApplication
from brain.adapters.ports import DeskAdapter
from brain.workers import WorkerPool
from contracts.tasks import Rejected


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'brain': {'capture_timeline': False}, 'reflection': {'enabled': False}, 'reception_real': {
            'kernel_runtime_dir': str(Path(self.temp.name) / 'kernel'),
            'runtime_dir': str(Path(self.temp.name) / 'old'), 'kernel_enabled': False}}
        for target, fn in [('select_backend', lambda *a, **kw: 'desk'),
                           ('target_identity', lambda *a: {'backend': 'desk'})]:
            patcher = patch('brain.service.' + target, side_effect=fn)
            patcher.start(); self.addCleanup(patcher.stop)
        self.apps = []
        self.addCleanup(lambda: [app.close(2) for app in self.apps])

    def application(self, model, perform=None, reflection=None):
        world = {'milk_1': {'category': 'milk', 'pos': [0, 0], 'grasped': False}}
        def action(text):
            if perform:
                perform(text)
            world['milk_1']['grasped'] = True
            return {'success': True}
        port = DeskAdapter(perform=action, world=lambda: world, zones={'milk_area': {'pos': [1, 1], 'radius': .1}})
        app = BrainApplication(create_service(self.config, model=model, port_factory=lambda _: port),
                               reflection_processor=reflection)
        self.apps.append(app)
        return app

    def plan(self, _):
        return json.dumps({'subtask_list': [{'robot_name': 'FQrobot', 'subtask': '抓取 milk_1'}]})

    def wait_state(self, app, state):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if app.status().get('state') == state:
                return
            time.sleep(.01)
        self.fail(f'{app.status()} != {state}')

    def test_cancel_and_status_respond_during_slow_planning(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(messages):
            entered.set(); release.wait(3)
            return self.plan(messages)
        commands = []
        app = self.application(slow, commands.append)
        thread = threading.Thread(target=app.publish, args=('抓取牛奶', 'slow-plan'))
        thread.start()
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        self.assertEqual(app.status()['state'], 'running')
        self.assertTrue(app.control('cancel')['completed'])
        self.assertLess(time.monotonic() - before, .8)
        release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(commands, [])
        self.assertEqual(app.status()['state'], 'cancelled')

    def test_cancel_during_body_wait_does_not_fabricate_stop(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(_):
            entered.set(); release.wait(3)
        app = self.application(self.plan, slow)
        app.publish('抓取牛奶', 'slow-body')
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        result = app.control('cancel')
        self.assertFalse(result.get('completed'))
        self.assertNotEqual(app.status()['state'], 'cancelled')
        self.assertLess(time.monotonic() - before, .8)
        release.set()

    def test_same_ledger_rejects_second_process_owner(self):
        first = self.application(self.plan).start()
        second = self.application(self.plan)
        with self.assertRaises(Rejected):
            second.start()
        self.assertTrue(first.health()['ready'])

    def test_late_reflection_is_bound_to_original_task(self):
        self.config['reflection']['enabled'] = True
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def reflect(episode):
            if episode['task_id'] == 'task-first':
                entered.set(); release.wait(3)
            return {'task_id': episode['task_id'], 'summary': episode['task_id']}
        app = self.application(self.plan, reflection=reflect)
        app.publish('抓取牛奶', 'task-first')
        self.wait_state(app, 'succeeded')
        self.assertTrue(entered.wait(1))
        app.publish('抓取牛奶', 'task-second')
        self.wait_state(app, 'succeeded')
        self.assertEqual(app.status()['task_id'], 'task-second')
        release.set()
        deadline = time.monotonic() + 2
        while not app.learning.result('task-first') and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(app.learning.result('task-first')['task_id'], 'task-first')
        self.assertNotEqual((app.status().get('reflection') or {}).get('task_id'), 'task-first')

    def test_worker_capacity_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        pool = WorkerPool('test-bounded', 1, capacity=1)
        try:
            running = pool.submit(lambda: (entered.set(), release.wait(2)))
            self.assertTrue(entered.wait(1))
            pending = pool.submit(lambda: None)
            with self.assertRaises(Rejected):
                pool.submit(lambda: None)
            self.assertEqual(len(pool.threads), 1)
            release.set()
            running.result(1); pending.result(1)
        finally:
            release.set(); pool.close(1)

    def test_all_task_writes_use_the_runtime_owner_thread(self):
        from brain.kernel.runtime import TaskRuntime
        original = TaskRuntime._save
        writers = []
        def save(runtime, event):
            writers.append(threading.current_thread().name)
            return original(runtime, event)
        app = self.application(self.plan)
        with patch.object(TaskRuntime, '_save', save):
            app.publish('抓取牛奶', 'one-owner')
            self.wait_state(app, 'succeeded')
        self.assertTrue(writers)
        self.assertEqual(set(writers), {'brain-runtime'})

    def test_shutdown_keeps_unknown_command_and_prevents_second_owner(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(_):
            entered.set(); release.wait(3)
        app = self.application(self.plan, slow)
        app.publish('抓取牛奶', 'shutdown-body')
        self.assertTrue(entered.wait(1))
        command = app.service.runtime.record['open_command_id']
        self.assertFalse(app.close(.1))
        self.assertEqual(app.service.runtime.record['open_command_id'], command)
        self.assertNotEqual(app.status()['state'], 'cancelled')
        second = self.application(self.plan)
        with self.assertRaises(Rejected):
            second.start()
        release.set()

    def test_reflection_persistence_deduplicates_after_restart(self):
        from brain.learning.reflection import persist_reflection
        root = Path(self.temp.name) / 'reflections'
        episode = {'task_id': 'durable-work', 'task': '观察现场', '_work_id': 'a' * 64}
        with patch('brain.learning.reflection._reflection_root', return_value=root):
            first = persist_reflection(episode, {'summary': 'first', 'new_rules': []})
            second = persist_reflection(episode, {'summary': 'late regenerated output', 'new_rules': []})
        self.assertEqual(first, second)
        self.assertEqual(len((root / 'candidates.jsonl').read_text().splitlines()), 1)

class GateWaitTests(unittest.TestCase):
    def test_control_is_serviced_while_navigation_gate_query_is_blocked(self):
        from brain.workers import RuntimeOwner, CooperativePort
        entered, release = threading.Event(), threading.Event()
        class Gate:
            @property
            def gate_open(self):
                entered.set(); release.wait(3)
                return True
        owner = RuntimeOwner(); io = WorkerPool('test-io', 2); control = WorkerPool('test-control', 1)
        port = CooperativePort(Gate(), owner, io, control)
        try:
            waiting = owner.submit(lambda: port.gate_open)
            self.assertTrue(entered.wait(1))
            self.assertEqual(owner.submit(lambda: 'control serviced', control=True).result(timeout=.5), 'control serviced')
            release.set()
            self.assertTrue(waiting.result(timeout=1))
        finally:
            release.set(); owner.close(); io.close(); control.close()
