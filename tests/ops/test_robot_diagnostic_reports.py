import json
from pathlib import Path
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from ops.app import create_app
from ops.diagnostic_reports import task_report
from tests.brain.test_task_diagnostic_reports import report_view


class RobotReportProxyTests(unittest.TestCase):
    def response(self, payload, status=200):
        response=Mock(status_code=status)
        response.iter_content.return_value=[json.dumps(payload).encode()]
        return response

    def test_proxy_uses_configured_brain_and_only_bounded_get(self):
        response=self.response(report_view())
        with patch('ops.diagnostic_reports.entry_url', return_value='http://brain.invalid:5000') as resolver, \
             patch('ops.diagnostic_reports.BrainClient') as factory:
            factory.return_value.request.return_value=response
            value,status=task_report('task-001',True)
        self.assertEqual(status,200)
        self.assertEqual(value['task_id'],'task-001')
        resolver.assert_called_once_with('MASTER_URL')
        factory.assert_called_once_with('http://brain.invalid:5000',timeout=6,source='ops')
        args,kwargs=factory.return_value.request.call_args
        self.assertEqual(args,('GET','/api/task_diagnostics'))
        self.assertEqual(kwargs['params'],{'task_id':'task-001','refresh':'1'})
        self.assertFalse(kwargs['allow_redirects']);self.assertTrue(kwargs['stream'])
        response.close.assert_called_once()

    def test_foreign_oversize_redirect_and_offline_are_not_displayed_as_reports(self):
        foreign=report_view('other-task')
        huge=self.response(report_view());huge.iter_content.return_value=[b'x'*300000]
        for response,expected in [(self.response(foreign),502),(huge,502),(self.response({},302),502)]:
            with self.subTest(expected=expected), patch('ops.diagnostic_reports.entry_url',return_value='http://brain.invalid'), \
                 patch('ops.diagnostic_reports.BrainClient') as factory:
                factory.return_value.request.return_value=response
                value,status=task_report('task-001')
                self.assertEqual(status,expected);self.assertIsNone(value['report'])
        with patch('ops.diagnostic_reports.entry_url',side_effect=RuntimeError('offline')):
            value,status=task_report('task-001')
        self.assertEqual(status,503)
        self.assertEqual(value['error']['code'],'BRAIN_REPORT_UNAVAILABLE')


class RobotReportApiTests(unittest.TestCase):
    def setUp(self):
        self.app=create_app();self.client=self.app.test_client()
        for worker in self.app.extensions['workers']:self.addCleanup(worker.shutdown)

    def test_filtering_and_distinct_missing_status(self):
        with patch('ops.diagnostic_reports.task_report',return_value=(report_view(report=None,tracking='not_tracked'),404)) as proxy:
            response=self.client.get('/api/brain/task_diagnostics?task_id=task-001')
            self.assertEqual(response.status_code,404)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            proxy.assert_called_once_with('task-001',False)
            for query in ('','?task_id=x&action=begin','?task_id=x&task_id=y','?task_id=x&refresh=3'):
                self.assertEqual(self.client.get('/api/brain/task_diagnostics'+query).status_code,400)
            self.assertEqual(proxy.call_count,1)

    def test_proxy_concurrency_does_not_block_archive_queries(self):
        entered=threading.Barrier(3);release=threading.Event()
        def slow(*args):
            entered.wait(timeout=3);release.wait(3);return report_view(),200
        def query():
            with self.app.test_client() as client:return client.get('/api/brain/task_diagnostics?task_id=task-001').status_code
        with patch('ops.diagnostic_reports.task_report',side_effect=slow), ThreadPoolExecutor(2) as pool:
            jobs=[pool.submit(query) for _ in range(2)]
            try:
                entered.wait(timeout=3)
                self.assertEqual(self.client.get('/api/brain/task_diagnostics?task_id=task-001').status_code,429)
                with patch('brain.observability.diagnostics.query_diagnostics',return_value={'records':[]}):
                    self.assertEqual(self.client.get('/api/brain/diagnostics?task_id=x').status_code,200)
            finally:release.set()
            self.assertEqual([j.result(3) for j in jobs],[200,200])


