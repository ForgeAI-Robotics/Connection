import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock

from brain.api.reporting import reception_report
from brain.kernel.runtime import status_from_record


class TaskFeedbackTests(unittest.TestCase):
    def test_status_proxy_has_short_deadline_and_no_cache(self):
        from entries.web.app import create_app
        client = Mock()
        client.request.return_value = Mock(content=b'{"state":"running"}', status_code=200,
                                           headers={'content-type': 'application/json'})
        app = create_app(client=client, ops_client=client).test_client()
        response = app.get('/api/task_status')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(client.request.call_args.kwargs['timeout'], 5)
        client.request.side_effect = TimeoutError('offline')
        self.assertEqual(app.get('/api/task_status').status_code, 503)

    def test_late_report_cannot_replace_another_command_or_state(self):
        page = Path('src/entries/web/templates/index.html').read_text()
        code = page[page.index('        async function loadReceptionReport'):page.index('        async function copyReceptionSummary')]
        script = '''const assert=require('assert');
let displayedTask={task_id:'t',state:'recovery_required',control_command_id:'old'};
let reportRequest=0,receptionSummary=''; const node={innerHTML:'current'};
const TASK_LABELS={states:{running:'执行中'}};
const document={getElementById:()=>node};const pending=[];
function taskJSON(){return new Promise((resolve,reject)=>pending.push({resolve,reject}));}
function taskText(x){return x;}function taskReason(x){return x;}
''' + code + '''
(async()=>{
const old=loadReceptionReport();
displayedTask.control_command_id='new';
pending[0].resolve({success:true,report:{task_id:'t',state:'recovery_required',summary_text:'OLD'}});
await old;assert.equal(node.innerHTML,'current');
const failed=loadReceptionReport();displayedTask.state='running';
pending[1].reject(new Error('late'));await failed;assert.equal(node.innerHTML,'current');
const fresh=loadReceptionReport();
pending[2].resolve({success:true,report:{task_id:'t',state:'running',summary_text:'NEW'}});
await fresh;assert(node.innerHTML.includes('NEW'));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        run = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)

    def browser(self, script, *, control=False):
        page = Path('src/entries/web/templates/index.html').read_text()
        code = page[page.index('        let taskSubmitting'):page.index('        // 发布任务')]
        if control:
            code += page[page.index('        async function controlReception'):page.index("        document.getElementById('task').addEventListener")]
        setup = '''const assert=require('assert'); const setInterval=()=>1;
const nodes={}; const document={getElementById(id){return nodes[id] ||= {style:{},innerHTML:'previous-progress',textContent:''};}};
const window={confirm:()=>true}; let lastTaskResponse=null;
function taskDisplayContext(d){return JSON.stringify([d?.task_id,d?.state,d?.control_command_id]);}
function taskPhase(x){return x;}
async function refreshTaskStatus(){const s=await readTaskStatus();markTaskStatus(s);displayedTask=s;}
'''
        run = subprocess.run(['node', '-e', setup+code+'\n(async()=>{'+script+'\n})().catch(e=>{console.error(e);process.exitCode=1;});'],
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_reads_coalesce_and_deadline_cancels_without_post(self):
        self.browser('''
let calls=0, resolve;
global.fetch=()=>{calls++;return new Promise(r=>{resolve=r;});};
const a=readTaskStatus(),b=readTaskStatus();assert.equal(calls,1);
resolve({ok:true,json:async()=>({state:'running'})});await Promise.all([a,b]);
global.fetch=(url,options)=>new Promise((r,j)=>options.signal.addEventListener('abort',()=>j(Object.assign(new Error(),{name:'AbortError'}))));
await assert.rejects(taskJSON('/api/task_status',{},10),/超时/);
assert.equal(statusReadPending,null);
''')

    def test_failure_keeps_last_progress_and_disables_stale_controls(self):
        self.browser('''
