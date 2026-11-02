"""Observe VLA terminal results while human confirmation is still pending."""
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch

from brain.application import BrainApplication
from contracts.tasks import Rejected
from tests.brain import test_running_finish as finish_fixture
from tests.brain.test_native_vla import now, evidence
from tests.brain.test_source_gate_vla import DirectVla


class VlaTerminalSyncTests(unittest.TestCase):
    setUp = finish_fixture.RunningFinishTests.setUp
    tearDown = finish_fixture.RunningFinishTests.tearDown
    service = finish_fixture.RunningFinishTests.service

    def test_normal_wait_skips_advice_but_query_errors_and_failure_do_not(self):
        svc = self.service(); rt = svc.runtime
        svc.reasoner = object()
        self.assertIsNone(svc.advice_context(rt))
        rt.record.pop('recovery_advice', None)
        rt.record['blocked_reason'] = 'vla_status_unconfirmed'
        rt.record['vla_confirmation']['observation_error'] = 'query timeout'
        with patch.object(rt, 'snapshot', return_value=deepcopy(rt.record)):
            self.assertIsNotNone(svc.advice_context(rt))
        self.terminal(svc)
        self.assertTrue(rt.observe_waiting_vla())
        self.assertEqual(rt.state, 'recovery_required')
        self.assertIsNotNone(svc.advice_context(rt))

    def terminal(self, service, state='failed'):
        cid=service.runtime.record['open_command_id']
        row=self.vla.commands[cid]
        row.update(state=state,completed_at=now(),result={'success':False,'policy_stopped':True},
                   error={'code':'TASK_EXECUTOR_FAILED','message':'O6 execution stopped: ENCODER_TIMEOUT'})
        self.vla._stop(row)
        return cid

    def test_failure_and_cancel_reach_runtime_without_continue_or_finish(self):
        service=self.service();rt=service.runtime;cid=self.terminal(service)
        nav=deepcopy(rt.record['steps']['NAVIGATING_TO_FRIDGE'])
        self.assertTrue(rt.observe_waiting_vla())
        self.assertEqual(rt.state,'recovery_required')
        self.assertEqual(rt.public_status()['failure_detail']['code'],'TASK_EXECUTOR_FAILED')
        self.assertEqual(rt.record['vla_confirmation']['state'],'failed')
        self.assertFalse(rt.record['command_unknown'])
        self.assertTrue(rt.record['stopped_confirmed']);self.assertTrue(rt.record['resources_cleared'])
        self.assertEqual(rt.record['steps']['NAVIGATING_TO_FRIDGE'],nav)
        self.assertEqual(rt.record['open_command_id'],cid)
        self.assertEqual(rt.public_status()['completed'],3)
        self.assertFalse(rt.observe_waiting_vla())
        self.assertEqual(len(self.vla.posts),1);self.assertEqual(self.vla.finish_posts,0)
        self.assertEqual(self.vla.cancels,[])

    def test_cancelled_without_stop_evidence_is_known_but_not_released(self):
        svc=self.service();cid=self.terminal(svc,'cancelled')
        row=self.vla.commands[cid];row['exit_state']['body_stopped']=False;row['result']['body_stopped']=False
        svc.runtime.observe_waiting_vla()
        self.assertEqual(svc.runtime.state,'recovery_required')
        self.assertFalse(svc.runtime.record['command_unknown'])
        self.assertFalse(svc.runtime.record['stopped_confirmed'])
        self.assertFalse(svc.runtime.record['resources_cleared'])

    def test_unchanged_running_does_not_write_a_new_revision(self):
        svc=self.service();rt=svc.runtime;before=rt.snapshot()
        self.assertFalse(rt.observe_waiting_vla())
        self.assertEqual(rt.snapshot(),before)
        self.assertEqual(self.vla.finish_posts,0)

    def test_network_error_recovers_read_only_and_does_not_repeat_identical_error(self):
        svc=self.service();rt=svc.runtime
        with patch.object(self.port,'query',side_effect=TimeoutError('query timeout')):
            self.assertTrue(rt.observe_waiting_vla())
            revision=rt.record['revision']
            self.assertFalse(rt.observe_waiting_vla())
            self.assertEqual(rt.record['revision'],revision)
        self.assertEqual(rt.state,'waiting_human')
        self.assertTrue(rt.record['command_unknown'])
        self.assertEqual(rt.record['vla_confirmation']['observation_error'],'query timeout')
        self.assertTrue(rt.observe_waiting_vla())
        self.assertNotIn('observation_error',rt.record['vla_confirmation'])
        self.assertEqual(rt.record['blocked_reason'],'vla_effect_confirmation')
        self.assertEqual(len(self.vla.posts),1)

    def test_foreign_or_undated_terminal_does_not_finish_current_command(self):
        svc=self.service();rt=svc.runtime;cid=self.terminal(svc)
        for change in ({'command_id':'foreign'},{'completed_at':None}):
            original=deepcopy(self.vla.commands[cid]);self.vla.commands[cid].update(change)
            rt.observe_waiting_vla()
            self.assertEqual(rt.state,'waiting_human')
            self.assertTrue(rt.record['command_unknown'])
            self.assertFalse(rt.record['stopped_confirmed'])
            self.vla.commands[cid]=original
        rt.observe_waiting_vla()
        self.assertEqual(rt.state,'recovery_required')

    def test_observed_success_never_advances_without_human_continue(self):
        svc=self.service();rt=svc.runtime;cid=rt.record['open_command_id']
        # A separate authorized verifier completed the downstream task.
        self.vla.pause('test-task',cid)
        DirectVla.finish(self.vla,'test-task',cid,evidence(),'Bearer test-only')
        self.assertTrue(rt.observe_waiting_vla())
        self.assertEqual(rt.state,'waiting_human');self.assertEqual(rt.public_status()['completed'],3)
        self.assertFalse(rt.record['command_unknown'])
        self.assertEqual(len(self.vla.posts),1)
        self.assertNotEqual(rt._latest('VLA_OPEN_FRIDGE')['verdict'],'PASS')
        self.assertFalse(rt.observe_waiting_vla())

    def test_manual_query_of_failure_does_not_disable_automatic_state_sync(self):
        svc=self.service();rt=svc.runtime;cid=self.terminal(svc)
        rt.vla_effect('test-task',cid,'query')
        self.assertEqual(rt.state,'waiting_human')
        self.assertTrue(rt.observe_waiting_vla())
        self.assertEqual(rt.state,'recovery_required')

    def test_late_observation_cannot_override_cancel(self):
        svc=self.service();rt=svc.runtime;self.terminal(svc)
        query=self.port.query
        def late(*args):
            value=query(*args);rt.request_cancel();return value
        with patch.object(self.port,'query',side_effect=late):
            self.assertFalse(rt.observe_waiting_vla())
        self.assertEqual(rt.state,'cancelled')
        self.assertEqual(self.vla.finish_posts,0)

    def test_late_observation_cannot_override_new_task(self):
        svc=self.service();rt=svc.runtime;self.terminal(svc)
        query=self.port.query
        def replaced(*args):
            value=query(*args)
            rt.request_cancel()
            rt.open_task('new-task',task_desc='new task')
            return value
        with patch.object(self.port,'query',side_effect=replaced):
            self.assertFalse(rt.observe_waiting_vla())
        self.assertEqual(rt.record['task_id'],'new-task')
        self.assertEqual(rt.state,'running')
        self.assertEqual(self.vla.finish_posts,0)

    def test_human_finish_during_observation_wins_over_late_result(self):
        svc=self.service();svc._launch=lambda _:None
        rt=svc.runtime;cid=rt.record['open_command_id'];query=self.port.query
        def confirmed(*args):
            value=query(*args)
            svc.vla_effect('test-task',cid,'finish',evidence=evidence(),
                           authorization='Bearer test-only',auto_advance=True)
            return value
        with patch.object(self.port,'query',side_effect=confirmed):
            self.assertFalse(rt.observe_waiting_vla())
        self.assertTrue(rt.pending_vla_completion())
        self.assertEqual(self.vla.finish_posts,1)

    def test_pending_finish_keeps_existing_completion_worker_authority(self):
        svc=self.service();svc._launch=lambda _:None
        cid=svc.runtime.record['open_command_id']
        svc.vla_effect('test-task',cid,'finish',evidence=evidence(),authorization='Bearer test-only',auto_advance=True)
        with patch.object(self.port,'query',side_effect=AssertionError('poll must defer to finish waiter')):
            self.assertFalse(svc.runtime.observe_waiting_vla())
        self.assertEqual(self.vla.finish_posts,1)

    def test_application_restart_observes_waiting_task_without_an_open_browser(self):
        svc=self.service();self.terminal(svc)
        # Rebuild from the saved waiting_human snapshot; the fake peers stay alive.
        self.control.close()
        from brain.app import create_service
        from tests.brain.test_source_gate import Gate
        from brain.adapters.source_gate import SourceGateBodyAdapter
        restored=create_service(self.config,model=lambda _:self.fail('No model'),
            port_factory=lambda _:SourceGateBodyAdapter(self.nav,self.vla,restored.body_control))
        with patch('brain.kernel.source_gate.SourceGateClient',return_value=Gate()):
            app=BrainApplication(restored).start()
            try:
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    if app.status()['state']=='recovery_required':break
                    time.sleep(.02)
                self.assertEqual(app.status()['state'],'recovery_required')
                self.assertEqual(self.vla.finish_posts,0);self.assertEqual(len(self.vla.posts),1)
                self.assertEqual(self.vla.cancels,[])
            finally:app.close(3)

    def test_blocked_background_query_does_not_block_cancellation(self):
        self.service();self.control.close()
        from brain.app import create_service
        from brain.adapters.source_gate import SourceGateBodyAdapter
        from tests.brain.test_source_gate import Gate
        restored=create_service(self.config,model=lambda _:self.fail('No model'),
            port_factory=lambda _:SourceGateBodyAdapter(self.nav,self.vla,restored.body_control))
        entered,release=threading.Event(),threading.Event()
        original=SourceGateBodyAdapter.query
        def slow(port,*args):
            value=original(port,*args);entered.set();release.wait(3);return value
        with patch('brain.kernel.source_gate.SourceGateClient',return_value=Gate()), \
             patch.object(SourceGateBodyAdapter,'query',autospec=True,side_effect=slow):
            app=BrainApplication(restored).start()
            try:
                self.assertTrue(entered.wait(2))
                start=time.monotonic();answer=app.control('cancel',task_id='test-task')
                self.assertTrue(answer['accepted'])
                self.assertLess(time.monotonic()-start,1.)
                release.set()
                self.assertEqual(app.status()['state'],'cancelled')
            finally:
                release.set();app.close(3)
