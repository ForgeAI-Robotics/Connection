"""Offline temporal vision and original-command finish; no robot or model service."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

from brain.adapters.door_verification import DoorObserver, SCHEMA, VISUAL_CONTRACT, semantic_pass
from brain.adapters.vla_camera import VlaCameraClient
from brain.adapters.http_client import HttpContractError
from tests.brain import test_running_finish as finish_fixture
from tests.brain.test_real_camera import picture


def answer(**changes):
    return dict(schema=SCHEMA, operation='open_fridge', door_open=True, door_stable_open=True,
                hands_clear_of_door=True, door_closed=False, needs_wrist=False, confidence=.96, **changes)


class Camera:
    def __init__(self):
        self.count = 0
        self.head_count = 0
        self.instance = 'service-1'
        self.after = datetime.now(timezone.utc) - timedelta(seconds=3)
        self.color = 120
        self.calls = []

    def latest(self, view='ego_view', *, task_id=None, command_id=None):
        self.count += 1
        self.calls.append(view)
        self.head_count += int(view == 'ego_view')
        offset = self.head_count * .6 + {'left_wrist':.01, 'right_wrist':.005}.get(view, 0)
        raw = io.BytesIO(); Image.new('RGB', (64, 48), (self.color,)*3).save(raw, format='JPEG')
        raw = raw.getvalue()
        return dict(camera_key=view, frame_id=str(self.count), captured_at=(self.after+timedelta(seconds=offset)).isoformat(),
                    service_instance_id=self.instance, camera_receiver_instance_id='cam1', robot_serial='g1-test',
                    frame_receipt_id=str(self.count), binding_proof='proof-'+str(self.count),
                    task_binding=dict(task_id=task_id, command_id=command_id, operation='open_fridge', execution_epoch='epoch',
                                      controller_instance_id='controller1', state_at_read='running',
                                      execution_ready_at=(self.after-timedelta(seconds=1)).isoformat()),
                    sha256=hashlib.sha256(raw).hexdigest()), raw


class DoorObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.camera = Camera(); self.t = 0; self.answers = []; self.model_calls = 0; self.model_views = []
        config = {'reception_real': {'door_verification': {'enabled':True, 'mode':'auto'}}}
        def chat(*args, **kwargs):
            self.model_calls += 1
            self.model_views.append([v['text'].split()[0] for v in args[1][0]['content'][1:] if v['type']=='text'])
            result = self.answers.pop(0) if self.answers else answer()
            if callable(result): return result()
            return json.dumps(result)
        self.observer = DoorObserver(config, self.tmp.name, camera=self.camera, chat=chat, clock=lambda:self.t)
        self.context = dict(task_id='t1', command_id='c1', attempt_id='a1', execution_ready_at=(self.camera.after-timedelta(seconds=1)).isoformat(), remaining_vlm_calls=10)

    def sample(self):
        self.t += .6
        return self.observer.sample(self.context)

    def run_candidate(self):
        self.assertIsNone(self.sample()); self.assertIsNone(self.sample()); return self.sample()

    def test_temporal_candidate_semantic_pass_and_saved_source_frames(self):
        result = self.run_candidate()
        self.assertEqual(result['status'], 'pass'); self.assertEqual(self.model_calls, 1)
        proof = json.loads(Path(result['reference']).read_text())
        self.assertEqual(proof['command_id'], 'c1'); self.assertEqual(len(proof['frames']), 6)
        self.assertEqual(len(result['evidence']['frames']), 5)
        self.assertEqual(self.model_views, [['ego_view']*3 + ['left_wrist','right_wrist']])
        for frame in proof['frames']:
            raw=(Path(result['reference']).parent/frame['file']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), frame['sha256'])
        self.assertEqual(result['evidence']['observed_at'], max((f['captured_at'] for f in proof['frames'][:5]), key=datetime.fromisoformat))

    def test_still_closed_or_unseen_hand_never_passes(self):
        for key,value in [('door_open',False),('hands_clear_of_door',None),('door_stable_open',False),('confidence',float('nan')),('door_closed',True),('confidence',True)]:
            candidate=answer(); candidate[key]=value
            self.assertFalse(semantic_pass(candidate,.9))
        bad=answer();bad['door_open']=False;self.answers=[bad]
        self.assertEqual(self.run_candidate()['status'],'unknown')

    def test_motion_does_not_trigger_model(self):
        self.sample(); self.camera.color=180;self.sample();self.camera.color=50
        self.assertIsNone(self.sample());self.assertEqual(self.model_calls,0)

    def test_changed_scene_after_model_rejects_pass(self):
        def changed():self.camera.color=240;return json.dumps(answer())
        self.answers=[changed]
        self.assertEqual(self.run_candidate()['status'],'unknown')

    def test_joint_unknown_never_triggers_second_model_request(self):
        uncertain=answer();uncertain.update(needs_wrist=True,hands_clear_of_door=None)
        self.answers=[uncertain,answer()]
        result=self.run_candidate()
        self.assertEqual(result['vlm_calls'],1)
        self.assertEqual(self.model_calls,1);self.assertEqual(result['status'],'unknown')
        self.assertNotIn('evidence',result)
        self.assertEqual(len(self.answers),1)
        self.assertIn('left_wrist',self.camera.calls);self.assertIn('right_wrist',self.camera.calls)

    def test_wrist_acquisition_order_does_not_replace_latest_capture_time(self):
        latest=self.camera.latest
        def skew(view,**kwargs):
            meta,raw=latest(view,**kwargs)
            if view=='right_wrist':
                meta['captured_at']=(datetime.fromisoformat(meta['captured_at'])-timedelta(seconds=.7)).isoformat()
            return meta,raw
        self.camera.latest=skew
        result=self.run_candidate()
        self.assertEqual(result['status'],'pass',result)
        proof=result['evidence']
        self.assertEqual(proof['observed_at'],max((f['captured_at'] for f in proof['frames']),key=datetime.fromisoformat))
        self.assertEqual(proof['observation_window']['end'],proof['observed_at'])

    def test_last_remaining_call_still_includes_both_wrists(self):
        self.context['remaining_vlm_calls']=1
        result=self.run_candidate()
        self.assertEqual(result['status'],'pass');self.assertEqual(self.model_calls,1)
        self.assertEqual(self.model_views,[['ego_view']*3+['left_wrist','right_wrist']])

    def test_missing_or_wrong_identity_wrist_never_falls_back_to_head_only(self):
        latest=self.camera.latest
        for fault in ('missing','epoch'):
            with self.subTest(fault=fault):
                self.observer.clear()
                def broken(view,**kwargs):
                    if view=='right_wrist' and fault=='missing':raise TimeoutError('wrist unavailable')
                    meta,raw=latest(view,**kwargs)
                    if view=='right_wrist':meta['task_binding']['execution_epoch']='old'
                    return meta,raw
                self.camera.latest=broken
                result=self.run_candidate()
                self.assertEqual(result['status'],'unknown');self.assertEqual(result['vlm_calls'],0)
                self.assertEqual(self.model_calls,0);self.assertNotIn('evidence',result)

    def test_out_of_sync_joint_views_do_not_reach_model(self):
        latest=self.camera.latest
        def skew(view,**kwargs):
            meta,raw=latest(view,**kwargs)
            if view=='right_wrist':meta['captured_at']=(datetime.fromisoformat(meta['captured_at'])-timedelta(seconds=1.1)).isoformat()
            return meta,raw
        self.camera.latest=skew
        result=self.run_candidate();self.assertEqual(result['status'],'unknown')
        self.assertEqual(self.model_calls,0)

    def test_slow_model_unknown_and_budget_is_recorded(self):
        def slow():self.t+=4;return json.dumps(answer())
        self.answers=[slow]
        result=self.run_candidate();self.assertEqual(result['status'],'unknown');self.assertEqual(result['vlm_calls'],1)

    def test_model_exception_never_finishes(self):
        def failure():raise TimeoutError('not for persisted diagnostics')
        self.answers=[failure]
        result=self.run_candidate();self.assertEqual(result['status'],'unknown');self.assertEqual(result['reason'],'TimeoutError')
        self.assertEqual(result['vlm_calls'],1)

    def test_new_command_resets_temporal_window(self):
        self.sample();self.sample();self.context['command_id']='c2'
        self.assertIsNone(self.sample());self.assertEqual(self.model_calls,0)

    def test_control_revision_change_discards_pre_control_frames(self):
        self.context['revision']=1;self.sample();self.sample();self.context['revision']=2
        self.assertIsNone(self.sample());self.assertEqual(self.model_calls,0)

    def test_camera_service_restart_resets_window(self):
        self.sample();self.sample();self.camera.instance='service-2'
        self.assertIsNone(self.sample());self.assertEqual(self.model_calls,0)


class BoundCameraTests(unittest.TestCase):
    def test_wrist_requires_original_task_and_valid_response_binding(self):
        from unittest.mock import Mock
        frame, raw=picture();frame.update(schema='fq/vla-camera-frame/v2',camera_key='left_wrist',service_instance_id='svc',
            camera_receiver_instance_id='cam', robot_serial='robot', frame_receipt_id='receipt', binding_proof='proof',
            task_binding=dict(task_id='task',command_id='cmd',operation='open_fridge',execution_epoch='epoch',controller_instance_id='ctrl'))
        http=Mock();http.request_json.return_value=(frame,200,{})
        camera=VlaCameraClient('http://invalid',http=http)
        self.assertEqual(camera.latest('left_wrist',task_id='task',command_id='cmd')[1],raw)
        self.assertIn('task_id=task&command_id=cmd',http.request_json.call_args.args[1])
        with self.assertRaises(ValueError):camera.latest('left_wrist')
        frame['task_binding']['execution_epoch']=True
        with self.assertRaises(HttpContractError):camera.latest('left_wrist',task_id='task',command_id='cmd')
        frame['task_binding']['execution_epoch']='epoch'
        frame['task_binding']['command_id']='other'
        with self.assertRaises(HttpContractError):camera.latest('left_wrist',task_id='task',command_id='cmd')


class RuntimeDoorTests(unittest.TestCase):
    setUp=finish_fixture.RunningFinishTests.setUp
    tearDown=finish_fixture.RunningFinishTests.tearDown
    service=finish_fixture.RunningFinishTests.service

    def enabled(self, mode='auto'):
        self.config['reception_real']['door_verification']={'enabled':True,'mode':mode}
        return self.service()

    def proposal(self, rt):
        context=rt.door_observation_context();self.assertIsNotNone(context)
        evidence=dict(source='visual_verifier',reference='/offline/evidence.json',observed_at=datetime.now(timezone.utc).isoformat(),
                      door_open=True,door_closed=False,door_stable_open=True,hands_clear_of_door=True,
                      binding=dict(task_id=context['task_id'],command_id=context['command_id']),visual_contract=VISUAL_CONTRACT)
        return context,dict(schema=SCHEMA,status='pass',vlm_calls=1,evidence=evidence)

    def test_pass_finishes_original_then_grasp_only_after_terminal(self):
        svc=self.enabled();rt=svc.runtime;context,result=self.proposal(rt)
        self.assertTrue(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(len(self.vla.posts),1);self.assertTrue(rt.pending_vla_completion())
        svc._drive(rt)
        self.assertEqual([r['operation'] for r in self.vla.posts],['open_fridge','grasp_cola'])
        self.assertIsNone(rt.door_observation_context());self.assertEqual(self.vla.finish_posts,1)
        self.assertNotIn('Bearer test-only',str(rt.record))

    def test_human_confirmation_wins_over_inflight_visual_result(self):
        from tests.brain.test_native_vla import evidence
        svc=self.enabled();rt=svc.runtime;context,result=self.proposal(rt)
        rt.vla_effect(context['task_id'],context['command_id'],'finish',
                      evidence=evidence(),authorization='Bearer test-only',auto_advance=True)
        self.assertFalse(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_posts,1)

    def test_shadow_records_without_finish(self):
        rt=self.enabled('shadow').runtime;context,result=self.proposal(rt)
        self.assertFalse(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_posts,0);self.assertEqual(len(self.vla.posts),1)

    def test_late_result_after_pause_rejected(self):
        rt=self.enabled().runtime;context,result=self.proposal(rt)
        rt.request_pause()
        self.assertFalse(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_posts,0)

    def test_late_result_after_cancel_rejected(self):
        rt=self.enabled().runtime;context,result=self.proposal(rt)
        rt.request_cancel()
        self.assertFalse(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_posts,0)

    def test_lost_finish_response_is_not_replayed(self):
        svc=self.enabled();rt=svc.runtime;context,result=self.proposal(rt);self.vla.lose_reply=True
        from contracts.tasks import Rejected
        with self.assertRaises(Rejected):rt.apply_door_observation(context,result,authorization='Bearer test-only')
        self.assertIsNone(rt.door_observation_context());svc._drive(rt)
        self.assertEqual(self.vla.finish_posts,1);self.assertEqual(self.vla.posts[-1]['operation'],'grasp_cola')

    def test_failure_in_finish_cleanup_does_not_grasp(self):
        svc=self.enabled();rt=svc.runtime;context,result=self.proposal(rt);self.vla.completion_state='failed'
        rt.apply_door_observation(context,result,authorization='Bearer test-only');svc._drive(rt)
        self.assertEqual(len(self.vla.posts),1);self.assertEqual(rt.state,'recovery_required')

    def test_existing_task_not_enabled_by_config_reload(self):
        rt=self.service().runtime
        self.config['reception_real']['door_verification']={'enabled':True,'mode':'auto'}
        self.assertIsNone(rt.door_observation_context())

    def test_unknown_counts_budget_across_runtime_reload(self):
        svc=self.enabled();rt=svc.runtime
        for i in range(10):
            context=rt.door_observation_context();self.assertIsNotNone(context)
            rt.apply_door_observation(context,dict(schema=SCHEMA,status='unknown',vlm_calls=1))
            rt=svc.attach()
        self.assertIsNone(rt.door_observation_context());self.assertEqual(self.vla.finish_posts,0)

    def test_duplicate_result_does_not_resubmit_finish(self):
        rt=self.enabled().runtime;context,result=self.proposal(rt)
        rt.apply_door_observation(context,result,authorization='Bearer test-only')
        self.assertFalse(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_posts,1)

    def test_expired_pass_records_but_does_not_finish(self):
        rt=self.enabled().runtime;context,result=self.proposal(rt)
        # A valid execution window, but inference is already outside the evidence budget.
        cid=context['command_id'];stamp=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()
        self.vla.commands[cid]['execution_ready_at']=stamp
        rt.record['vla_confirmation']['execution_ready_at']=stamp;rt._save('test_window')
        context=rt.door_observation_context()
        result['evidence']['observed_at']=(datetime.now(timezone.utc)-timedelta(seconds=8)).isoformat()
        self.assertFalse(rt.apply_door_observation(context,result,authorization='Bearer test-only'))
        self.assertEqual(self.vla.finish_posts,0)

    def test_application_rechecks_failure_after_visual_model(self):
        from brain.application import BrainApplication
        from brain.workers import WorkerPool, RuntimeOwner
        from unittest.mock import Mock
        svc=self.enabled();rt=svc.runtime;_,result=self.proposal(rt)
        app=object.__new__(BrainApplication);app.service=svc;app.config=self.config
        app.owner=RuntimeOwner();app.io=WorkerPool('offline-door',1)
        self.addCleanup(app.io.close);self.addCleanup(app.owner.close)
        app._door_future=None;app._door_observer=Mock();app._door_observer.options={}
        def sample(context):
            row=self.vla.commands[context['command_id']]
            row.update(state='failed',completed_at=datetime.now(timezone.utc).isoformat(),
                       result={'success':False,'policy_stopped':True},error={'code':'ARM_TRACKING_STALLED','message':'stalled'})
            self.vla._stop(row)
            return result
        app._door_observer.sample=sample
        with patch.dict('os.environ',{'VLA_VERIFIER_TOKEN':'test-only'}):app._observe_open_door(rt)
        self.assertEqual(rt.state,'recovery_required');self.assertEqual(self.vla.finish_posts,0)

    def test_application_no_overlapping_vlm_when_previous_call_is_still_running(self):
        from brain.application import BrainApplication
        from concurrent.futures import Future
        from unittest.mock import Mock
        svc=self.enabled();app=object.__new__(BrainApplication)
        app._door_future=Future();app._door_observer=Mock();app.io=Mock()
        app._observe_open_door(svc.runtime)
        app.io.submit.assert_not_called()

    def test_slow_vlm_cannot_block_cancel_or_apply_late_success(self):
        import threading,time
        from brain.app import create_service
        from brain.application import BrainApplication
        from brain.adapters.source_gate import SourceGateBodyAdapter
        from tests.brain.test_source_gate import Gate
        svc=self.enabled();_,result=self.proposal(svc.runtime);self.control.close()
        entered,release=threading.Event(),threading.Event()
        def sample(observer,context):
            entered.set();release.wait(3);return result
        restored=create_service(self.config,model=lambda _:self.fail('No model'),
            port_factory=lambda _:SourceGateBodyAdapter(self.nav,self.vla,restored.body_control))
        with patch('brain.kernel.source_gate.SourceGateClient',return_value=Gate()), \
             patch('brain.adapters.door_verification.DoorObserver.sample',sample):
            app=BrainApplication(restored).start()
            try:
                self.assertTrue(entered.wait(2))
                started=time.monotonic();response=app.control('cancel',task_id='test-task')
                self.assertTrue(response['accepted']);self.assertLess(time.monotonic()-started,1)
                release.set();time.sleep(.1)
                self.assertEqual(app.status()['state'],'cancelled');self.assertEqual(self.vla.finish_posts,0)
            finally:release.set();app.close(3)


class DoorCooperativeDeadlineTests(unittest.TestCase):
    def test_slow_observation_has_a_total_deadline_and_does_not_mutate_late(self):
        from brain.workers import RuntimeOwner, WorkerPool
        from concurrent.futures import TimeoutError
        import threading,time
        owner=RuntimeOwner();pool=WorkerPool('deadline-test',1)
        release=threading.Event();late=[]
        self.addCleanup(owner.close);self.addCleanup(pool.close);self.addCleanup(release.set)
        def blocked():release.wait(2);late.append('read-only result');return 'late'
        pending=pool.submit(blocked)
        started=time.monotonic()
        wait=owner.submit(lambda:owner.wait(pending,timeout_sec=.05))
        with self.assertRaises(TimeoutError):wait.result(1)
        self.assertLess(time.monotonic()-started,.5)
        self.assertFalse(pending.done())
        release.set();self.assertEqual(pending.result(1),'late')


class PreparedAdmissionTests(unittest.TestCase):
    def test_warm_refusal_requires_explicit_no_action_and_no_uncertainty(self):
        from contracts.submission import submission_refusal
        for code in ('VLA_MODEL_NOT_WARM','VLA_CAMERA_CACHE_INVALID','VLA_PREPARATION_STOPPING'):
            raw=dict(success=False,error=dict(code=code,message='not ready',
                     details=dict(action_started=False,action_state_uncertain=False)))
            self.assertIsNotNone(submission_refusal(503,raw,command_id='c',task_id='t'))
            for key in ('action_started','action_state_uncertain'):
                missing=deepcopy(raw);missing['error']['details'].pop(key)
                self.assertIsNone(submission_refusal(503,missing,command_id='c',task_id='t'))
            wrong=deepcopy(raw);wrong['command_id']='other'
            self.assertIsNone(submission_refusal(503,wrong,command_id='c',task_id='t'))
            self.assertIsNone(submission_refusal(504,raw,command_id='c',task_id='t'))