const data={task_id:'t',state:'recovery_required',terminal:false,can_resume:true};
displayedTask=data; markTaskStatus(data);updateTaskButtons();
assert(!nodes['task-continue'].disabled);
assert.equal(nodes['task-freshness'].textContent,'');
taskStatusLastReceived=Date.now()-1000;updateTaskFreshness();
assert.equal(nodes['task-freshness'].textContent,'');
markTaskStatusError(new Error('offline'));
assert.equal(document.getElementById('task-progress').innerHTML,'previous-progress');
assert(nodes['task-continue'].disabled);
assert(nodes['task-freshness'].textContent.includes('保留上次进度'));
markTaskStatus(data);taskStatusLastReceived=Date.now()-12000;updateTaskFreshness();
assert(nodes['task-continue'].disabled);assert(nodes['task-freshness'].textContent.includes('12 秒'));
markTaskStatus(data);updateTaskButtons();assert(!nodes['task-continue'].disabled);
assert.equal(nodes['task-freshness'].textContent,'');
''')

    def test_lost_continue_reply_never_resends_and_reconciles_new_command(self):
        self.browser('''
let post=0;const data={task_id:'t',state:'recovery_required',terminal:false,can_resume:true,control_step_id:'S',control_command_id:'old'};
displayedTask=data;taskStatusKnown=true;
global.fetch=async(url,options)=>{
 if(options.method==='POST'){post++;throw new Error('reply lost');}
 return {ok:true,json:async()=>data};
};
await controlReception('continue');assert.equal(post,1);
assert(pendingTaskControl);assert(nodes['task-continue'].disabled);
assert(nodes['task-command-feedback'].textContent.includes('结果待确认'));
await controlReception('continue');assert.equal(post,1);
markTaskStatus({...data,control_command_id:'new',state:'running'});
assert.equal(pendingTaskControl,null);
assert(nodes['task-command-feedback'].textContent.includes('新命令：new'));
''', control=True)

    def test_explicit_rejection_allows_review_but_does_not_retry(self):
        self.browser('''
let post=0;const data={task_id:'t',state:'recovery_required',terminal:false,can_resume:true,control_step_id:'S',control_command_id:'old'};
displayedTask=data;taskStatusKnown=true;
global.fetch=async(url,options)=>options.method==='POST'
 ? (post++,{ok:false,status:409,json:async()=>({error:'当前命令已变化'})})
 : {ok:true,json:async()=>data};
await controlReception('continue');assert.equal(post,1);
assert.equal(pendingTaskControl,null);assert(!nodes['task-continue'].disabled);
assert(nodes['task-command-feedback'].textContent.includes('当前命令已变化'));
''', control=True)

    def test_projection_distinguishes_prepare_intent_unknown_and_rejection(self):
        record={'task_id':'t','state':'running','phase_specs':[{'step_id':'S','kind':'navigate'}],
                'steps':{'S':{'attempts':[{'command_id':'n','submitted':False}]}}}
        a=record['steps']['S']['attempts'][0]
        self.assertEqual(status_from_record(record)['current_command']['stage'], 'prepared')
        a['submitted']=True
        self.assertEqual(status_from_record(record)['current_command']['stage'], 'submitted')
        record['command_unknown']=True
        record['state']='recovery_required'
        self.assertEqual(status_from_record(record)['current_command']['stage'], 'unknown')
        a.update(verdict='FAIL',progress={'evidence':{'submission_rejected':{'message':'refused','code':'INVALID_REQUEST'}}})
        s=status_from_record(record)
        self.assertEqual(s['current_command']['stage'],'rejected')
        self.assertEqual(s['failure_history'][0]['kind'],'rejected')

    def test_report_keeps_failure_unknown_skip_and_mock_scope(self):
        status={'task_id':'t','state':'recovery_required','active':True,'completed':2,'total':11,
                'execution_backend':'reception_real_sim_manipulation','command_unknown':True,
                'control_step_id':'NAVIGATING_TO_TABLE2','control_command_id':'n',
                'attempt_summary':{'retry_count':1,'failure_count':1},'manual_skips':['s'],
                'failure_history':[{'step_id':'NAVIGATING_TO_TABLE2','command_id':'old','kind':'execution_failed',
                                    'message':'original English error'}]}
        r=reception_report(status)
        self.assertFalse(r['verdict']);self.assertIn('original English error',r['summary_text'])
        self.assertIn('模拟',r['summary_text']);self.assertEqual(r['unresolved'][0]['command_id'],'n')
        status.update(state='succeeded',active=False,command_unknown=False,manual_skips=[],completed=11)
        r=reception_report(status)
        self.assertTrue(r['verdict']);self.assertEqual(len(r['failure_history']),1);self.assertFalse(r['unresolved'])
        status.update(state='cancelled')
        self.assertIn('不代表',reception_report(status)['summary_text'])
