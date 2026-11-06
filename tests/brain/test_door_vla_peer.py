"""Brain client/observer against actual VLA HTTP, with fake executor and images only."""
import sys
from pathlib import Path
import json,time,threading,tempfile,unittest
from unittest.mock import patch
from brain.adapters.door_verification import DoorObserver,SCHEMA
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
  for code in ('VLA_MODEL_NOT_WARM','VLA_CAMERA_CACHE_INVALID','VLA_PREPARATION_STOPPING'):
   with self.subTest(code=code):self.exercise(prepared_refusal=code)
 def exercise(self, *, uncertain=False, stop_confirmed=True, prepared_refusal=False):
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
   driver.start=start;runtime.backend.native.driver=driver
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
     self.assertEqual(views,['ego_view']*3+['left_wrist','right_wrist'])
     if uncertain:
      return json.dumps(dict(schema=SCHEMA,operation='open_fridge',needs_wrist=True,hands_clear_of_door=None))
     return json.dumps(dict(schema=SCHEMA,operation='open_fridge',door_open=True,door_stable_open=True,hands_clear_of_door=True,door_closed=False,needs_wrist=False,confidence=.99))
    observer=DoorObserver(brain_config,tmp,camera=camera,chat=model)
    context=dict(task_id=row.task_id,command_id=row.command_id,attempt_id='offline-attempt',execution_ready_at=row.execution_ready_at,remaining_vlm_calls=10)
    result=None
    for _ in range(3):
     time.sleep(.55);result=observer.sample(context)
    self.assertEqual(model_calls,[1]);self.assertEqual(result['vlm_calls'],1)
    if uncertain:
     self.assertEqual(result['status'],'unknown');self.assertNotIn('evidence',result)
     self.assertFalse(runtime.task(row.command_id).finish_requested)
     self.assertEqual([x[0] for x in driver.calls],['start']);return
    self.assertEqual(result['status'],'pass',result)
    evidence=result['evidence']
    self.assertEqual(len(evidence['frames']),5)
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
   finally:
    for event in runtime._cancel.values():event.set()
    deadline=time.monotonic()+3
    while runtime._active_command_id and time.monotonic()<deadline:time.sleep(.01)
    server.shutdown();server.server_close();thread.join();runtime.close()

if __name__=='__main__':unittest.main()
