from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brain.adapters.body_control import ControlledBodyAdapter
from brain.adapters.http_client import HttpContractError
from brain.kernel.body_control import BodyControl
from brain.service import BrainService
from contracts.body_control import ControlError
from contracts.tasks import Rejected
from tests.brain import test_body_control as fixture


class ControlCleanupTests(unittest.TestCase):
    setUp = fixture.AuthorityTests.setUp
    schedule = fixture.AuthorityTests.schedule

    def cancelled(self):
        record = self.store.load_state()
        record.update(state='cancelled', dispatch_closed=True, control_request='cancel')
        self.store.save_state(record)

    def ready_nav(self, *, business=False):
        request = fixture.nav_request()
        self.schedule(request)
        ref = self.authority.prepare_navigation(request)
        self.lease = ref['lease_id']
        if business:
            body = dict(request['body'], control_authorization=ref)
            self.authority.record_submission(request, body)
            self.wire.nav_command(body)
        return request

    def cleanup(self):
        result = self.authority.request_cleanup('test-task', self.lease)
        self.authority._cleanup_future.result(timeout=5)
        return self.authority.recovery_status()

    def failed_prepare(self, *, closed):
        original = self.wire.call
        def call(service, method, path, payload=None):
            if method == 'POST' and service == 'nav' and payload.get('action') in {'prepare','yield'}:
                self.wire.calls.append((service, method, path, deepcopy(payload)))
                result = dict(deepcopy(payload), state='unknown', accepted=False, blockers=['NAV_EFFECT_UNCONFIRMED'])
                if payload['action'] == 'prepare':
                    result['execution_query'] = {'record': {'controller_instance_id':'sonic-test'}, 'native_steps':{}}
                    self.wire.nav.update(navigation_stopped=True, output_fenced=closed,
                        brain_control={'operation_pending':False, 'control_association':{
                            key:deepcopy(payload[key]) for key in ('task_id','command_id','control_authorization')}})
                self.wire.operations[payload['request_id']] = result
                return deepcopy(result)
            return original(service, method, path, payload)
        self.wire.call = call
        request = fixture.nav_request()
        self.schedule(request)
        with self.assertRaises(ControlError):
            self.authority.prepare_navigation(request)
        self.lease = self.authority.store.read()['current']
        return request

    def test_cancelled_prepared_nav_releases_original_without_business_or_new_grant(self):
        request = self.ready_nav()
        self.cancelled()
        before = self.store.state_path.read_bytes()
        epoch = self.authority.store.read()['epoch']
        result = self.cleanup()
        self.assertFalse(result['blocked'], result)
        self.assertEqual(self.store.state_path.read_bytes(), before)
        self.assertEqual(self.authority.store.read()['epoch'], epoch)
        self.assertTrue(self.authority.business_not_submitted_after_cleanup('test-task','nav-1'))
        port = ControlledBodyAdapter(SimpleNamespace(), SimpleNamespace(), self.authority)
        observed = port.query('nav-1', request)
        self.assertFalse(observed['started'])
        self.assertTrue(observed['resources_released'])
        self.assertFalse(any(path == '/v1/navigation/goals' for _,_,path,_ in self.wire.calls))

    def test_failed_prepare_reconciles_current_exit_without_repeating_prepare(self):
        self.failed_prepare(closed=True)
        self.cancelled()
        acquire_before = deepcopy(next(op for op in self.authority.store.read()['requests'].values()
                                       if op['body']['action']=='acquire'))
        result = self.cleanup()
        self.assertFalse(result['blocked'], result)
        state = self.authority.store.read()
        self.assertEqual(next(op for op in state['requests'].values() if op['body']['action']=='acquire'), acquire_before)
        self.assertEqual(sum(method=='POST' and payload.get('action')=='prepare'
            for _,method,_,payload in self.wire.calls if payload), 1)
        self.assertTrue(state['leases'][self.lease]['evidence']['reconciled'])

    def test_unconfirmed_output_stays_blocked_and_recheck_only_queries_original_yield(self):
        self.failed_prepare(closed=False)
        self.cancelled()
        result = self.cleanup()
        self.assertTrue(result['blocked'])
        self.assertIn('output_fenced', result['source']['cleanup_error']['message'])
        with self.assertRaises(ControlError): self.authority.assert_new_task_allowed()
        self.wire.nav['output_fenced'] = True
        self.assertFalse(self.cleanup()['blocked'])
        posts = [payload['request_id'] for _,method,_,payload in self.wire.calls
                 if method=='POST' and payload.get('action')=='yield']
        self.assertEqual(len(posts), 1)

    def test_missing_original_native_state_or_changed_controller_never_releases(self):
        self.failed_prepare(closed=True)
        prepare = next(value for value in self.wire.operations.values() if value.get('action')=='prepare')
        prepare['execution_query']['native_steps'] = {'activate':{'state':'unknown'}}
        self.cancelled()
        self.assertTrue(self.cleanup()['blocked'])
        prepare['execution_query']['native_steps'] = {}
        self.wire.nav['controller_instance_id'] = 'new-controller'
        self.assertTrue(self.cleanup()['blocked'])
        self.assertIsNotNone(self.authority.store.read()['current'])

    def rebooted_prepare(self):
        self.failed_prepare(closed=True)
        old = '4349cba7-10a1-4597-b4bb-f94a8cecc36f'
        new = '1777fba5-5e87-4f3c-9973-a8e23ef6cab0'
        prepare = next(value for value in self.wire.operations.values() if value.get('action') == 'prepare')
        prepare['historical'] = True
        record = prepare['execution_query']['record']
        record.update(controller_instance_id=old + ':7513:23190', owner_instance=old + ':10853:29240',
                      request={key:deepcopy(prepare[key]) for key in ('task_id','command_id','request_id','control_authorization')})
        self.wire.nav['controller_instance_id'] = new + ':21718:25252'
        self.wire.vla['controller_instance_id'] = new + ':21718:25252'
        self.cancelled()
        return prepare, record

    def test_verified_reboot_releases_old_source_and_preserves_original_failure(self):
        self.rebooted_prepare()
        before = deepcopy(next(op for op in self.authority.store.read()['requests'].values()
                               if op['body']['action'] == 'acquire'))
        self.assertFalse(self.cleanup()['blocked'])
        state = self.authority.store.read()
        proof = state['leases'][self.lease]['evidence']['restart_exit_evidence']
        self.assertTrue(proof['old_boot_processes_cannot_remain'])
        self.assertNotEqual(proof['old_boot_id'], proof['current_boot_id'])
        self.assertEqual(before, next(op for op in state['requests'].values() if op['body']['action']=='acquire'))
        self.assertTrue(self.authority.business_not_submitted_after_cleanup('test-task', 'nav-1'))

    def test_same_boot_pid_change_is_not_reboot_proof(self):
        _, record = self.rebooted_prepare()
        boot = record['controller_instance_id'].split(':')[0]
        self.wire.nav['controller_instance_id'] = boot + ':9999:88888'
        self.wire.vla['controller_instance_id'] = boot + ':9999:88888'
        self.assertTrue(self.cleanup()['blocked'])

    def test_reboot_requires_matching_original_owner_and_request(self):
        prepare, record = self.rebooted_prepare()
        original = deepcopy(record)
        for key, value in [('owner_instance', 'missing-boot'),
                           ('request', dict(record['request'], command_id='other'))]:
            record.update(deepcopy(original)); record[key] = value
            with self.subTest(key=key):
                self.assertTrue(self.cleanup()['blocked'])
        record.update(original); prepare['historical'] = False
        self.assertTrue(self.cleanup()['blocked'])

    def test_reboot_retirement_is_independent_of_next_vla_readiness(self):
        self.rebooted_prepare()
        self.wire.nav['hold_verified'] = None
        original = self.wire.call
        def call(service, method, path, payload=None):
            if service == 'vla':
                raise HttpContractError('VLA is offline')
            return original(service, method, path, payload)
        self.wire.call = call
        self.assertFalse(self.cleanup()['blocked'])
        state = self.authority.store.read()
        self.assertFalse(state['leases'][self.lease]['evidence']['physical_readiness_verified'])
        with self.assertRaises(HttpContractError):
            self.driver.idle()  # Retirement never grants new motion while VLA is offline.

    def test_reboot_retirement_still_requires_output_fence_and_stable_new_instance(self):
        self.rebooted_prepare()
        self.wire.nav['output_fenced'] = False
        self.assertTrue(self.cleanup()['blocked'])
        self.wire.nav['output_fenced'] = True
        original = self.wire.call
        def call(service, method, path, payload=None):
            result = original(service, method, path, payload)
            if service == 'relay' and method == 'POST' and payload.get('action') == 'close':
                self.wire.nav['controller_instance_id'] = 'another-controller'
            return result
        self.wire.call = call
        self.assertTrue(self.cleanup()['blocked'])

    def test_foreign_task_and_stale_page_cannot_stop_current_owner(self):
        self.ready_nav()
        self.cancelled()
        before = len(self.wire.calls)
        for task, lease in [('other-task',self.lease), ('test-task','other-lease')]:
            with self.assertRaises(ControlError): self.authority.request_cleanup(task, lease)
        self.assertEqual(len(self.wire.calls), before)

    def test_busy_runtime_cannot_be_recovered_without_cancel_or_pause(self):
        self.ready_nav()
        with self.assertRaisesRegex(ControlError, '取消或暂停'):
            self.authority.request_cleanup('test-task',self.lease)

    def test_actual_business_cancel_is_journaled_once_and_original_result_is_queried(self):
        self.ready_nav(business=True)
        original = self.wire.call
        def call(service, method, path, payload=None):
            if path == '/v1/navigation/cancel':
                self.wire.calls.append((service, method, path, deepcopy(payload)))
                self.wire.commands['nav-1'].update(state='cancelled', result={'navigation_stopped':True})
                raise HttpContractError('reply lost after cancel')
            return original(service, method, path, payload)
        self.wire.call = call
        self.cancelled()
        self.assertFalse(self.cleanup()['blocked'])
        self.assertEqual(sum(path=='/v1/navigation/cancel' for _,_,path,_ in self.wire.calls), 1)
        self.assertFalse(self.authority.business_not_submitted_after_cleanup('test-task','nav-1'))
        self.assertEqual(self.wire.commands['nav-1']['state'], 'cancelled')

    def test_original_business_missing_cannot_be_treated_as_never_executed(self):
        self.ready_nav(business=True)
        original = self.wire.call
        def call(service, method, path, payload=None):
            if path == '/v1/navigation/cancel' or path == '/v1/commands/nav-1':
                raise HttpContractError('unknown command',status_code=404)
            return original(service, method, path, payload)
        self.wire.call = call
        self.driver.timeout = .02; self.driver.poll = .002
        self.cancelled()
        self.assertTrue(self.cleanup()['blocked'])
        self.assertFalse(self.authority.business_not_submitted_after_cleanup('test-task','nav-1'))

    def test_unused_request_behind_old_source_is_sealed_without_deleting_failure(self):
        self.failed_prepare(closed=True)
        request = fixture.nav_request('nav-2')
        self.schedule(request)
        with self.assertRaises(ControlError): self.authority.prepare_navigation(request)
        self.cancelled()
        self.assertFalse(self.cleanup()['blocked'])
        state = self.authority.store.read()
        row = next(row for row in state['leases'].values() if row['command_id']=='nav-2')
        self.assertTrue(row['evidence']['not_acquired'])
        self.assertEqual(next(op for op in state['requests'].values()
            if op['body']['command_id']=='nav-2')['receipt']['error']['code'], 'SOURCE_NOT_RELEASED')

    def test_new_task_is_rejected_before_replacing_cancelled_task_ledger(self):
        self.ready_nav(); self.cancelled()
        service = BrainService(self.config, None, None)
        service.body_control = self.authority
        before = self.store.state_path.read_bytes()
        with self.assertRaisesRegex(Rejected, '旧控制'):
            service.publish('开始接待', 'new-task')
        self.assertEqual(self.store.state_path.read_bytes(), before)

    def test_stale_idle_before_grant_never_reserves_control(self):
        self.wire.stale = True
        request = fixture.nav_request(); self.schedule(request)
        with self.assertRaises(ControlError): self.authority.prepare_navigation(request)
        state = self.authority.store.read()
        self.assertIsNone(state['current'])
        self.assertEqual(next(iter(state['leases'].values()))['state'], 'released')
        self.assertFalse(any(method=='POST' for _,method,_,_ in self.wire.calls))
        self.assertEqual(next(iter(state['requests'].values()))['receipt']['error']['code'], 'STALE_EVIDENCE')
        self.assertTrue(self.authority.business_not_submitted_after_cleanup('test-task', 'nav-1'))

    def failed_relay_before_nav(self):
        self.wire.relay.update(enabled=False, recovery_required=False,
            local_subscriber_queue_closed=True, relay_instance_id='relay-test',
            applied_authorization={'command_id': 'older-released-command'})
        self.wire.nav['brain_control'] = {'control_association': {'command_id': 'older-released-command'}}
        call = self.wire.call
        def fail_enable(service, method, path, payload=None):
            if method == 'POST' and service == 'relay' and payload['action'] == 'enable_navigation':
                self.wire.calls.append((service, method, path, deepcopy(payload)))
                result = self.wire.observed(dict(request=deepcopy(payload), status='failed', state='failed', completed=False,
                    error={'code':'RELAY_EXECUTION_NOT_READY', 'message':'NATIVE_CURRENT_SAMPLE_STALE'},
                    result={'port_bound':False, 'forwarding':False, 'local_subscriber_queue_closed':True},
                    bound=False, forwarding_enabled=False, action_port_released=True))
                self.wire.operations[payload['request_id']] = result
                return deepcopy(result)
            return call(service, method, path, payload)
        self.wire.call = fail_enable
        request = fixture.nav_request(); self.schedule(request)
        with self.assertRaises(ControlError): self.authority.prepare_navigation(request)
        state = self.authority.store.read(); self.lease = state['current']
        self.enable_id = next(op['children']['relay-enable']['wire']['payload']['request_id']
            for op in state['requests'].values() if 'relay-enable' in op['children'])
        self.cancelled()

    def test_failed_relay_with_old_nav_association_retires_without_nav_or_reenable(self):
        self.failed_relay_before_nav()
        before = deepcopy(self.authority.store.read()['requests'])
        calls = len(self.wire.calls)
        self.assertFalse(self.cleanup()['blocked'])
        cleanup_calls = self.wire.calls[calls:]
        self.assertTrue(cleanup_calls)
        self.assertTrue(all(service == 'relay' and method == 'GET'
                            for service, method, _, _ in cleanup_calls))
        state = self.authority.store.read()
        for key, op in before.items(): self.assertEqual(state['requests'][key], op)
        self.assertIsNone(state['current'])
        self.assertTrue(state['leases'][self.lease]['evidence']['navigation_prepare_not_sent'])
        self.assertFalse(state['leases'][self.lease]['evidence']['physical_readiness_verified'])

    def test_unknown_relay_enable_stays_blocked_without_nav_fallback(self):
        self.failed_relay_before_nav()
        self.wire.operations[self.enable_id].update(state='unknown', status='unknown')
        calls = len(self.wire.calls)
        self.assertTrue(self.cleanup()['blocked'])
        self.assertTrue(all(service == 'relay' and method == 'GET'
                            for service, method, _, _ in self.wire.calls[calls:]))

    def test_relay_receipt_wrong_identity_cannot_release_grant(self):
        self.failed_relay_before_nav()
        self.wire.operations[self.enable_id]['command_id'] = 'different-command'
        self.assertTrue(self.cleanup()['blocked'])
        self.assertEqual(self.authority.store.read()['current'], self.lease)

    def test_unreleased_foreign_relay_cannot_be_closed_by_failed_new_grant(self):
        self.failed_relay_before_nav()
        self.wire.relay.update(bound=True, forwarding_enabled=True, action_port_released=False)
        calls = len(self.wire.calls)
        self.assertTrue(self.cleanup()['blocked'])
        self.assertFalse(any(method == 'POST' for _,method,_,_ in self.wire.calls[calls:]))

    def test_unclosed_relay_queue_does_not_count_as_exit(self):
        self.failed_relay_before_nav()
        self.wire.relay['local_subscriber_queue_closed'] = False
        self.assertTrue(self.cleanup()['blocked'])

    def test_relay_recovery_required_does_not_count_as_exit(self):
        self.failed_relay_before_nav()
        self.wire.relay['recovery_required'] = True
        self.assertTrue(self.cleanup()['blocked'])

    def test_missing_original_relay_request_cannot_be_assumed_unexecuted(self):
        self.failed_relay_before_nav()
        del self.wire.operations[self.enable_id]
        self.assertTrue(self.cleanup()['blocked'])

    def test_late_terminal_relay_failure_can_be_requeried_without_repeating_effects(self):
        self.failed_relay_before_nav()
        original = deepcopy(self.wire.operations[self.enable_id])
        self.wire.operations[self.enable_id].update(state='pending', status='pending')
        calls = len(self.wire.calls)
        self.assertTrue(self.cleanup()['blocked'])
        self.wire.operations[self.enable_id] = original
        self.assertFalse(self.cleanup()['blocked'])
        self.assertFalse(any(method == 'POST' for _,method,_,_ in self.wire.calls[calls:]))

    def test_legacy_wrong_nav_yield_is_preserved_but_not_repeated(self):
        self.failed_relay_before_nav()
        state = self.authority.store.read(); row = state['leases'][self.lease]
        rid = self.authority._reserve_cleanup_release(row)
        state = self.authority.store.read()
        state['requests'][rid]['children']['nav-yield'] = {'state':'pending', 'wire':{'query_path':'/old-yield'}}
        self.authority.store.write(state)
        self.authority._finish(rid, None, ControlError('IDENTITY_MISMATCH', 'old failed cleanup'))
        calls = len(self.wire.calls)
        self.assertFalse(self.cleanup()['blocked'])
        self.assertEqual(self.authority.store.read()['requests'][rid]['children']['nav-yield'],
                         state['requests'][rid]['children']['nav-yield'])
        self.assertTrue(all(service == 'relay' and method == 'GET'
                            for service,method,_,_ in self.wire.calls[calls:]))

    def test_cancel_during_relay_enable_closes_original_relay_without_nav_prepare(self):
        self.wire.relay.update(enabled=False, recovery_required=False,
            local_subscriber_queue_closed=True, relay_instance_id='relay-test')
        request = fixture.nav_request(); self.schedule(request)
        pool = ThreadPoolExecutor(2); self.addCleanup(pool.shutdown)
        self.authority.executor = pool
        self.wire.block_action = 'enable_navigation'; self.wire.drop_action = 'close'
        errors = []
        def acquire():
            try: self.authority.prepare_navigation(request)
            except ControlError as exc: errors.append(exc)
        thread = threading.Thread(target=acquire); thread.start()
        self.assertTrue(self.wire.entered.wait(2))
        self.lease = self.authority.store.read()['current']; self.cancelled()
        self.authority.request_cleanup('test-task', self.lease)
        self.wire.unblock.set(); thread.join(3)
        self.authority._cleanup_future.result(timeout=3)
        self.assertFalse(thread.is_alive()); self.assertTrue(errors)
        self.assertFalse(self.authority.recovery_status()['blocked'])
        actions = [payload['action'] for _,method,_,payload in self.wire.calls if method=='POST']
        self.assertEqual(actions, ['enable_navigation','close'])

    def test_current_relay_evidence_must_be_fresh(self):
        self.failed_relay_before_nav(); self.wire.stale = True
        self.assertTrue(self.cleanup()['blocked'])

    def test_relay_restart_during_exit_check_keeps_grant_unconfirmed(self):
        self.failed_relay_before_nav()
        call = self.wire.call; reads = []
        def restart(service, method, path, payload=None):
            result = call(service, method, path, payload)
            if service == 'relay' and path.startswith('/v1/relay/status'):
                reads.append(path)
                if len(reads) > 1: result['relay_instance_id'] = 'restarted-relay'
            return result
        self.wire.call = restart
        self.assertTrue(self.cleanup()['blocked'])

    def test_sent_nav_prepare_never_uses_relay_only_exit(self):
        self.failed_relay_before_nav()
        state = self.authority.store.read()
        acquire = next(op for op in state['requests'].values() if op['body']['action']=='acquire')
        acquire['children']['nav-prepare'] = {'state':'pending', 'wire':{}}
        self.assertIsNone(self.authority._relay_only_prepare(state, state['leases'][self.lease]))

    def test_upgrade_retires_proven_unissued_intent_without_touching_peers_or_failure(self):
        self.test_stale_idle_before_grant_never_reserves_control()
        state = self.authority.store.read()
        key = next(iter(state['leases']))
        state['leases'][key].update(state='recovery_required', evidence={})
        state['current'] = key
        self.authority.store.write(state)
        before_requests = deepcopy(state['requests'])
        calls = len(self.wire.calls)
        self.authority.close()
        restored = BodyControl(self.config, self.root, self.driver, fixture.Immediate())
        self.addCleanup(restored.close); restored.start()
        after = restored.store.read()
        self.assertIsNone(after['current'])
        self.assertEqual(after['requests'], before_requests)
        self.assertEqual(len(self.wire.calls), calls)

    def test_upgrade_never_retires_a_published_grant_based_on_stale_error_alone(self):
        self.test_stale_idle_before_grant_never_reserves_control()
        state = self.authority.store.read(); key = next(iter(state['leases']))
        state['leases'][key].update(state='recovery_required', evidence={}, grant_published=True)
        state['current'] = key; self.authority.store.write(state)
        self.authority.close()
        restored = BodyControl(self.config, self.root, self.driver, fixture.Immediate())
        self.addCleanup(restored.close); restored.start()
        self.assertEqual(restored.store.read()['current'], key)

    def test_duplicate_cleanup_is_single_flight_and_revokes_permission_immediately(self):
        self.ready_nav(); self.cancelled()
        pool = ThreadPoolExecutor(2); self.addCleanup(pool.shutdown)
        self.authority.executor = pool
        self.wire.block_action = 'yield'
        self.authority.request_cleanup('test-task', self.lease)
        self.assertTrue(self.wire.entered.wait(2))
        status = self.authority.request_cleanup('test-task', self.lease)
        self.assertTrue(status['pending'])
        self.assertFalse(self.authority.query(self.lease, observation_id='check-1')['authorization_current'])
        self.wire.unblock.set(); self.authority._cleanup_future.result(timeout=3)
        self.assertEqual(sum(method=='POST' and payload.get('action')=='yield'
            for _,method,_,payload in self.wire.calls if payload), 1)

    def test_cancel_during_prepare_does_not_commit_late_authorization_or_goal(self):
        request = fixture.nav_request(); self.schedule(request)
        pool = ThreadPoolExecutor(2); self.addCleanup(pool.shutdown)
        self.authority.executor = pool
        self.wire.block_action = 'prepare'
        outcome = []
        def prepare():
            try:self.authority.prepare_navigation(request)
            except ControlError as exc:outcome.append(exc)
        thread = threading.Thread(target=prepare); thread.start()
        self.assertTrue(self.wire.entered.wait(2))
        self.lease = self.authority.store.read()['current']
        self.cancelled()
        self.authority.request_cleanup('test-task', self.lease)
        self.assertFalse(self.authority.query(self.lease, observation_id='check-1')['authorization_current'])
        self.wire.unblock.set(); thread.join(3); self.authority._cleanup_future.result(timeout=4)
        self.assertTrue(outcome)
        self.assertFalse(self.authority.recovery_status()['blocked'])
        self.assertNotIn('business_submission',self.authority.store.read()['leases'][self.lease])

    def test_vla_cleanup_allows_release_callback_and_never_restores_navigation(self):
        self.wire.nav_command(fixture.nav_request()['body'])
        request = fixture.vla_request()
        self.schedule(request)
        row = self.authority.register(request)
        self.authority.record_submission(request, request['body'])
        grant = self.authority.submit(dict(self.authority.identity(row), action='acquire', request_id='vla-acquire',
            navigation_proof=request['body']['navigation_proof']))
        self.assertEqual(grant['state'], 'authorized')
        self.lease = grant['lease_id']
        release = fixture.AuthorityTests.release_vla(self, grant)
        original = self.driver.stop_vla
        def stop(row, exchange):
            result = original(row, exchange)
            self.assertEqual(self.authority.submit(release)['state'], 'released')
            return result
        self.driver.stop_vla = stop
        self.cancelled()
        self.assertFalse(self.cleanup()['blocked'])
        self.assertFalse(any(method=='POST' and payload.get('action')=='enable_navigation'
            for _,method,_,payload in self.wire.calls if payload))

    def test_late_cleanup_result_cannot_write_new_authority_ledger(self):
        self.ready_nav(); self.cancelled()
        pool = ThreadPoolExecutor(2); self.addCleanup(pool.shutdown)
        self.authority.executor = pool
        self.wire.block_action = 'yield'
        self.authority.request_cleanup('test-task', self.lease)
        self.assertTrue(self.wire.entered.wait(2))
        with self.authority.store.writer():
            state = self.authority.store.read()
            state['authority_id'] = 'new-authority'
            self.authority.store.write(state)
        before = self.authority.store.path.read_bytes()
        self.wire.unblock.set(); self.authority._cleanup_future.result(timeout=3)
        self.assertEqual(self.authority.store.path.read_bytes(), before)