class RobotReportBrowserTests(unittest.TestCase):
    def test_report_rendering_stale_task_and_bounded_get_polling(self):
        source=Path('src/ops/static/panel.js').read_text()
        source=source[source.index('function renderRobotReport('):source.index('installDiagnosticSearch();')]
        setup=r'''
const assert=require('node:assert/strict');
const nodes={};
function node(){return {value:'',textContent:'',disabled:false,children:[],events:{},addEventListener(k,f){this.events[k]=f;},replaceChildren(...c){this.children=c;},appendChild(c){this.children.push(c);}};}
function $(id){return nodes[id]||=(node());}
const document={createElement:node};
const requests=[],timers=new Map();let next=0;
function setTimeout(fn,ms){timers.set(++next,{fn,ms});return next;}function clearTimeout(id){timers.delete(id);}
function fetch(url,options){return new Promise((resolve,reject)=>{requests.push({url,options,resolve,reject});options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')));});}
async function flush(){for(let n=0;n<12;n++)await Promise.resolve();}
function answer(call,body,status=200){call.resolve({ok:status<400,status,json:async()=>body});}
function body(id,refreshing=false){return {enabled:true,task_id:id,run_id:'diag-'+id,tracking:'finished',refreshing,report:{task_id:id,status:'complete',quality:{missing_evidence:['nav_events_missing'],loss_visibility:'unknown'},findings:[{code:'HINT',assessment:'suspected',message:'<img src=x onerror=alert(1)>',evidence_ids:['e1']}],evidence_refs:[{evidence_id:'e1',available:false,reference:'file:///private/bag'}]},error:null,incomplete_reasons:[]};}
'''
        script=setup+source+r'''
installRobotReport();
(async()=>{
 const task=$('robot-report-task'),form=$('robot-report-form'),ev={preventDefault(){}};
 form.events.submit(ev);assert.equal(requests.length,0);
 task.value='old-task';form.events.submit(ev);form.events.submit(ev);assert.equal(requests.length,1);
 task.value='new-task';task.events.input();assert(requests[0].options.signal.aborted);
 form.events.submit(ev);assert.equal(requests.length,2);
 answer(requests[1],body('new-task'));await flush();answer(requests[0],body('old-task'));await flush();
 assert($('robot-report-summary').textContent.includes('new-task'));assert(!$('robot-report-summary').textContent.includes('old-task'));
 assert($('robot-report-status').textContent.includes('报告已生成'));assert(!$('robot-report-status').textContent.includes('机器人成功'));
 assert($('robot-report-summary').textContent.includes('nav_events_missing'));
 const lines=$('robot-report-findings').children.flatMap(n=>n.children).map(n=>n.textContent);
 assert(lines.some(x=>x.includes('<img')));assert.equal($('robot-report-findings').innerHTML,undefined);
 $('robot-report-refresh').events.click();assert(requests[2].url.includes('refresh=1'));
 answer(requests[2],body('new-task',true));await flush();
 const delay=[...timers].find(([id,v])=>v.ms===1000);timers.delete(delay[0]);delay[1].fn();await flush();
 assert.equal(requests.length,4);assert(requests[3].url.includes('refresh=0'));
 const cached=body('new-task');cached.error={code:'OFFLINE',message:'gateway unavailable'};
 answer(requests[3],cached);await flush();assert($('robot-report-status').textContent.includes('缓存报告'));
 assert(requests.every(r=>r.options.method==='GET'));
 task.value='another-task';task.events.input();form.events.submit(ev);
 answer(requests[4],body('foreign-task'));await flush();assert($('robot-report-status').textContent.includes('身份不匹配'));
 assert.equal($('robot-report-summary').textContent,'');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
