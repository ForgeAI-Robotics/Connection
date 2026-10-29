"""Source-gate integration using temporary original ledgers and fake peers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from brain.adapters.source_gate import SourceGateClient, SourceGateNavigationAdapter, VERSION
from brain.adapters.http_client import HttpContractError
from brain.adapters.ports import build_port, target_identity
from brain.adapters.simulated_manipulation import HybridReceptionAdapter, SimulatedManipulationClient
from brain.kernel.source_gate import SourceGateControl
from brain.storage.tasks import KernelStore
from brain.app import create_service
from brain.application import BrainApplication
from brain.packages.reception_skill import BASELINE_PATH
from contracts.body_control import ControlError
from contracts.tasks import Rejected
from tests.brain.test_body_control import configuration, nav_request
from tests.brain.test_native_vla import FakeDream, now


class Gate:
    def __init__(self):
        self.source, self.instance, self.revision = 'navigation', 'sonic-1', 0
        self.puts, self.queries, self.requests = [], [], {}
        self.pending = self.lost = self.unavailable = self.corrupt = False

    def current(self):
        if self.unavailable:
            raise HttpContractError('RECEIVER_UNAVAILABLE', status_code=503)
        return dict(contract_version=VERSION, effective_source=self.source,
                    receiver_instance_id=self.instance, revision=self.revision,
                    pending_requests=int(self.pending), data_forwarding=False)

    def set(self, body):
        self.puts.append(deepcopy(body))
        changed = self.source != body['source']
        self.source = body['source']
        self.revision += int(changed)
        raw = dict(body, contract_version=VERSION, state='pending' if self.pending else 'applied',
                   changed=changed, effective_source=self.source, revision=self.revision)
        self.requests[body['request_id']] = raw
        if self.lost:
            raise HttpContractError('lost PUT response')
        return deepcopy(raw)

    def query(self, rid):
        self.queries.append(rid)
        raw = deepcopy(self.requests[rid])
        if self.corrupt:
            raw['request_id'] = 'another-request'
        return raw


class Nav(FakeDream):
    def __init__(self):
        super().__init__()
        self.posts, self.cancels = [], []
        self.http = type('HTTP', (), {})()

    def submit_navigation(self, body):
        self.posts.append(deepcopy(body))
        return super().submit_navigation(body)

    def cancel(self, task, command):
        self.cancels.append((task, command))
        return self.command(command)


class SourceGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = configuration(self.root)
        self.config['reception_real']['body_control']['mode'] = 'source_gate'
        self.config['brain'] = {'capture_timeline': False}
        self.pool = ThreadPoolExecutor(3)
        self.gate, self.nav = Gate(), Nav()
        self.control = SourceGateControl(self.config, self.root, self.pool, client=self.gate)
        self.control.start()
        self.port = SourceGateNavigationAdapter(self.nav, self.control)
        self.store = KernelStore(self.root)

    def tearDown(self):
        self.pool.shutdown(wait=True)
        self.control.close()
        self.temp.cleanup()

    def schedule(self, request, backend='reception_real_sim_manipulation'):
        old = self.store.load_state() or {}
        self.store.save_state(dict(task_id=request['body']['task_id'], revision=old.get('revision', 0),
            state='running', open_command_id=request['body']['command_id'],
            execution_backend=backend, execution_target=target_identity(self.config, backend)))

    def test_default_nav_and_continuous_goals_never_put_or_attach_authorization(self):
        for command in ('nav-1', 'nav-2'):
            request = nav_request(command)
            self.schedule(request)
            self.assertTrue(self.port.submit(command, request)['accepted'])
            self.assertEqual(self.nav.posts[-1], request['body'])
            self.assertTrue(self.port.query(command, request)['stopped'])
        self.assertEqual(self.gate.puts, [])

    def test_switch_from_none_is_pinned_and_only_once(self):
        self.gate.source = 'none'
        r = nav_request(); self.schedule(r)
        self.assertTrue(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(self.gate.puts[0]['receiver_instance_id'], 'sonic-1')
        self.assertEqual(self.gate.puts[0]['source'], 'navigation')
        self.assertTrue(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(len(self.gate.puts), 1)
        self.assertEqual(len(self.nav.posts), 1)

    def test_lost_put_only_queries_original_request(self):
        self.gate.source, self.gate.lost = 'none', True
        r = nav_request(); self.schedule(r)
        self.assertTrue(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(self.gate.queries, [self.gate.puts[0]['request_id']])
        self.assertEqual(len(self.gate.puts), 1)

    def test_pending_result_does_not_send_navigation_or_reissue_selection(self):
        original = self.gate.set
        def pending(body):
            self.gate.pending = True
            return original(body)
        self.gate.source = 'none'; self.gate.set = pending
        r = nav_request(); self.schedule(r)
        result = self.port.submit('nav-1', r)
        self.assertFalse(result['accepted'])
        self.assertIsNotNone(result['submission_rejected'])
        self.assertEqual(self.nav.posts, [])
        self.assertEqual(len(self.gate.puts), 1)
        self.assertGreater(len(self.gate.queries), 0)
        self.port.query('nav-1', r)
        self.assertEqual(len(self.gate.puts), 1)

    def test_unavailable_receiver_is_not_navigation_readiness(self):
        self.gate.unavailable = True
        r = nav_request(); self.schedule(r)
        self.assertFalse(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(self.nav.posts, [])

    def test_wrong_receipt_and_receiver_restart_prevent_goal(self):
        self.gate.source, self.gate.lost, self.gate.corrupt = 'none', True, True
        r = nav_request(); self.schedule(r)
        self.assertFalse(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(self.nav.posts, [])

    def test_historical_applied_cannot_certify_changed_current_source(self):
        self.gate.source = 'none'
        original = self.gate.set
        def changed(body):
            raw = original(body)
            self.gate.source = 'vla'; self.gate.revision += 1
            return raw
        self.gate.set = changed
        r = nav_request(); self.schedule(r)
        self.assertFalse(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(self.nav.posts, [])

    def test_late_selection_after_task_cancel_never_dispatches_goal(self):
        self.gate.source = 'none'
        entered, release = threading.Event(), threading.Event()
        original = self.gate.set
        def delayed(body):
            entered.set(); release.wait(2)
            return original(body)
        self.gate.set = delayed
        r = nav_request(); self.schedule(r)
        future = self.pool.submit(self.port.submit, 'nav-1', r)
        self.assertTrue(entered.wait(1))
        record = self.store.load_state(); record.update(state='cancelled', dispatch_closed=True)
        self.store.save_state(record)
        self.control.cancel_task('test-task')
        release.set(); future.result(2)
        self.control._futures['test-task'].result(2)
        self.assertEqual(self.nav.posts, [])
        self.assertEqual(self.gate.source, 'none')
        task = self.control._read()['tasks']['test-task']
        self.assertTrue(task['navigation_not_submitted'])
        self.assertIsNone(task['navigation_stopped'])

    def test_cancel_keeps_nav_stop_and_gate_closed_separate_and_is_idempotent(self):
        r = nav_request(); self.schedule(r); self.port.submit('nav-1', r)
        self.nav.commands['nav-1']['state'] = 'cancelled'
        self.nav.commands['nav-1']['result']['navigation_stopped'] = False
        record = self.store.load_state(); record.update(state='cancelled', dispatch_closed=True)
        self.store.save_state(record)
        with patch('brain.kernel.source_gate.DreamClient', return_value=self.nav):
            self.control.cancel_task('test-task'); self.control._futures['test-task'].result(2)
            row = self.control._read()['tasks']['test-task']
            self.assertTrue(row['gate_closed']); self.assertFalse(row['navigation_stopped'])
            self.nav.commands['nav-1']['result']['navigation_stopped'] = True
            self.control.request_cleanup('test-task', 'test-task')
            self.control._futures['test-task'].result(2)
        self.assertTrue(self.control._read()['tasks']['test-task']['completed'])
        self.assertEqual(len(self.nav.cancels), 1)
        self.assertEqual([b['source'] for b in self.gate.puts], ['none'])

    def test_restart_preserves_original_intent_and_never_replays_put_or_goal(self):
        self.gate.source, self.gate.lost = 'none', True
        r = nav_request(); self.schedule(r); self.port.submit('nav-1', r)
        self.control.close()
        self.control = SourceGateControl(self.config, self.root, self.pool, client=self.gate)
        self.port = SourceGateNavigationAdapter(self.nav, self.control)
        self.control.start()
        self.assertTrue(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(len(self.gate.puts), 1); self.assertEqual(len(self.nav.posts), 1)

    def test_old_ledger_preserved_and_target_identity_changes(self):
        with self.control.store.writer():
            state = self.control.store.read()
            state['leases']['old'] = {'state': 'unknown', 'evidence': {'failure': 'original'}}
            self.control.store.write(state)
        self.control.close()
        self.control = SourceGateControl(self.config, self.root, self.pool, client=self.gate)
        self.control.start()
        self.assertEqual(self.control.store.read()['leases']['old']['state'], 'unknown')
        unified = deepcopy(self.config); unified['reception_real']['body_control'].pop('mode')
        self.assertNotEqual(target_identity(unified, 'reception_real'), target_identity(self.config, 'reception_real'))

    def test_nav_only_adapter_still_refuses_real_vla(self):
        r = nav_request(); r['skill'] = 'open_fridge'; self.schedule(r)
        self.assertFalse(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(self.nav.posts, [])

    def test_full_runtime_mixed_workflow_uses_nav_and_local_simulation_only(self):
        self.config.update(brain={'reception_backend': 'real', 'capture_timeline': False,
                                  'reasoning': {'recovery_advice': False}}, reflection={'enabled': False})
        self.config['reception_real']['skill_file'] = str(BASELINE_PATH.with_name('reception_single_can_v2.yaml'))
        port = HybridReceptionAdapter(self.nav, SimulatedManipulationClient(self.nav, delay_sec=0), navigation=self.port)
        service = create_service(self.config, model=lambda _: self.fail('No model'), port_factory=lambda _: port)
        service.body_control = self.control
        with patch.dict(os.environ, {'FQ_EXECUTION_STATE': str(self.root/'execution.json'),
                'FQ_EXECUTION_LOCK': str(self.root/'execution.lock'), 'FQ_EXECUTION_BLOCK': str(self.root/'execution.blocked')}):
            service.publish('开始接待（导航真机＋操控模拟）', 'test-task', options={'reflect': False})
        self.assertEqual(service.runtime.state, 'succeeded', service.runtime.public_status())
        self.assertEqual(len(self.nav.posts), 5)
        self.assertEqual(len(port.vla._commands), 2)
        self.assertEqual(self.gate.puts, [])

    def test_application_selects_new_manager_without_legacy_driver(self):
        self.control.close()
        service = create_service(self.config, model=lambda _: self.fail('No model'))
        with patch('brain.kernel.source_gate.SourceGateClient', return_value=self.gate), \
                patch('brain.adapters.body_control.ControlDriver', side_effect=AssertionError('old driver')):
            app = BrainApplication(service)
            try:
                self.assertEqual(app.body_control.mode, 'source_gate')
                port = build_port(self.config, 'reception_real_sim_manipulation', body_control=app.body_control)
                self.assertIsInstance(port.port.navigation, SourceGateNavigationAdapter)
                self.assertEqual(port.port.dream.http.auth_token_env, 'BRAIN_CONTROL_TOKEN')
                self.assertEqual(app.status()['control_recovery']['mode'], 'source_gate')
            finally:
                app.close()

    def test_old_cleanup_cannot_close_new_task_after_it_completed(self):
        r = nav_request(); self.schedule(r); self.port.submit('nav-1', r)
        record = self.store.load_state(); record.update(state='cancelled', dispatch_closed=True)
        self.store.save_state(record)
        with patch('brain.kernel.source_gate.DreamClient', return_value=self.nav):
            self.control.cancel_task('test-task'); self.control._futures['test-task'].result(2)
        next_request = nav_request('nav-2')
        next_request['task_id'] = next_request['body']['task_id'] = 'new-task'
        self.schedule(next_request)
        self.assertTrue(self.port.submit('nav-2', next_request)['accepted'])
        count = len(self.gate.puts)
        self.control.cancel_task('test-task')
        self.assertEqual(len(self.gate.puts), count)
        self.assertEqual(self.gate.source, 'navigation')

    def test_receiver_restart_after_put_is_not_success(self):
        self.gate.source = 'none'
        original = self.gate.set
        def restart(body):
            result = original(body)
            self.gate.instance = 'sonic-2'
            return result
        self.gate.set = restart
        r = nav_request(); self.schedule(r)
        self.assertFalse(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(self.nav.posts, [])

    def test_pending_selection_survives_brain_restart_without_put_replay(self):
        self.gate.source = 'none'
        original = self.gate.set
        def pending(body):
            self.gate.pending = True
            return original(body)
        self.gate.set = pending
        r = nav_request(); self.schedule(r)
        self.assertFalse(self.port.submit('nav-1', r)['accepted'])
        self.control.close()
        self.control = SourceGateControl(self.config, self.root, self.pool, client=self.gate)
        self.port = SourceGateNavigationAdapter(self.nav, self.control)
        self.gate.pending = False
        for receipt in self.gate.requests.values():
            receipt['state'] = 'applied'
        self.assertTrue(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(len(self.gate.puts), 1)

    def test_nav_timeout_keeps_original_command_and_never_reposts(self):
        r = nav_request(); self.schedule(r)
        original = self.nav.submit_navigation
        def lost(body):
            original(body)
            raise HttpContractError('unknown original navigation POST')
        self.nav.submit_navigation = lost
        self.assertTrue(self.port.submit('nav-1', r)['unclear'])
        self.assertTrue(self.port.query('nav-1', r)['stopped'])
        self.port.submit('nav-1', r)
        self.assertEqual(len(self.nav.posts), 1)

    def test_historical_failed_cancel_does_not_block_new_navigation(self):
        old = dict(task_id='old-task', completed=False, gate_closed=False,
                   navigation_stopped=False, error='NAV offline; RECEIVER_UNAVAILABLE')
        self.control._update(lambda g: g['tasks'].__setitem__('old-task', deepcopy(old)))
        self.gate.source = 'none'
        r = nav_request(); self.schedule(r)
        self.assertTrue(self.port.submit('nav-1', r)['accepted'])
        self.assertEqual(self.control._read()['tasks']['old-task'], old)
        self.assertEqual(self.gate.source, 'navigation')
        self.assertFalse(self.control.recovery_status()['pending'])
        self.assertEqual(self.control.recovery_status()['previous_exit_unconfirmed'], 1)

    def test_queued_old_cleanup_never_sends_stop_or_none_after_new_task(self):
        r = nav_request(); self.schedule(r); self.port.submit('nav-1', r)
        self.control._update(lambda g: g['tasks'].__setitem__('test-task',
            dict(task_id='test-task', completed=False, navigation_stopped=False, error='original unknown')))
        with self.control._lock:
            queued = self.pool.submit(self.control._cleanup, 'test-task')
            next_request = nav_request('nav-2')
            next_request['task_id'] = next_request['body']['task_id'] = 'new-task'
            self.schedule(next_request)
            self.assertTrue(self.port.submit('nav-2', next_request)['accepted'])
        queued.result(2)
        self.assertEqual(self.nav.cancels, [])
        self.assertEqual(self.gate.puts, [])
        old = self.control._read()['tasks']['test-task']
        self.assertFalse(old['completed']); self.assertFalse(old['navigation_stopped'])
        self.assertTrue(old['cleanup_superseded']); self.assertEqual(old['error'], 'original unknown')

    def test_rechecking_old_cancel_cannot_close_new_task(self):
        self.control._update(lambda g: g['tasks'].__setitem__('old-task', dict(task_id='old-task', completed=False)))
        r = nav_request(); self.schedule(r); self.port.submit('nav-1', r)
        with patch('brain.kernel.source_gate.DreamClient', side_effect=AssertionError('old cleanup must not call NAV')):
            self.control.request_cleanup('old-task', 'old-task')
            self.control._futures['old-task'].result(2)
        self.assertEqual(self.gate.puts, [])
        self.assertEqual(self.gate.source, 'navigation')

    def test_restart_keeps_old_cancel_unknown_without_blocking_new_task(self):
        self.control._update(lambda g: g['tasks'].__setitem__('old-task', dict(task_id='old-task', completed=False)))
        self.control.close()
        self.control = SourceGateControl(self.config, self.root, self.pool, client=self.gate)
        self.port = SourceGateNavigationAdapter(self.nav, self.control)
        r = nav_request(); self.schedule(r)
        self.assertTrue(self.port.submit('nav-1', r)['accepted'])
        self.assertFalse(self.control._read()['tasks']['old-task']['completed'])
        self.assertFalse(self.control.recovery_status()['pending'])


if __name__ == '__main__':
    unittest.main()
