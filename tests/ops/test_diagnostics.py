import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from ops.app import create_app


class DiagnosticApiTests(unittest.TestCase):
    def setUp(self):
        self.scratch=tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.env=patch.dict(os.environ,{'FQPLANNER_LOG_ROOT':self.scratch.name})
        self.env.start();self.addCleanup(self.env.stop)
        self.app=create_app();self.client=self.app.test_client()
        for worker in self.app.extensions['workers']:self.addCleanup(worker.shutdown)
        p=Path(self.scratch.name)/'2026-10-31/master/brain.jsonl'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'task_id':'test-task','cmd':'test-command','event':'failure'})+'\n')

    def test_query_and_export_are_read_only_and_no_cache(self):
        with patch('ops.app._service_status',side_effect=AssertionError('no live probe')):
            response=self.client.get('/api/brain/diagnostics?task_id=test-task')
            export=self.client.get('/api/brain/diagnostics/export?command_id=test-command')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(response.get_json()['coverage']['returned'],1)
        self.assertTrue(export.headers['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(export.get_json()['records'][0]['identity']['task_id'],'test-task')
        self.assertEqual(self.client.post('/api/brain/diagnostics').status_code,405)

    def test_path_injection_and_duplicate_filters_rejected(self):
        for suffix in ('?root=/etc','?attachment=/etc/passwd','?peer_events=x',
                       '?task_id=a&task_id=b','?limit=900000','?task_id=../x',
                       '?date_from=2026-02-30'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.get('/api/brain/diagnostics'+suffix).status_code,400)

    def test_concurrent_queries_bounded_without_blocking_live_logs(self):
        entered=threading.Barrier(3);release=threading.Event()
        def slow(**kwargs):
            entered.wait(timeout=3);release.wait(3)
            return {'coverage':{'returned':0}}
        def request_query():
            with self.app.test_client() as client:return client.get('/api/brain/diagnostics').status_code
        with patch('brain.observability.diagnostics.query_diagnostics',side_effect=slow), ThreadPoolExecutor(2) as pool:
            tasks=[pool.submit(request_query) for _ in range(2)]
            try:
                entered.wait(timeout=3)
                self.assertEqual(self.client.get('/api/brain/diagnostics').status_code,429)
                self.assertEqual(self.client.get('/api/services/master/logs').status_code,200)
            finally:release.set()
            self.assertEqual([task.result(timeout=3) for task in tasks],[200,200])

    def test_default_view_still_has_live_log_and_separate_history_search(self):
        page=self.client.get('/').get_data(as_text=True)
        self.assertIn('id="log"',page)
        self.assertIn('id="diagnostic-search"',page)
        self.assertIn('结果不代表当前任务或机器人状态',page)


class DiagnosticBrowserTests(unittest.TestCase):
    def test_input_changes_discard_old_result_and_export_uses_reviewed_snapshot(self):
        source=Path('src/ops/static/panel.js').read_text()
        source=source[source.index('function installDiagnosticSearch()'):source.index('document.querySelectorAll(".layer").forEach',source.index('function installDiagnosticSearch()'))]
        script=r'''
const assert=require('node:assert/strict');
const nodes={};
function $(id){return nodes[id]||=( {value:'',textContent:'',disabled:false,events:{},addEventListener(k,f){this.events[k]=f;}} );}
const requests=[], blobs=[], anchors=[];
const document={createElement(){const a={click(){this.clicked=true;}};anchors.push(a);return a;}};
URL.createObjectURL=(blob)=>{blobs.push(blob);return 'blob:local';};URL.revokeObjectURL=()=>{};
const timers=new Map();let next=0;
function setTimeout(fn,ms){timers.set(++next,fn);return next;}function clearTimeout(id){timers.delete(id);}
function fetch(url,options){return new Promise(resolve=>requests.push({url,options,resolve}));}
'''+source+r'''
(async()=>{
 const form=$('diagnostic-form'),submit=form.events.submit,ev={preventDefault(){}};
 await submit(ev);assert.equal(requests.length,0);
 $('diagnostic-task').value='old-task';
 const first=submit(ev);assert.equal(requests.length,1);assert($('diagnostic-export').disabled);
 $('diagnostic-task').value='new-task';form.events.input();
 requests[0].resolve({ok:true,json:async()=>({coverage:{returned:1},records:[{event:{task_id:'old-task'}}]})});await first;
 assert($('diagnostic-export').disabled);assert($('diagnostic-status').textContent.includes('条件已变化'));
 const second=submit(ev);
 const body={coverage:{returned:1,incomplete:true,issues:{partial_line_skipped:1}},records:[{event:{message:'<img onerror=alert(1)>'}}],missing_evidence:[{service:'nav'}]};
 requests[1].resolve({ok:true,json:async()=>body});await second;
 assert.equal(requests[1].options.cache,'no-store');assert.equal(requests.length,2);
 assert($('diagnostic-results').textContent.includes('<img'));assert.equal($('diagnostic-results').innerHTML,undefined);
 assert($('diagnostic-status').textContent.includes('扫描不完整'));assert($('diagnostic-status').textContent.includes('nav'));
 $('diagnostic-export').events.click();assert.equal(requests.length,2);assert(anchors[0].clicked);
 assert.deepEqual(JSON.parse(await blobs[0].text()),body);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
