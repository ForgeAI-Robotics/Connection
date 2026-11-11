"""Brain client/observer against actual VLA HTTP, with fake executor and images only."""
import sys
from pathlib import Path
import json,time,threading,tempfile,unittest
from unittest.mock import patch
from brain.adapters.door_verification import DoorObserver,SCHEMA,GRASP_SCHEMA
from brain.adapters.vla_camera import VlaCameraClient
from brain.adapters.vla_client import VlaClient

class DoorVlaPeerTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  import os
  source=os.environ.get('CONNECTION_VLA_SOURCE')
  if not source: raise unittest.SkipTest('Set CONNECTION_VLA_SOURCE for actual VLA offline HTTP integration')
  cls.source=str(Path(source).resolve());sys.path.insert(0,cls.source)
  global bridge,Driver,task,ROOT,bundle
  from g1_brain_vla_bridge import bridge_service as bridge
  from g1_brain_vla_bridge.tests.test_direct_execution import Driver,task,ROOT
  from g1_brain_vla_bridge.tests.test_camera_read import bundle
 @classmethod
 def tearDownClass(cls):sys.path.remove(cls.source)
 def test_bound_camera_to_original_finish(self):
  self.exercise()
 def test_joint_unknown_does_not_requery_or_finish(self):
  self.exercise(uncertain=True)
 def test_visual_pass_cannot_override_failed_physical_stop(self):
  self.exercise(stop_confirmed=False)
 def test_model_not_warm_is_definite_refusal_without_start_or_task(self):
  for code in ('VLA_MODEL_NOT_WARM','VLA_CAMERA_NOT_READY','VLA_CAMERA_CACHE_INVALID','VLA_PREPARATION_STOPPING'):
   with self.subTest(code=code):self.exercise(prepared_refusal=code)
 def test_revoked_arm_return_after_model_cannot_finish(self):
  self.exercise(revoke_arm=True)
 def test_counterfeit_arm_receipt_cannot_finish(self):
  self.exercise(forge_arm=True)
 def test_dual_wrist_grasp_auto_finishes_without_retreat_assertion(self):
  self.exercise(verify_grasp=True)
 def test_uncertain_grasp_does_not_finish(self):
  self.exercise(verify_grasp=True,grasp_unknown=True)
 def test_grasp_cleanup_failure_is_not_success(self):
  self.exercise(verify_grasp=True,grasp_stop_failure=True)
 def test_one_sided_grasp_does_not_finish(self):
  self.exercise(verify_grasp=True,grasp_one_side=True)
 def test_verified_grasp_finishes_without_o6_holding_registration(self):
  self.exercise(verify_grasp=True,grasp_missing_o6=True)
 def exercise(self, *, uncertain=False, stop_confirmed=True, prepared_refusal=False, revoke_arm=False, forge_arm=False,
              verify_grasp=False,grasp_unknown=False,grasp_stop_failure=False,grasp_one_side=False,grasp_missing_o6=False):
  with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'BRAIN_CONTROL_TOKEN':'b'*40,'BRAIN_CONTROL_TOKEN_FILE':'','EXPECTED_NX_SERIAL':'offline-robot'}):
   config=json.loads((ROOT/'g1_brain_vla_bridge/config.json').read_text())
   config.update(execution_mode='direct',runtime_dir=tmp,visual_evidence={'enabled':True},preparation={'enabled':False})
   config['gateway_control']['enabled']=False;config['backend']['real_action_enabled']=True
   config['camera'].update(host='127.0.0.1',port=1)
   runtime=bridge.BridgeRuntime(config,ROOT/'g1_brain_vla_bridge')
   driver=Driver();driver.stop_confirmed=stop_confirmed;original=driver.start;kinds=[]
   def start(kind,row,cancel):
    original(kind,row,cancel);kinds.append(kind)
    row.handoff['execution'].update(execution_epoch='offline-epoch',input_session_id='offline-session',controller_instance_id='offline-controller',identity={'robot_serial':'offline-robot'})
   driver.start=start
   driver.arm_return_status=lambda row:dict(schema='fq/vla-arm-return/v1',valid=True,event_id='event-1',return_dwell_s=.8)
   driver.arm_hold_status=lambda row:dict(schema='fq/vla-arm-hold/v1',valid=True,event_id='hold-1',hold_dwell_s=.8)
   runtime.backend.native.driver=driver
   server=bridge.BridgeHTTPServer(('127.0.0.1',0),runtime)
   thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
   try:
    url='http://127.0.0.1:'+str(server.server_port)
    client=VlaClient(url);client.http.auth_token_env='BRAIN_CONTROL_TOKEN'
    if prepared_refusal:
     from brain.adapters.http_client import HttpContractError
     from contracts.submission import submission_refusal
     with patch.object(runtime.preparation,'admission',return_value=dict(ready=False,code=prepared_refusal,blockers=['loading'])):
      with self.assertRaises(HttpContractError) as caught:client.submit_task(task())
     refusal=submission_refusal(caught.exception.status_code,caught.exception.payload,command_id='open-1',task_id='business-1')
     self.assertIsNotNone(refusal,caught.exception.payload)
     self.assertNotIn('open-1',runtime._tasks);self.assertEqual(driver.calls,[])
     return
    client.submit_task(task())
    deadline=time.monotonic()+3
    while runtime.task('open-1').state!='running' and time.monotonic()<deadline:time.sleep(.01)
    row=runtime.task('open-1');self.assertEqual(row.state,'running')
    camera=VlaCameraClient(url)
    read=camera.latest
    def fresh(*args,**kwargs):
     runtime.camera_stream.ingest(bundle(time.time()-.001))
     return read(*args,**kwargs)
    camera.latest=fresh
    brain_config={'reception_real':{'vla_base_url':url,'door_verification':{'enabled':True,'mode':'auto'}}}
    model_calls=[]
    def model(endpoint,messages,**kwargs):
     model_calls.append(1)
     views=[part['text'].split()[0] for part in messages[0]['content'][1:] if part['type']=='text']
     self.assertEqual(views,['ego_view'])
     if uncertain:
      return json.dumps(dict(schema=SCHEMA,operation='open_fridge',door_open=None,door_closed=None,confidence=.3))
     return json.dumps(dict(schema=SCHEMA,operation='open_fridge',door_open=True,door_closed=False,confidence=.99))
    observer=DoorObserver(brain_config,tmp,camera=camera,chat=model)
    context=dict(task_id=row.task_id,command_id=row.command_id,attempt_id='offline-attempt',execution_ready_at=row.execution_ready_at)
    result=None
    time.sleep(.02);result=observer.sample(context)
    self.assertEqual(model_calls,[1]);self.assertEqual(result['vlm_calls'],1)
    if uncertain:
     self.assertEqual(result['status'],'unknown');self.assertNotIn('evidence',result)
     self.assertFalse(runtime.task(row.command_id).finish_requested)
     self.assertEqual([x[0] for x in driver.calls],['start']);return
    self.assertEqual(result['status'],'pass',result)
    evidence=result['evidence']
    self.assertEqual(len(evidence['frames']),1)
    if revoke_arm or forge_arm:
     from brain.adapters.http_client import HttpContractError
     if revoke_arm:
      driver.arm_return_status=lambda row:dict(schema='fq/vla-arm-return/v1',valid=False,event_id='event-1',return_dwell_s=.8)
     else:
      evidence['arm_return']['event_id']='counterfeit'
     with self.assertRaises(HttpContractError):client.finish(row.task_id,row.command_id,evidence,'Bearer '+runtime._completion_token)
     self.assertFalse(runtime.task(row.command_id).finish_requested)
     self.assertEqual(kinds,['fridge'])
     return
    reply=client.finish(row.task_id,row.command_id,evidence,'Bearer '+runtime._completion_token)
    self.assertTrue(reply.get('finish_requested'),reply)
    terminal=client.wait_task(row.command_id,timeout_sec=3,poll_interval_sec=.02)
    self.assertEqual(terminal['state'],'succeeded' if stop_confirmed else 'failed',terminal)
    if not stop_confirmed:
     self.assertEqual(kinds,['fridge']);return
    self.assertEqual(terminal['completion']['evidence'],evidence)
    self.assertEqual([x[0] for x in driver.calls],['start','pause','stop'])
    deadline=time.monotonic()+3
    while runtime._active_command_id and time.monotonic()<deadline:time.sleep(.01)
    client.submit_task(task('cola-1',operation='grasp_cola'))
    deadline=time.monotonic()+3
    while runtime.task('cola-1').state!='running' and time.monotonic()<deadline:time.sleep(.01)
    self.assertEqual(runtime.task('cola-1').state,'running')
    self.assertEqual(kinds,['fridge','cola'])
    if verify_grasp:
     row=runtime.task('cola-1');self.assertFalse(row.finish_requested)
     def cola_model(endpoint,messages,**kwargs):
      views=[part['text'].split()[0] for part in messages[0]['content'][1:] if part['type']=='text']
      self.assertEqual(views,['left_wrist','right_wrist'])
      return json.dumps(dict(schema=GRASP_SCHEMA,operation='grasp_cola',left_grasped=True,right_grasped=None if grasp_unknown else False if grasp_one_side else True,confidence=.99))
     observer.chat=cola_model
     context=dict(task_id=row.task_id,command_id=row.command_id,attempt_id='cola-attempt',operation='grasp_cola',
                  object_id='cola_can_1',execution_ready_at=row.execution_ready_at)
     time.sleep(.02);result=observer.sample(context)
     if grasp_unknown or grasp_one_side:
      self.assertEqual(result['status'],'unknown');self.assertFalse(runtime.task(row.command_id).finish_requested);return
     self.assertEqual(result['status'],'pass',result)
     evidence=result['evidence'];self.assertEqual(len(evidence['frames']),2)
     self.assertNotIn('clear_of_fridge',evidence);self.assertNotIn('door_open',evidence)
     driver.stop_confirmed=not grasp_stop_failure
     if grasp_missing_o6:
      stop=driver.stop
      driver.stop=lambda kind:dict(stop(kind),hands_stopped=None,hold_verified=None,output_fenced=None,
                                    holding_registered=False,holding_evidence=None)
     client.finish(row.task_id,row.command_id,evidence,'Bearer '+runtime._completion_token)
     terminal=client.wait_task(row.command_id,timeout_sec=3,poll_interval_sec=.02)
     self.assertEqual(terminal['state'],'failed' if grasp_stop_failure else 'succeeded',terminal)
     if not grasp_stop_failure:self.assertEqual(terminal['result']['evidence'],evidence)
     if grasp_missing_o6:
      from contracts.vla_direct import stop_facts
      self.assertEqual(stop_facts(terminal),(True,True))
      self.assertIsNone(terminal['result']['hands_stopped'])
      self.assertNotIn('holding_evidence',terminal['result'])
   finally:
    for event in runtime._cancel.values():event.set()
    deadline=time.monotonic()+3
    while runtime._active_command_id and time.monotonic()<deadline:time.sleep(.01)
    server.shutdown();server.server_close();thread.join();runtime.close()

if __name__=='__main__':unittest.main()
