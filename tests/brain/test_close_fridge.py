"""Generated close is a task-bound step after verified grasp, before delivery."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from brain.adapters.door_verification import (
    DoorObserver, SCHEMA, VISUAL_CONTRACT, GRASP_SCHEMA, GRASP_CONTRACT,
    CLOSE_SCHEMA, CLOSE_CONTRACT, semantic_pass,
)
from brain.packages.reception_skill import BASELINE_PATH, compile_skill, load_skill, from_snapshot
from contracts.tasks import Rejected
from contracts.vla import validate_evidence
from tests.brain.test_door_verification import Camera
from tests.brain import test_running_finish as finish_fixture


def proof(context):
    operation = context['operation']
    schema, contract = {'open_fridge': (SCHEMA, VISUAL_CONTRACT),
                        'grasp_cola': (GRASP_SCHEMA, GRASP_CONTRACT),
                        'close_fridge': (CLOSE_SCHEMA, CLOSE_CONTRACT)}[operation]
    evidence = dict(source='visual_verifier', reference='/offline/'+operation+'.json',
        observed_at=datetime.now(timezone.utc).isoformat(), visual_contract=contract,
        binding=dict(task_id=context['task_id'], command_id=context['command_id']),
        arms_returned=True, arm_return=dict(schema='fq/vla-arm-return/v1', valid=True,
                                            event_id='offline-'+operation, return_dwell_s=.8))
    if operation == 'grasp_cola':
        evidence.update(object_grasped=True, left_grasped=True, right_grasped=True, holding='cola_can_1')
    else:
        evidence.update(door_closed=operation == 'close_fridge', door_open=operation == 'open_fridge')
    return dict(schema=schema, status='pass', vlm_calls=1, evidence=evidence)


class CloseReceptionTests(unittest.TestCase):
    setUp = finish_fixture.RunningFinishTests.setUp
    tearDown = finish_fixture.RunningFinishTests.tearDown

    def service(self):
        self.config['reception_real']['door_verification'] = dict(enabled=True, mode='auto')
        return finish_fixture.RunningFinishTests.service(self, with_close=True)

    def complete(self, service):
        rt = service.runtime
        context = rt.door_observation_context()
        self.assertIsNotNone(context, rt.public_status())
        proposal = proof(context)
        self.assertTrue(rt.apply_door_observation(context, proposal, authorization='Bearer test-only'))
        service._drive(rt)
        return context, proposal

    def at_close(self):
        svc = self.service()
        self.complete(svc); self.complete(svc)
        self.assertEqual(svc.runtime.record['phase'], 'VLA_CLOSE_FRIDGE')
        self.assertEqual(len(self.nav.posts), 1)
        return svc

    def test_verified_grasp_then_close_then_delivery_with_same_task_holding(self):
        svc = self.at_close()
        self.assertEqual(self.vla.posts[-1]['operation'], 'close_fridge')
        self.assertTrue(svc.status()['vla_confirmation']['auto_verification'])
        old_context, result = self.complete(svc)
        self.assertEqual(svc.runtime.state, 'succeeded', svc.status())
        self.assertEqual(svc.runtime.record['holding'], 'cola_can_1')
        self.assertEqual(len(self.nav.posts), 5)
        self.assertEqual(self.vla.finish_posts, 3)
        self.assertEqual([p['source'] for p in self.gate.puts], ['vla', 'navigation'])
        self.assertEqual(len({p['command_id'] for p in self.vla.posts}), 3)
        self.assertFalse(svc.runtime.apply_door_observation(old_context, result, authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_posts, 3)

    def test_failed_close_cleanup_does_not_dispatch_delivery_or_erase_holding(self):
        svc = self.at_close(); self.vla.completion_state = 'failed'
        self.complete(svc)
        self.assertEqual(svc.runtime.state, 'recovery_required')
        self.assertEqual(len(self.nav.posts), 1)
        self.assertEqual(svc.runtime.record['holding'], 'cola_can_1')

    def test_cancel_rejects_late_close_vision_without_finishing_or_delivery(self):
        svc = self.at_close(); rt = svc.runtime
        context = rt.door_observation_context(); result = proof(context)
        rt.request_cancel()
        self.assertFalse(rt.apply_door_observation(context, result, authorization='Bearer test-only'))
        self.assertEqual(rt.state, 'cancelled'); self.assertEqual(len(self.nav.posts), 1)
        self.assertEqual(self.vla.finish_posts, 2)

    def test_unknown_closed_door_does_not_finish_and_open_contract_is_rejected(self):
        svc = self.at_close(); rt = svc.runtime
        context = rt.door_observation_context(); result = proof(context)
        result.update(status='unknown'); result.pop('evidence')
        self.assertFalse(rt.apply_door_observation(context, result, authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_posts, 2)
        context = rt.door_observation_context(); result = proof(context)
        result['evidence']['visual_contract'] = VISUAL_CONTRACT
        with self.assertRaises(Rejected):
            rt.apply_door_observation(context, result, authorization='Bearer test-only')
        self.assertEqual(len(self.nav.posts), 1)

    def test_historical_v2_snapshot_keeps_original_workflow(self):
        definition = load_skill(path=BASELINE_PATH.with_name('reception_single_can_v2.yaml')).definition
        definition['real_workflow']['sop']['version'] = '2'
        definition['real_workflow']['steps'] = [s for s in definition['real_workflow']['steps'] if s['action'] != 'close_fridge']
        old = compile_skill(definition).snapshot()
        self.assertNotIn('close_fridge', [s.kind for s in from_snapshot(old).real_steps])


class CloseVisionTests(unittest.TestCase):
    def make(self, arm_valid=True, closed=True):
        camera = Camera(); camera.arm['valid'] = arm_valid
        original = camera.latest
        def latest(*args, **kwargs):
            meta, raw = original(*args, **kwargs)
            meta['task_binding']['operation'] = 'close_fridge'
            return meta, raw
        camera.latest = latest
        calls = []
        def chat(endpoint, messages, **kwargs):
            calls.append(messages)
            return json.dumps(dict(schema=CLOSE_SCHEMA, operation='close_fridge',
                door_closed=closed, door_open=False, confidence=.96))
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        observer = DoorObserver({'reception_real':{}}, tmp.name, camera=camera, chat=chat)
        context = dict(task_id='t', command_id='close', attempt_id='a', operation='close_fridge',
                       execution_ready_at=(camera.after-timedelta(seconds=1)).isoformat())
        return observer.sample(context), camera, calls

    def test_one_head_image_and_original_arm_return_with_loose_closure_prompt(self):
        result, camera, calls = self.make()
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(camera.calls, ['ego_view', 'ego_view'])
        self.assertEqual(len(result['evidence']['frames']), 1)
        self.assertEqual(result['evidence']['visual_contract'], CLOSE_CONTRACT)
        self.assertIn('不判断密封条', calls[0][0]['content'][0]['text'])
        validate_evidence('close_fridge', 'fridge_1', result['evidence'], camera.after.isoformat())

    def test_missing_arm_receipt_never_calls_vlm_and_unknown_door_never_passes(self):
        result, _, calls = self.make(arm_valid=False)
        self.assertEqual(result['status'], 'unknown'); self.assertEqual(calls, [])
        result, _, _ = self.make(closed=None)
        self.assertEqual(result['status'], 'unknown'); self.assertNotIn('evidence', result)
        answer = dict(schema=CLOSE_SCHEMA, operation='close_fridge', door_closed=True, door_open=True, confidence=.99)
        self.assertFalse(semantic_pass(answer, .9, 'close_fridge'))
