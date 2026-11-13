"""Single-button close test reuses the original close executor and verification."""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from brain.app import create_service
from brain.adapters.source_gate import SourceGateBodyAdapter
from brain.adapters.preflight import reception_preflight
from brain.adapters.door_verification import CLOSE_SCHEMA, CLOSE_CONTRACT
from brain.packages.fridge_test import SOP
from brain.packages.reception_skill import BASELINE_PATH
from contracts.intent import classify_entry
from tests.brain import test_source_gate as fixture
from tests.brain.test_source_gate_vla import DirectVla
from tests.brain.test_native_vla import now


class TestVla(DirectVla):
    def submit_task(self, body):
        assert 'navigation_proof' not in body
        value=super().submit_task(body)
        self.commands[body['command_id']]['execution_ready_at']=now()
        return value

    def finish(self, task_id, command_id, evidence, authorization):
        assert authorization=='Bearer test-only'
        self.finish_calls+=1
        row=self.commands[command_id]
        row.update(state='succeeded',completed_at=now(),finish_requested=True,
            result=dict(success=True,object_id=row['object_id'],policy_stopped=True,
                evidence=deepcopy(evidence),effect_verified_by=evidence['source'],
                evidence_level=evidence['source'],autonomous_effect_verification=False,
                door_closed=True,door_open=False,holding_known=False),
            completion=dict(status='completed',requested_from='running',evidence=deepcopy(evidence),
                            evidence_window_start=row['execution_ready_at']))
        self._stop(row)
        return self.task(command_id)


class StandaloneTests(unittest.TestCase):
    setUp=fixture.SourceGateTests.setUp
    tearDown=fixture.SourceGateTests.tearDown

    def build(self):
        self.config.update(brain={'reception_backend':'real','capture_timeline':False,
            'reasoning':{'recovery_advice':False}},reflection={'enabled':False})
        self.config['reception_real'].update(
            skill_file=str(BASELINE_PATH.with_name('reception_single_can_v2.yaml')),
            door_verification=dict(enabled=True,mode='auto'))
        if not hasattr(self,'vla'):self.vla=TestVla()
        self.port=SourceGateBodyAdapter(self.nav,self.vla,self.control)
        svc=create_service(self.config,model=lambda _:self.fail('No model for explicit test'),port_factory=lambda _:self.port)
        svc.body_control=self.control
        scope=patch.dict(os.environ,{k:str(self.root/name) for k,name in (
            ('FQ_EXECUTION_STATE','execution.json'),('FQ_EXECUTION_LOCK','execution.lock'),('FQ_EXECUTION_BLOCK','execution.blocked'))})
        scope.start();self.addCleanup(scope.stop)
        return svc

    def publish(self, confirmed=True):
        svc=self.build()
        svc.publish(SOP['triggers'][0],'standalone-test',options={'worksite_confirmed':confirmed,'reflect':False})
        return svc

    def proposal(self,svc):
        context=svc.runtime.door_observation_context()
        self.assertEqual(context['operation'],'close_fridge_test')
        evidence=dict(source='visual_verifier',reference='/offline/close',observed_at=now(),
            visual_contract=CLOSE_CONTRACT,door_closed=True,door_open=False,arms_returned=True,
            arm_return=dict(schema='fq/vla-arm-return/v1',valid=True,event_id='close-return',return_dwell_s=.8),
            binding=dict(task_id=context['task_id'],command_id=context['command_id']))
        return context,dict(schema=CLOSE_SCHEMA,status='pass',vlm_calls=1,evidence=evidence)

    def test_one_close_command_without_navigation_grasp_or_delivery(self):
        svc=self.publish();rt=svc.runtime
        self.assertEqual(rt.state,'waiting_human',svc.status())
        self.assertEqual([p['operation'] for p in self.vla.posts],['close_fridge_test'])
        self.assertEqual(self.nav.posts,[])
        context,result=self.proposal(svc)
        rt.apply_door_observation(context,result,authorization='Bearer test-only');svc._drive(rt)
        self.assertEqual(rt.state,'succeeded',svc.status());self.assertIsNone(rt.record['holding'])
        self.assertEqual(self.nav.posts,[])
        self.assertEqual([x['source'] for x in self.gate.puts],['vla'])
        self.assertEqual(rt.record['selection']['sop']['id'],'fridge.close_test')
        self.assertEqual([s['kind'] for s in rt.record['phase_specs']],['local','local','close_fridge_test','verify'])
        self.assertFalse(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_calls,1)

    def test_missing_site_confirmation_never_dispatches(self):
        svc=self.publish(False)
        self.assertEqual(svc.runtime.state,'recovery_required')
        self.assertEqual(self.vla.posts,[]);self.assertEqual(self.nav.posts,[])

    def test_wrong_backend_does_not_fall_back_to_simulation(self):
        svc=self.build();svc.config['reception_real']['body_control']['mode']='unified'
        svc.publish(SOP['triggers'][0],'standalone-test',options={'worksite_confirmed':True,'reflect':False})
        self.assertEqual(svc.runtime.state,'recovery_required')
        self.assertEqual(self.vla.posts,[]);self.assertEqual(self.nav.posts,[])

    def test_cancel_rejects_late_close_vision(self):
        svc=self.publish();rt=svc.runtime;context,result=self.proposal(svc);rt.request_cancel()
        self.assertFalse(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(rt.state,'cancelled');self.assertEqual(self.vla.finish_calls,0)

    def test_test_operation_cannot_be_injected_into_normal_reception(self):
        from contracts.planning import Plan
        from contracts.steps import StepSpec
        svc=self.build()
        svc.plan_call=lambda *args:Plan((StepSpec('injected','close_fridge_test'),),'injected')
        svc.publish('开始接待','normal-task',options={'reflect':False})
        self.assertEqual(svc.runtime.state,'recovery_required')
        self.assertEqual(self.vla.posts,[]);self.assertEqual(self.nav.posts,[])

    def test_entry_and_preflight_are_explicit_without_nav_query(self):
        self.build()
        report=classify_entry(SOP['triggers'][0]);self.assertFalse(report['needs_llm_route'])
        self.assertEqual(report['risk'],'motion')
        report=reception_preflight(self.config,SOP['triggers'][0],client_factory=lambda *a,**k:self.fail('No NAV preflight'))
        self.assertTrue(report['ready'])

    def test_web_button_uses_existing_publish_route_and_site_confirmation(self):
        from entries.web.app import create_app
        client=Mock();client.request.return_value=Mock(content=b'{"accepted":true}',status_code=200,
            headers={'content-type':'application/json'})
        browser=create_app(client=client).test_client()
        page=browser.get('/').get_data(as_text=True)
        self.assertIn('id="task-fridge-test"',page)
        self.assertIn("startReception('单独测试关冰箱')",page)
        self.assertNotIn('renderFridgeAlignmentTest',page)
        response=browser.post('/publish_task',json=dict(task='单独测试关冰箱',worksite_confirmed=True))
        self.assertEqual(response.status_code,200)
        forwarded=client.request.call_args.kwargs['json']
        self.assertTrue(forwarded['worksite_confirmed']);self.assertEqual(forwarded['task'],'单独测试关冰箱')
        self.assertNotIn('navigation_proof',forwarded)