class CleanupApplicationTests(unittest.TestCase):
    def test_explicit_current_exit_check_is_forwarded_without_continuing_task(self):
        from brain.application import BrainApplication
        app = BrainApplication.__new__(BrainApplication)
        app.start = lambda: None; app.body_control = Mock()
        app.recover_control('task','old',new_exit_check=True)
        app.body_control.request_cleanup.assert_called_once_with('task','old',new_exit_check=True)

    def test_runtime_cancel_event_also_cleans_up_confirmed_advice_cancellation(self):
        from brain.application import BrainApplication
        app = BrainApplication.__new__(BrainApplication)
        app.config = {'brain': {'capture_timeline': False}}
        app.owner = Mock(); app._execution_ports = []
        app.body_control = Mock()
        app.body_control.recovery_status.return_value = {'blocked':True,'source':{'lease_id':'old'}}
        with patch('brain.application.emit'):
            app._event('task_cancelled', {'task_id':'task','state':'cancelled'})
        app.body_control.request_cleanup.assert_called_once_with('task','old')

    def test_application_cancel_requests_cleanup_but_status_never_does(self):
        from brain.application import BrainApplication
        app = BrainApplication.__new__(BrainApplication)
        app.start = lambda: None
        app.owner = SimpleNamespace(call=lambda fn,**kw:fn())
        app.service = Mock()
        app.service.runtime = None
        app.service.control.return_value = {'accepted':True,'state':'cancelled','task_id':'task'}
        app.service.status.return_value = {'task_id':'task'}
        app.learning = Mock(); app.learning.result.return_value = None
        app.body_control = Mock()
        app.body_control.recovery_status.return_value = {'blocked':True,'source':{'lease_id':'old'}}
        app.body_control.request_cleanup.return_value = {'blocked':True,'pending':True}
        with patch('shared.brain_journal.emit'):
            result = app.control('cancel',task_id='task')
        self.assertTrue(result['control_recovery']['pending'])
        app.body_control.request_cleanup.assert_called_once_with('task','old')
        app.status(); app.status()
        self.assertEqual(app.body_control.request_cleanup.call_count, 1)


if __name__ == '__main__': unittest.main()
