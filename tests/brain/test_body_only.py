"""Body-only permits keep hand nulls, actual body guards and old-hand tombstones."""
import unittest
from copy import deepcopy
from contracts.body_control import ControlError
from tests.brain.test_body_control import Fixture,nav_request


class BodyOnlyTests(Fixture,unittest.TestCase):
    def test_all_permits_explicitly_exclude_hands(self):
        self.prepare()
        for op in ('grant','allow_execute'):
            q=self.posts(op)[0][3]
            self.assertEqual(q['hand_mode'],'not_required');self.assertIn('hold_reference_id',q)
            self.assertIsNone(q['hold_reference_id']);self.assertFalse(q.get('required_holding'))
        self.assertIsNone(self.driver.native_facts(self.wire.status('read'))['o6_hold_verified'])
    def test_old_hand_pending_unknown_or_missing_still_blocks(self):
        for pending in (True,None,'false'):
            s=self.wire.status('old-hand');s['native']['old_hand_task_pending']=pending
            with self.subTest(pending=pending),self.assertRaises(ControlError):self.driver.native_facts(s)
    def test_old_hand_policy_and_invented_hand_success_are_rejected(self):
        for mutate in (lambda s:s.update(hand_policy='required'),
                       lambda s:s['native'].update(o6_hold_verified=True),
                       lambda s:s['native'].pop('o6_output_fenced')):
            s=self.wire.status('policy');mutate(s)
            with self.assertRaises(ControlError):self.driver.native_facts(s)
    def test_native_safety_stale_or_body_hold_failure_still_blocks(self):
        for mutate in (lambda s:s.update(observation_error='NATIVE_SAFETY_NOT_CLEAR'),
                       lambda s:s['native'].update(body_hold_verified=False),
                       lambda s:s['native'].update(observed_monotonic=0)):
            s=self.wire.status('safety');mutate(s)
            with self.assertRaises(ControlError):self.driver.native_facts(s)
    def test_body_exit_does_not_need_hand_success(self):
        self.prepare();result=self.authority.release_control('test-task','nav-1')
        self.assertTrue(result['exit_confirmed']);self.assertEqual(result['hold_scope'],'body')
        self.assertIsNone(result['current_gateway']['native']['o6_hold_verified'])
    def test_current_exit_can_confirm_without_rewriting_unknown_original(self):
        self.prepare()
        original=self.driver.exchange
        def unknown(wire,**kw):
            result=original(wire,**kw)
            if wire['service']=='gateway' and wire['payload'].get('operation')=='fence':
                raise ControlError('HANDOFF_UNCONFIRMED','original result unknown')
            return result
        self.driver.exchange=unknown
        result=self.authority.release_control('test-task','nav-1')
        self.assertTrue(result['exit_confirmed'])
        self.assertEqual(result['gateway']['original_outcome'],'unknown')
        ops=self.authority.store.read()['requests'].values()
        child=next(o['children']['gateway-fence'] for o in ops if 'gateway-fence' in o['children'])
        self.assertNotEqual(child['state'],'confirmed')
        self.assertIn('last_error',child)
    def test_incomplete_or_foreign_current_exit_is_not_release(self):
        self.prepare();row=self.row()
        self.wire.last=deepcopy(self.wire.permit);self.wire.permit=None;self.wire.isolated=True
        for mutate in (lambda s:s['exit_status'].update(input_session_retired=False),
                       lambda s:s['exit_status']['identity'].update(control_session_id='foreign'),
                       lambda s:s['native'].update(old_hand_task_pending=True)):
            s=self.wire.status('exit');mutate(s)
            with self.assertRaises(ControlError):self.driver.verify_exit(row,{},s)
    def test_wire_required_holding_rejected_even_on_hybrid_backend(self):
        request=nav_request();request['body']['required_holding']='cola_can_1';self.schedule(request)
        task=self.store.load_state();task['execution_backend']='reception_real_sim_manipulation';self.store.save_state(task)
        with self.assertRaises(ControlError):self.authority.register(request)
        self.assertFalse(self.posts('prepare'))

    def test_current_permission_allows_motion_but_upgrade_and_exit_still_require_hold(self):
        self.prepare();row=self.row()
        original=self.wire.status
        def moving(observation):
            s=original(observation);s['native']['body_hold_verified']=False;s['native']['body_stopped']=False
            return s
        self.wire.status=moving
        self.driver.verify_current_permit(row,moving('check'),'execute')
        self.assertEqual(len(self.posts('renew')),0)
        self.assertFalse(self.row().get('revoked',False))
        s=moving('admission')
        with self.assertRaises(ControlError):self.driver.verify_current_permit(row,s,'execute',require_hold=True)
        with self.assertRaises(ControlError):self.driver.native_facts(s,isolated=True)

    def test_prepare_observation_allows_blending_without_granting_execution(self):
        self.prepare();row=self.row();s=self.wire.status('blend')
        s['permit']['phase']='prepare';s['native']['body_hold_verified']=False
        s['native']['sample']['planner_transition_blending']=True
        self.driver.verify_current_permit(row,s,'prepare')
        with self.assertRaises(ControlError):self.driver.verify_current_permit(row,s,'prepare',require_hold=True)

    def test_observation_still_rejects_safety_override_stale_feedback_and_wrong_session(self):
        self.prepare();row=self.row()
        for change in (lambda s:s['native']['sample'].update(safety_override=True),
                       lambda s:s['native']['sample'].update(low_state_monotonic_ns=0),
                       lambda s:s['native']['sample'].update(policy_output_selected=False),
                       lambda s:s['native'].update(active_session='foreign')):
            s=self.wire.status('runtime-check');change(s)
            with self.assertRaises(ControlError):self.driver.verify_current_permit(row,s,'execute')

    def test_async_exit_is_observed_until_ready_without_repeating_fence(self):
        self.prepare();original=self.wire.status;reads=[]
        def delayed(observation):
            s=original(observation)
            if s.get('exit_status') and s['permit'] is None:
                reads.append(observation)
                if len(reads)<3:
                    s['exit_status']['exit_confirmed']=False
                    s['exit_status']['body_hold_verified']=False
            return s
        self.wire.status=delayed
        result=self.authority.release_control('test-task','nav-1')
        self.assertTrue(result['exit_confirmed']);self.assertGreaterEqual(len(reads),3)
        self.assertEqual(len(self.posts('fence')),1);self.assertEqual(len(self.posts('revoke')),1)

    def test_exit_observation_read_timeout_retries_get_without_effect_replay(self):
        from brain.adapters.http_client import HttpContractError
        self.prepare();row=self.row()
        self.wire.last=self.wire.permit;self.wire.permit=None;self.wire.isolated=True
        original=self.driver.status;reads=[]
        def flaky(service):
            reads.append(service)
            if len(reads)==1:raise HttpContractError('read operation timed out')
            return original(service)
        self.driver.status=flaky
        before=len([x for x in self.wire.calls if x[1]=='POST'])
        result=self.driver.wait_exit(row,{'original_outcome':'unknown'})
        self.assertTrue(result['exit_confirmed']);self.assertEqual(reads,['gateway','gateway'])
        self.assertEqual(before,len([x for x in self.wire.calls if x[1]=='POST']))
