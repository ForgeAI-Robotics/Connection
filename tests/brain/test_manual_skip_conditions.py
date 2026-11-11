"""Human skip satisfies business conditions; original stop and ledger remain real."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from contracts.tasks import Rejected
from contracts.body_control import ControlError
from contracts.vla import manual_holding_assumed
from tests.brain import test_running_finish as finish_fixture


class ManualSkipTests(unittest.TestCase):
    setUp = finish_fixture.RunningFinishTests.setUp
    tearDown = finish_fixture.RunningFinishTests.tearDown
    service = finish_fixture.RunningFinishTests.service
    finish = finish_fixture.RunningFinishTests.finish

    def test_skip_open_then_grasp_delivers_without_vla_business_confirmation(self):
        service = self.service()
        open_command = service.runtime.record['open_command_id']
        service.control('skip', task_id='test-task', step_id='VLA_OPEN_FRIDGE')
        self.assertEqual(service.runtime.state, 'waiting_human')
        grasp_command = service.runtime.record['open_command_id']
        service.control('skip', task_id='test-task', step_id='VLA_GRASP_COLA')
        record = service.runtime.record
        self.assertEqual(record['state'], 'failed')  # Completed flow with human skips.
        self.assertTrue(record['flow_finished'])
        self.assertEqual(record['holding'], 'cola_can_1')
        self.assertEqual(len(self.nav.posts), 5)
        self.assertEqual([p['source'] for p in self.gate.puts], ['vla','navigation'])
        self.assertEqual(self.vla.finish_posts, 0)
        self.assertEqual(self.vla.commands[open_command]['state'], 'cancelled')
        self.assertEqual(self.vla.commands[grasp_command]['state'], 'cancelled')
        self.assertGreater(self.vla.current_queries, 0)  # Body publishing status, not O6 holding proof.
        for step in ('VLA_OPEN_FRIDGE','VLA_GRASP_COLA'):
            self.assertEqual(record['steps'][step]['status'], 'manual_skipped')
            self.assertNotEqual(record['steps'][step]['attempts'][-1].get('verdict'), 'PASS')
        nav = record['steps']['NAVIGATING_TO_RELAY1']['attempts'][-1]['request']
        self.assertTrue(manual_holding_assumed(nav))
        self.assertEqual(nav['operator_assumption']['command_id'], grasp_command)
        self.assertNotIn('operator_assumption', self.nav.posts[-1])  # Internal fact, original NAV wire body.

    def test_skip_holding_assumption_survives_runtime_reconstruction(self):
        service = self.service();self.finish(service)
        service._launch = lambda _: None
        service.control('skip', task_id='test-task', step_id='VLA_GRASP_COLA')
        self.assertEqual(len(self.nav.posts), 1)
        service.runtime = None
        restored = service.attach()
        service._drive(restored)
        self.assertEqual(len(self.nav.posts), 5)
        self.assertTrue(restored.record['flow_finished'])
        self.assertEqual(self.vla.finish_posts, 1)

    def test_skip_transport_leg_assumes_its_holding_condition_for_remaining_route(self):
        service = self.service();self.finish(service)
        service._launch = lambda _: None
        service.control('skip',task_id='test-task',step_id='VLA_GRASP_COLA')
        rt = service.runtime
        rt.record['holding'] = None
        rt.record['manual_skips'][-1].pop('conditions_satisfied')  # Historical skip has no assumption.
        rt._save('historical_skip_missing_holding')
        service.control('skip',task_id='test-task',step_id='NAVIGATING_TO_RELAY1')
        service._drive(rt)
        self.assertEqual(len(self.nav.posts),4)  # First transport leg was skipped, later three still run.
        self.assertEqual(rt.record['holding'],'cola_can_1')
        request=rt.record['steps']['NAVIGATING_TO_RELAY2']['attempts'][-1]['request']
        self.assertEqual(request['operator_assumption']['step_id'],'NAVIGATING_TO_RELAY1')
        self.assertTrue(manual_holding_assumed(request))

    def test_no_holding_assumption_when_current_body_port_is_unreleased(self):
        service = self.service();self.finish(service)
        original_cancel = self.vla.cancel
        def unknown(task_id, command_id):
            result = original_cancel(task_id, command_id)
            self.vla.commands[command_id]['result']['body_stopped'] = None
            self.vla.commands[command_id]['exit_state']['body_stopped'] = None
            return result
        self.vla.cancel = unknown
        self.vla.status_override['action_port_released'] = False
        result = service.control('skip', task_id='test-task', step_id='VLA_GRASP_COLA')
        self.assertFalse(result['accepted'])
        self.assertFalse(service.runtime.record.get('manual_skips'))
        self.assertIsNone(service.runtime.record['holding'])
        self.assertEqual(len(self.nav.posts), 1)

    def test_o6_failure_and_replaced_instances_do_not_block_manual_navigation(self):
        service = self.service();self.finish(service)
        rt = service.runtime;cid = rt.record['open_command_id']
        row = self.vla.commands[cid]
        self.vla.cancel('test-task',cid)
        row.update(state='failed',error={'code':'EXECUTION_STOP_UNCONFIRMED','message':'O6 receiver stopped'})
        row['result'].update(hands_stopped=None,hold_verified=None)
        row['exit_state'].update(hands_stopped=None,hold_verified=None,output_fenced=None,
                                runtime={'blockers':['O6_RECEIVER_NOT_RUNNING']})
        original = deepcopy(row)
        rt.observe_waiting_vla()
        self.assertEqual(rt.state,'recovery_required')
        self.vla.status_override.update(service_instance_id='restarted-vla',
            hold_verified=None,output_fenced=None,
            execution_facts={'blockers':['BODY_INSTANCE_CHANGED','O6_INSTANCE_CHANGED']})
        result = service.control('skip',task_id='test-task',step_id='VLA_GRASP_COLA')
        self.assertTrue(result['accepted'],result)
        self.assertEqual(len(self.nav.posts),5)
        self.assertEqual([p['source'] for p in self.gate.puts],['vla','navigation'])
        self.assertEqual(self.vla.commands[cid],original)  # The old failure is still true and preserved.
        self.assertEqual(self.vla.finish_posts,1)
        skipped = rt.record['manual_skips'][-1]
        self.assertFalse(skipped['stop_observation']['stopped'])
        self.assertTrue(skipped['body_handoff_observation']['confirmed'])
        self.assertEqual(skipped['body_handoff_observation']['scope'],'current_body_publishing_path')

    def test_unknown_or_active_current_body_path_cannot_be_skipped_into_navigation(self):
        from brain.adapters.http_client import HttpContractError
        service = self.service();self.finish(service)
        self.vla.cancel('test-task',service.runtime.record['open_command_id'])
        for override in ({'active_command_id':'another-command'}, {'policy_running':True},
                         {'policy_running':None}, {'action_port_released':None}, {'service_instance_id':''}):
            with self.subTest(override=override):
                self.vla.status_override.clear();self.vla.status_override.update(override)
                request={'task_id':'test-task','step_id':'NEXT_NAV','kind':'to_nav',
                         'source_command_id':'grasp-1','source_request':{'skill':'grasp_cola'},'manual_skips':['VLA_GRASP_COLA']}
                # Use a stopped, bound original receipt; only current body facts vary.
                query = self.port.query
                self.port.query = lambda *_: dict(command_id='grasp-1',terminal='failed',
                    evidence={'identity_ok':True,'time_ok':True})
                try:
                    self.assertFalse(self.port.handoff_with_context(request)['confirmed'])
                finally:
                    self.port.query = query
        with patch.object(self.vla,'control_status',side_effect=HttpContractError('status unavailable')), \
             patch.object(self.port,'query',return_value=dict(terminal='failed',evidence={'identity_ok':True,'time_ok':True})):
            self.assertEqual(self.port.handoff_with_context(request)['reason'],'current_body_status_unavailable')

    def test_skip_waits_for_original_async_cancel_then_delivers_without_second_click(self):
        service = self.service();self.finish(service)
        original_cancel, original_wait = self.vla.cancel, self.vla.wait_task
        pending = set();waits = []
        def cancel(task_id, command_id):
            original_cancel(task_id, command_id)
            row = self.vla.commands[command_id]
            row.update(state='stopping_policy', completed_at=None)
            pending.add(command_id)
            return self.vla.task(command_id)
        def wait(command_id, **options):
            if command_id in pending:
                waits.append((command_id,options))
                pending.remove(command_id)
                self.vla.commands[command_id].update(state='cancelled',completed_at=datetime.now(timezone.utc).isoformat())
                return self.vla.task(command_id)
            return original_wait(command_id,**options)
        self.vla.cancel, self.vla.wait_task = cancel, wait
        cid = service.runtime.record['open_command_id']
        result=service.control('skip',task_id='test-task',step_id='VLA_GRASP_COLA')
        self.assertTrue(result['accepted'],result)
        self.assertEqual([w[0] for w in waits],[cid])
        self.assertLessEqual(waits[0][1]['timeout_sec'],30)
        self.assertEqual(len(self.nav.posts),5)
        self.assertEqual(self.vla.finish_posts,1)

    def test_stale_command_skip_is_rejected_before_stopping_anything(self):
        service = self.service();before = deepcopy(service.runtime.record)
        with self.assertRaises(Rejected):
            service.control('skip',task_id='test-task',step_id='VLA_OPEN_FRIDGE',expected_command_id='old-command')
        self.assertEqual(service.runtime.record, before)

    def test_cancel_during_stop_wait_discards_human_assumption_and_next_navigation(self):
        service = self.service();self.finish(service)
        original_cancel = self.vla.cancel
        def cancel(task_id, command_id):
            original_cancel(task_id, command_id)
            self.vla.commands[command_id].update(state='stopping_policy',completed_at=None)
            return self.vla.task(command_id)
        def wait(command_id, request):
            service.runtime.request_cancel()
            return self.port.query(command_id,request)
        self.vla.cancel = cancel
        self.port.wait_completion = wait
        self.assertFalse(service.control('skip',task_id='test-task',step_id='VLA_GRASP_COLA')['accepted'])
        self.assertEqual(service.runtime.state,'cancelled')
        self.assertFalse(service.runtime.record.get('manual_skips'))
        self.assertIsNone(service.runtime.record['holding'])
        self.assertEqual(len(self.nav.posts),1)

    def test_invalid_or_foreign_holding_assumptions_do_not_bypass_current_vla_check(self):
        service = self.service();self.finish(service)
        request = dict(skill='navigate',body={'task_id':'test-task'},required_holding='cola_can_1')
        valid = dict(source='human_skip',task_id='test-task',step_id='VLA_GRASP_COLA',holding='cola_can_1',at='now')
        for changes in ({'task_id':'other'}, {'holding':'other'}, {'source':'model'}, {'step_id':''}, {'at':''}):
            candidate = dict(request,operator_assumption=dict(valid,**changes))
            self.assertFalse(manual_holding_assumed(candidate))
            with self.assertRaises(ControlError):
                self.port._current_holding(candidate)

    def test_skip_api_forwards_expected_command_identity(self):
        from brain.api.app import create_app
        facade = Mock();facade.control.return_value={'accepted':True}
        client = create_app(Mock(),facade=facade).test_client()
        body = dict(task_id='task',step_id='VLA_GRASP_COLA',expected_command_id='grasp-1')
        self.assertEqual(client.post('/api/task_skip',json=body).status_code,200)
        facade.control.assert_called_once_with('skip',**body)
