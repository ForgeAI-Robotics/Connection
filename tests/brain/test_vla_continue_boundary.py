"""VLA retries consume lifecycle facts; the executor owns current O6 admission."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from brain.adapters.http_client import HttpContractError
from contracts.tasks import Rejected
from tests.brain import test_running_finish as fixture


class VlaContinueBoundaryTests(unittest.TestCase):
    setUp = fixture.RunningFinishTests.setUp
    tearDown = fixture.RunningFinishTests.tearDown
    service = fixture.RunningFinishTests.service
    finish = fixture.RunningFinishTests.finish

    def failed_grasp(self):
        service = self.service();self.finish(service)
        rt = service.runtime;cid = rt.record['open_command_id']
        self.vla.cancel('test-task',cid)
        raw = self.vla.commands[cid]
        raw.update(state='failed',error={'code':'EXECUTION_STOP_UNCONFIRMED','message':'O6 instance retired'})
        raw['result'].update(hands_stopped=None,hold_verified=None)
        raw['exit_state'].update(hands_stopped=None,hold_verified=None,output_fenced=None,
                                 runtime={'blockers':['O6_INSTANCE_CHANGED']})
        rt.observe_waiting_vla()
        self.assertEqual(rt.state,'recovery_required')
        return service,cid,deepcopy(raw)

    def test_continue_sends_new_grasp_despite_old_o6_exit_unknown(self):
        service,cid,old = self.failed_grasp()
        self.vla.status_override.update(hold_verified=None,output_fenced=None,
            execution_facts={'blockers':['BODY_INSTANCE_CHANGED','O6_INSTANCE_CHANGED']})
        reply = service.control('continue',task_id='test-task',step_id='VLA_GRASP_COLA',expected_command_id=cid)
        self.assertTrue(reply['accepted'],reply)
        self.assertTrue(reply['retry_prepared'])
        rt = service.runtime;attempts = rt.record['steps']['VLA_GRASP_COLA']['attempts']
        self.assertEqual(len(attempts),2)
        self.assertEqual(attempts[-1]['command_id'],cid+'-r1')
        self.assertEqual(self.vla.posts[-1]['operation'],'grasp_cola')
        self.assertEqual(self.vla.posts[-1]['command_id'],cid+'-r1')
        self.assertEqual(rt.state,'waiting_human')
        self.assertEqual(self.vla.commands[cid],old)
        self.assertFalse(attempts[0]['progress']['publisher_stopped'])
        self.assertFalse(attempts[0]['progress']['evidence']['resources_released'])
        self.assertEqual(attempts[0]['progress']['evidence']['downstream'],old)
        self.assertTrue(attempts[0]['retry_boundary_observation']['confirmed'])
        self.assertEqual(attempts[-1]['authorized_by'],'human_continue')
        self.assertEqual(len(self.nav.posts),1)

    def test_current_o6_and_model_admission_is_left_to_vla(self):
        service,cid,_ = self.failed_grasp()
        self.vla.status_override.update(service_ready=False,recovery_required=True,
            hold_verified=None,native_execution_readiness={'ready':False,'blockers':['O6_NOT_READY']})
        posts = []
        def refuse(body):
            posts.append(deepcopy(body))
            raise HttpContractError('O6 not ready',status_code=503,payload={'success':False,'error':{
                'code':'VLA_EXECUTION_NOT_READY','message':'executor rejected current O6',
                'details':{'task_id':body['task_id'],'command_id':body['command_id'],
                           'action_started':False,'action_state_uncertain':False}}})
        with patch.object(self.vla,'submit_task',side_effect=refuse):
            self.assertTrue(service.control('continue',task_id='test-task')['accepted'])
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0]['command_id'],cid+'-r1')
        self.assertEqual(service.runtime.state,'recovery_required')
        self.assertEqual(service.status()['failure_detail']['code'],'VLA_EXECUTION_NOT_READY')
        self.assertEqual(len(self.nav.posts),1)

    def test_busy_or_unknown_body_boundary_does_not_create_a_retry(self):
        service,cid,_ = self.failed_grasp();before = deepcopy(service.runtime.record)
        for changes in ({'active_command_id':'other'}, {'policy_running':True}, {'policy_running':None},
                        {'action_port_released':False}, {'action_port_released':None},
                        {'service_instance_id':''}, {'service_instance_id':True}):
            with self.subTest(changes=changes):
                self.vla.status_override.clear();self.vla.status_override.update(changes)
                reply = service.control('continue',task_id='test-task')
                self.assertFalse(reply['accepted'])
                self.assertEqual(service.runtime.record,before)
        self.vla.status_override.clear()
        with patch.object(self.vla,'control_status',side_effect=HttpContractError('unavailable')):
            self.assertFalse(service.control('continue',task_id='test-task')['accepted'])
        self.assertEqual(len(self.vla.posts),2)
        self.assertEqual(service.runtime.record['open_command_id'],cid)

    def test_cancel_during_body_query_rejects_late_retry(self):
        service,_,_ = self.failed_grasp();status = self.vla.control_status
        def cancel_then_return():
            service.runtime.request_cancel()
            return status()
        with patch.object(self.vla,'control_status',side_effect=cancel_then_return):
            self.assertFalse(service.control('continue',task_id='test-task')['accepted'])
        self.assertEqual(service.runtime.state,'cancelled')
        self.assertEqual(len(self.vla.posts),2)
        self.assertEqual(len(service.runtime.record['steps']['VLA_GRASP_COLA']['attempts']),1)

    def test_success_is_not_reexecuted_when_old_o6_confirmation_is_missing(self):
        service,cid,_ = self.failed_grasp()
        raw = self.vla.commands[cid];raw.update(state='succeeded');raw['result']['success'] = True
        self.assertFalse(service.control('continue',task_id='test-task')['accepted'])
        self.assertEqual(len(self.vla.posts),2)
        self.assertEqual(len(service.runtime.record['steps']['VLA_GRASP_COLA']['attempts']),1)

    def test_unqueryable_or_foreign_original_does_not_create_a_retry(self):
        service,cid,_ = self.failed_grasp();query = self.port.query
        original = query(cid,service.runtime._latest('VLA_GRASP_COLA')['request'])
        for changes in ({'command_id':'other'}, {'timed_out':True},
                        {'evidence':dict(original['evidence'],identity_ok=False)},
                        {'evidence':dict(original['evidence'],time_ok=False)}):
            with self.subTest(changes=changes), patch.object(self.port,'query',return_value=dict(original,**changes)):
                self.assertFalse(service.control('continue',task_id='test-task')['accepted'])
        with patch.object(self.port,'query',side_effect=HttpContractError('query timed out')):
            self.assertFalse(service.control('continue',task_id='test-task')['accepted'])
        self.assertEqual(len(self.vla.posts),2)

    def test_repeated_click_does_not_dispatch_another_retry(self):
        service,cid,_ = self.failed_grasp()
        self.assertTrue(service.control('continue',task_id='test-task')['accepted'])
        with self.assertRaises(Rejected):
            service.control('continue',task_id='test-task',step_id='VLA_GRASP_COLA',expected_command_id=cid)
        self.assertFalse(service.control('continue',task_id='test-task')['accepted'])
        self.assertEqual(len(self.vla.posts),3)
        self.assertEqual(len(service.runtime.record['steps']['VLA_GRASP_COLA']['attempts']),2)

    def test_paused_cancelled_grasp_can_retry_without_old_o6_receipt(self):
        service,cid,_ = self.failed_grasp()
        self.vla.commands[cid]['state']='cancelled'
        rt=service.runtime;rt.record.update(state='paused',control_request='pause',dispatch_closed=True)
        rt._save('test_paused_o6_failure')
        self.assertTrue(service.control('continue',task_id='test-task')['accepted'])
        self.assertEqual(self.vla.posts[-1]['command_id'],cid+'-r1')
