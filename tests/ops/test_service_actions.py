"""Service-card operations are isolated; real processes and network are never used."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
import subprocess,threading,unittest
from unittest.mock import patch
from ops.app import create_app
from ops.services import Service

class ServiceActionConcurrencyTests(unittest.TestCase):
 def setUp(self):
  self.app=create_app();self.app.testing=True
  for worker in self.app.extensions['workers']:self.addCleanup(worker.shutdown,wait=False,cancel_futures=True)
  self.services={s:Service(id=s,name=s,layer='brain',controllable=True) for s in ('master','deploy')}
  for target,kwargs in [
   ('ops.app.by_id',dict(side_effect=self.services.__getitem__)),
   ('ops.app._service_status',dict(side_effect=lambda service:{'id':service.id,'state':'tmux'})),
   ('shared.execution_profile.admission',dict(side_effect=lambda **kw:nullcontext())),
   ('ops.app._stop',dict(return_value=None))]:
   p=patch(target,**kwargs);p.start();self.addCleanup(p.stop)
 def post(self,service,action='start'):
  with self.app.test_client() as client:return client.post(f'/api/services/{service}/{action}')
 def test_same_service_is_nonblocking_409_while_other_service_completes(self):
  entered,release=threading.Event(),threading.Event();calls=[]
  self.addCleanup(release.set)
  def start(service):
   calls.append(service.id)
   if service.id=='master':entered.set();release.wait(3)
  with patch('ops.app._start',side_effect=start),ThreadPoolExecutor(max_workers=2) as pool:
   original=pool.submit(self.post,'master','restart')
   self.assertTrue(entered.wait(1))
   try:
    duplicate=pool.submit(self.post,'master','stop').result(1)
    self.assertEqual(duplicate.status_code,409);self.assertEqual(duplicate.json['service_id'],'master')
    independent=pool.submit(self.post,'deploy').result(1)
    self.assertEqual(independent.status_code,200)
    self.assertEqual(calls,['master','deploy'])
   finally:release.set()
   self.assertEqual(original.result(1).status_code,200)
 def test_failed_action_releases_only_its_service_guard(self):
  with patch('ops.app._start',side_effect=[RuntimeError('startup failed'),None]):
   self.assertEqual(self.post('master').status_code,400)
   self.assertEqual(self.post('master').status_code,200)
 def test_execution_switch_admission_still_blocks_local_actions(self):
  with patch('shared.execution_profile.admission',side_effect=ValueError('运行环境正在切换')),patch('ops.app._start') as start:
   response=self.post('master');self.assertEqual(response.status_code,400);start.assert_not_called()
  with patch('ops.app._start'):
   self.assertEqual(self.post('master').status_code,200)

class ServiceCardBrowserTests(unittest.TestCase):
 def browser(self,script):
  source=Path('src/ops/static/panel.js').read_text()
  action=source[source.index('function showServiceOperation('):source.index('async function loadLogs(')]
  setup=r'''
const assert=require('node:assert/strict');
const serviceOperations=new Map(),snapshot={};let selectedLayer='brain';
const REMOTE_ACTION_LABEL={};const paints=[];const controllers=[];const requests=[];
function $(id){return null;}function refresh(){}function loadLogs(){}function selectService(){}
const window={confirm(){return true;}};
const AbortSignal={timeout(ms){const c=new AbortController();controllers.push({ms,c});return c.signal;}};
function fetch(url,options){return new Promise((resolve,reject)=>{
 const entry={url,options,resolve,reject};requests.push(entry);
 options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','TimeoutError')));
});}
function reply(i,payload={ok:true},ok=true){requests[i].resolve({ok,json:async()=>payload});}
async function flush(){for(let i=0;i<10;i++)await Promise.resolve();}
'''
  result=subprocess.run(['node','-e',setup+action+'\n(async()=>{'+script+'})().catch(e=>{console.error(e);process.exitCode=1;});'],capture_output=True,text=True,timeout=10)
  self.assertEqual(result.returncode,0,result.stderr)
 def test_stalled_card_does_not_block_another_or_overwrite_its_result(self):
  self.browser(r'''
const master={id:'master',name:'大脑'},deploy={id:'deploy',name:'任务网页'};
const first=act(master,'restart');assert(serviceOperations.get('master').pending);
await act(master,'stop');assert.equal(requests.length,1);
const second=act(deploy,'start');assert.equal(requests.length,2);
reply(1);await second;
assert(!serviceOperations.get('deploy').pending);assert(!serviceOperations.get('deploy').error);
assert(serviceOperations.get('deploy').text.includes('已完成'));assert(serviceOperations.get('master').pending);
reply(0,{ok:false,error:'大脑启动失败'},false);await first;
assert.equal(serviceOperations.get('master').text,'大脑启动失败');
assert(serviceOperations.get('master').error);assert(serviceOperations.get('deploy').text.includes('已完成'));
''')
 def test_timeout_is_unknown_result_and_never_retries_or_locks_other_cards(self):
  self.browser(r'''
const first=act({id:'master',name:'大脑'},'restart');
assert.equal(controllers[0].ms,45000);controllers[0].c.abort();await first;
assert(!serviceOperations.get('master').pending);assert(serviceOperations.get('master').error);
assert(serviceOperations.get('master').text.includes('操作结果未确认'));assert.equal(requests.length,1);
const second=act({id:'deploy',name:'网页'},'start');reply(1);await second;
const remote=act({id:'vla',name:'VLA',remote_control:'ssh_vla'},'start');
assert.equal(controllers[2].ms,620000);
reply(2,{ok:true,confirmed:true,message:'已核验'});await remote;
assert.equal(serviceOperations.get('vla').text,'已核验');
''')
 def test_invalid_reply_releases_card_without_claiming_no_execution(self):
  self.browser(r'''
const pending=act({id:'master',name:'大脑'},'restart');
requests[0].resolve({ok:true,json:async()=>{throw SyntaxError('invalid JSON');}});await pending;
assert(!serviceOperations.get('master').pending);assert(serviceOperations.get('master').text.includes('操作结果未确认'));
assert.equal(requests.length,1);
''')
 def test_service_notices_have_no_global_banner(self):
  self.assertNotIn('id="action-status"',Path('src/ops/templates/index.html').read_text())
  self.assertNotIn('$("action-status")',Path('src/ops/static/panel.js').read_text())
