"""Three explicit reception modes; synthetic peers, no physical IO."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

from brain.app import create_service
from brain.adapters.ports import target_identity
from brain.adapters.simulated_navigation import SimulatedNavigationAdapter
from brain.adapters.preflight import reception_preflight
from brain.packages.reception_skill import BASELINE_PATH
from brain.reasoning import TaskReasoner
from contracts.body_control import ControlError
from tests.brain.test_body_control import Fixture, nav_request
from tests.brain.test_vla_gateway import VlaWire
from tests.brain.test_native_vla import FakeVla, evidence

THIRD='开始接待（导航模拟＋操控真机）'


class SimNavigationTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config.update(brain={'reception_backend':'real','capture_timeline':False,
                                 'reasoning':{'recovery_advice':False}},reflection={'enabled':False})
        self.config['reception_real']['skill_file']=str(BASELINE_PATH.with_name('reception_single_can_v2.yaml'))
        self.wire=VlaWire()
        original=self.wire.status
        def status(observation):
            value=original(observation);value['native']['safety_override']=False;return value
        self.wire.status=status
        self.driver.clients={k:self.wire.client(k) for k in ('vla','gateway')}
        class Vla(FakeVla):
            def submit_task(inner,body):
                result=super().submit_task(body)
                inner.commands[body['command_id']].update(**{k:body['control'][k] for k in
                    ('control_session_id','control_authorization','command_seq')})
                return result
            def finish(inner,*args):
                value=super().finish(*args)
                inner.commands[value['command_id']]['result']['publisher_stopped']=True
                return inner.task(value['command_id'])
        self.vla=Vla();self.port=SimulatedNavigationAdapter(self.vla,self.authority)
        self.service=create_service(self.config,model=lambda _:self.fail('Exact button must not call model'),
                                    port_factory=lambda backend:self.port)
        self.service.body_control=self.authority
        env=patch.dict(os.environ,{'FQ_EXECUTION_STATE':str(self.root/'execution.json'),
            'FQ_EXECUTION_LOCK':str(self.root/'execution.lock'),'FQ_EXECUTION_BLOCK':str(self.root/'execution.blocked')})
        env.start();self.addCleanup(env.stop)

    def publish(self,confirmed=True):
        self.service.publish(THIRD,'test-task',options={'entry':'web','reflect':False,'worksite_confirmed':confirmed})
        return self.service.runtime

    def test_three_buttons_bind_distinct_explicit_modes(self):
        reasoner=TaskReasoner(lambda _:self.fail('model'),self.config)
        cases=[('开始接待（导航真机＋操控真机）','real'),
               ('开始接待（导航真机＋操控模拟）','real_sim_manipulation'),(THIRD,'real_sim_navigation')]
        for text,variant in cases:
            chosen=reasoner.select(text,{})
            self.assertEqual(chosen['workflow_variant'],variant)

    def test_sim_navigation_does_not_contact_nav_in_preflight(self):
        value=reception_preflight(self.config,THIRD,client_factory=lambda *a,**k:self.fail('NAV contacted'))
        self.assertTrue(value['ready']);self.assertFalse(value['required'])
        self.assertEqual(value['execution_backend'],'reception_sim_navigation_real')

    def test_no_operator_confirmation_does_not_prepare_real_vla(self):
        runtime=self.publish(False)
        self.assertEqual(runtime.state,'recovery_required')
        self.assertFalse(self.wire.calls);self.assertFalse(self.vla.posts)

    def test_complete_mixed_flow_uses_real_vla_and_never_real_nav(self):
        runtime=self.publish()
        self.assertEqual(runtime.state,'waiting_human',runtime.public_status())
        self.assertEqual(runtime.record['execution_backend'],'reception_sim_navigation_real')
        self.assertEqual(runtime.record['execution_target']['dream'],'local:simulated-navigation/v1')
        for operation in ('open_fridge','grasp_cola'):
            command=runtime.record['open_command_id']
            self.assertEqual(self.vla.commands[command]['operation'],operation)
            self.service.vla_effect('test-task',command,'pause')
            self.service.vla_effect('test-task',command,'finish',evidence=evidence(operation),authorization='Bearer test-only')
            runtime.continue_current();runtime.drive()
        self.assertEqual(runtime.state,'succeeded',runtime.public_status())
        self.assertEqual(len(self.vla.posts),2)
        self.assertEqual({c[0] for c in self.wire.calls},{'vla','gateway'})
        self.assertEqual(len(self.posts('grant')),2)
        for body in self.vla.posts:
            self.assertNotIn('navigation_proof',body)
            self.assertEqual(body['worksite_evidence']['kind'],'operator_ready')
            self.assertFalse(body.get('simulated',False))
        rows=runtime.public_status()['subtask_list']
        navs=[row for row in rows if row['step_id'].startswith(('NAVIGATING_','LATERAL_'))]
        self.assertTrue(navs);self.assertTrue(all(row['simulated'] for row in navs))
        self.assertFalse(any(row['simulated'] for row in rows if row['step_id'].startswith('VLA_')))

    def test_restore_navigation_only_from_original_runtime_receipt(self):
        request=nav_request();self.port.submit('nav-1',request)
        viewed=self.port.query('nav-1',request)
        new=SimulatedNavigationAdapter(self.vla,self.authority)
        self.assertIsNone(new.query('nav-1',request)['terminal'])
        record=dict(task_id='test-task',execution_backend='reception_sim_navigation_real',steps={
            'nav':{'attempts':[dict(command_id='nav-1',request=request,progress={'evidence':{'downstream':viewed['raw']}})]}})
        new.restore_receipts(record)
        self.assertEqual(new.query('nav-1',request)['terminal'],'succeeded')
        self.assertFalse(new.query('nav-1',request)['evidence']['physical_execution'])
        self.assertFalse(self.wire.calls)

    def test_confirmation_expiry_and_controller_change_never_query_nav(self):
        runtime=self.publish()
        command=runtime.record['open_command_id'];row=self.authority.command_row('test-task',command)
        for change in ({'observed_at':'2000-01-01T00:00:00+00:00'},{'controller_instance_id':'different'}):
            candidate=deepcopy(row);candidate['worksite_confirmation'].update(change)
            with self.assertRaises(ControlError):self.driver.vla_business(candidate,'new-prepare')
        self.assertFalse(any(c[0]=='nav' for c in self.wire.calls))

    def test_new_backend_is_bound_to_real_vla_and_local_navigation(self):
        target=target_identity(self.config,'reception_sim_navigation_real')
        self.assertEqual(target['dream'],'local:simulated-navigation/v1')
        self.assertEqual(target['vla'],self.config['reception_real']['vla_base_url'])

    def test_all_three_ports_keep_selected_real_and_simulated_sides(self):
        from brain.adapters.ports import build_port
        from brain.adapters.body_control import ControlledBodyAdapter
        from brain.adapters.simulated_manipulation import HybridReceptionAdapter,SimulatedManipulationClient
        from brain.adapters.vla_client import VlaClient
        first=build_port(self.config,'reception_real',body_control=self.authority).port
        second=build_port(self.config,'reception_real_sim_manipulation',body_control=self.authority).port
        third=build_port(self.config,'reception_sim_navigation_real',body_control=self.authority).port
        self.assertIs(type(first),ControlledBodyAdapter);self.assertIsInstance(first.vla,VlaClient)
        self.assertIsInstance(second,HybridReceptionAdapter);self.assertIsInstance(second.vla,SimulatedManipulationClient)
        self.assertIsInstance(third,SimulatedNavigationAdapter);self.assertIsNone(third.dream)
        self.assertIsInstance(third.vla,VlaClient)
        self.assertFalse(self.wire.calls)

    def test_cancel_real_manipulation_keeps_real_exit_and_no_nav_io(self):
        runtime=self.publish();command=runtime.record['open_command_id']
        runtime.request_cancel()
        self.authority.cancel_task('test-task')
        for future in self.authority._cleanup_futures.values():future.result(2)
        self.assertTrue(self.authority.released('test-task',command))
        self.assertTrue(self.posts('yield'));self.assertTrue(self.posts('revoke'))
        self.assertFalse(any(c[0]=='nav' for c in self.wire.calls))

    def test_browser_forwards_explicit_worksite_confirmation(self):
        from unittest.mock import Mock
        from entries.web.app import create_app
        client=Mock();client.request.return_value=Mock(content=b'{"accepted":true}',status_code=200,
                                                      headers={'content-type':'application/json'})
        response=create_app(client=client).test_client().post('/publish_task',json={
            'task':THIRD,'task_id':'test-task','worksite_confirmed':True})
        self.assertEqual(response.status_code,200)
        self.assertIs(client.request.call_args.kwargs['json']['worksite_confirmed'],True)
        html=create_app(client=client).test_client().get('/').get_data(as_text=True)
        for text in ('导航真机＋操控真机','导航真机＋操控模拟','导航模拟＋操控真机'):
            self.assertIn('开始接待（'+text+'）',html)
        self.assertNotIn('开始接待（模拟导航＋操控真机）',html)
