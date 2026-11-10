"""Dual-wrist visual evidence through the existing observer; no model or hardware."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json,tempfile,unittest
from brain.adapters.door_verification import DoorObserver,GRASP_SCHEMA,GRASP_CONTRACT
from tests.brain.test_door_verification import Camera

class GraspObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.camera=Camera();self.t=0.;self.calls=[]
        self.hold=dict(schema='fq/vla-arm-return/v1',valid=True,event_id='hold-1',return_dwell_s=.8)
        latest=self.camera.latest
        def wrist(view,**kwargs):
            meta,raw=latest(view,**kwargs)
            meta['task_binding']['operation']='grasp_cola'
            meta['captured_at']=(self.camera.after+timedelta(seconds=self.camera.count*.05)).isoformat()
            meta['arm_return']=deepcopy(self.hold)
            return meta,raw
        self.camera.latest=wrist
        self.answer=dict(schema=GRASP_SCHEMA,operation='grasp_cola',left_grasped=True,right_grasped=True,confidence=.96)
        self.during=None
        def chat(endpoint,messages,**kwargs):
            self.calls.append(messages)
            if self.during:self.during()
            return json.dumps(self.answer)
        self.observer=DoorObserver({'reception_real':{'door_verification':{'enabled':True,'mode':'auto'}}},
            self.tmp.name,camera=self.camera,chat=chat,clock=lambda:self.t)
        self.context=dict(task_id='t',command_id='c',attempt_id='a',operation='grasp_cola',object_id='cola_can_1',
            execution_ready_at=(self.camera.after-timedelta(seconds=1)).isoformat(),remaining_vlm_calls=10)
    def sample(self):
        self.t+=.6;return self.observer.sample(self.context)
    def test_both_wrists_once_and_no_retreat_or_door_assertions(self):
        result=self.sample();self.assertEqual(result['status'],'pass',result)
        self.assertEqual(self.camera.calls,['left_wrist','right_wrist']*2)
        content=self.calls[0][0]['content']
        self.assertEqual(sum(p['type']=='image_url' for p in content),2)
        evidence=result['evidence'];self.assertEqual(evidence['visual_contract'],GRASP_CONTRACT)
        self.assertEqual(evidence['holding'],'cola_can_1')
        self.assertEqual([f['camera_key'] for f in evidence['frames']],['left_wrist','right_wrist'])
        for field in ('clear_of_fridge','door_open','door_stable_open','hands_clear_of_door'):
            self.assertNotIn(field,evidence)
    def test_unconfirmed_object_never_passes(self):
        self.answer['right_grasped']=None
        result=self.sample();self.assertEqual(result['status'],'unknown');self.assertNotIn('evidence',result)
    def test_either_side_false_or_unknown_never_passes(self):
        for left,right in [(True,False),(False,True),(True,None),(None,True),(None,None)]:
            with self.subTest(left=left,right=right):
                self.observer.clear();self.answer.update(left_grasped=left,right_grasped=right,object_grasped=True)
                result=self.sample();self.assertEqual(result['status'],'unknown');self.assertNotIn('evidence',result)

    def test_old_low_speed_hold_signal_is_not_a_return_signal(self):
        self.hold.clear();self.hold.update(schema='fq/vla-arm-hold/v1',valid=True,event_id='old-hold',hold_dwell_s=.8)
        result=self.sample();self.assertEqual(result['status'],'unknown');self.assertEqual(self.calls,[])

    def test_missing_hold_does_not_call_model(self):
        self.hold['valid']=False
        result=self.sample();self.assertEqual(result['vlm_calls'],0);self.assertEqual(self.calls,[])
    def test_missing_wrist_or_foreign_execution_does_not_call_model(self):
        latest=self.camera.latest
        def broken(view,**kwargs):
            if view=='right_wrist':raise TimeoutError()
            return latest(view,**kwargs)
        self.camera.latest=broken
        self.assertEqual(self.sample()['status'],'unknown');self.assertEqual(self.calls,[])
    def test_cross_execution_wrists_rejected_before_model(self):
        latest=self.camera.latest
        def foreign(view,**kwargs):
            meta,raw=latest(view,**kwargs)
            if view=='right_wrist':meta['task_binding']['execution_epoch']='old'
            return meta,raw
        self.camera.latest=foreign
        self.assertEqual(self.sample()['status'],'unknown');self.assertEqual(self.calls,[])
    def test_out_of_sync_wrists_rejected_before_model(self):
        latest=self.camera.latest
        def skew(view,**kwargs):
            meta,raw=latest(view,**kwargs)
            if view=='right_wrist':meta['captured_at']=(datetime.fromisoformat(meta['captured_at'])-timedelta(seconds=1.2)).isoformat()
            return meta,raw
        self.camera.latest=skew
        self.assertEqual(self.sample()['status'],'unknown');self.assertEqual(self.calls,[])
    def test_hold_changed_during_model_does_not_finish(self):
        self.during=lambda:self.hold.update(event_id='new-hold')
        result=self.sample();self.assertEqual(result['status'],'unknown');self.assertNotIn('evidence',result)
    def test_slow_model_does_not_finish(self):
        self.during=lambda:setattr(self,'t',self.t+4)
        result=self.sample();self.assertEqual(result['status'],'unknown');self.assertNotIn('evidence',result)
