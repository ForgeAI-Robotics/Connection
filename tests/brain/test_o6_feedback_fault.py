"""Optional O6 diagnostics remain identity-bound and cannot change execution proof."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from contracts.vla_direct import o6_feedback_fault
from tests.brain import test_vla_terminal_sync as sync_fixture
from brain.app import create_runtime


def example():
    return json.loads(Path('tests/contracts/o6_feedback_fault.example.json').read_text())['response_excerpt']


def extract(raw):
    return o6_feedback_fault(raw,task_id='example-task',command_id='example-open-fridge')


class O6FaultContractTests(unittest.TestCase):
    def test_both_compatible_locations_and_null_age_are_preserved(self):
        for source in ('both','result','error'):
            with self.subTest(source=source):
                raw=example()
                if source=='result':raw.pop('error')
                if source=='error':raw.pop('result')
                fault=extract(raw)
                self.assertEqual(fault['side'],'right');self.assertEqual(fault['can_id'],39)
                self.assertEqual(fault['feedback_at_fault']['right']['age_ms'],234.)
                self.assertEqual(fault['first_fault_at'],'2026-10-29T11:00:00+00:00')
                fault['side']='left'
                self.assertEqual(extract(raw)['side'],'right')
        raw=example()
        for f in (raw['result']['feedback_fault'],raw['error']['details']['feedback_fault']):
            f['last_valid_feedback_age_ms']=None;f['code']='CAN_READ_FAILED';f['errno']=100
        self.assertIsNone(extract(raw)['last_valid_feedback_age_ms'])
        self.assertEqual(extract(raw)['errno'],100)

    def test_old_absent_malformed_foreign_and_conflicting_faults_stay_unprojected(self):
        cases=[{},None,{'task_id':'example-task','command_id':'example-open-fridge','result':{'feedback_fault':[]}}]
        for key,value in (('schema','future/v2'),('task_id','other'),('command_id','old')):
            raw=example()
            for f in (raw['result']['feedback_fault'],raw['error']['details']['feedback_fault']):f[key]=value
            cases.append(raw)
        raw=example();raw['result']['feedback_fault']['fault_id']='different';cases.append(raw)
        raw=example();raw['task_id']='other';cases.append(raw)
        for raw in cases:
            with self.subTest(raw=raw):
                before=deepcopy(raw);self.assertIsNone(extract(raw));self.assertEqual(raw,before)

    def test_original_instance_session_and_robot_must_match_when_available(self):
        for key in ('receiver_instance_id','input_session_id','robot_serial'):
            with self.subTest(key=key):
                raw=example();value=raw['result']['feedback_fault'][key]
                raw['handoff']={'execution':{key:value}}
                self.assertIsNotNone(extract(raw))
                raw['handoff']['execution'][key]='other-instance'
                self.assertIsNone(extract(raw))

    def test_cleanup_fallback_is_diagnostic_only(self):
        raw=example();fault=raw['result'].pop('feedback_fault');raw['error']['details'].pop('feedback_fault')
        raw['exit_state']={'runtime':{'feedback_fault':fault}}
        self.assertEqual(extract(raw),fault)


class O6FaultRuntimeTests(unittest.TestCase):
    setUp=sync_fixture.VlaTerminalSyncTests.setUp
    tearDown=sync_fixture.VlaTerminalSyncTests.tearDown
    service=sync_fixture.VlaTerminalSyncTests.service
    terminal=sync_fixture.VlaTerminalSyncTests.terminal

    def test_failure_sync_history_and_restart_preserve_fault_without_certifying_stop(self):
        svc=self.service();rt=svc.runtime;cid=self.terminal(svc)
        fault=example()['result']['feedback_fault'];fault.update(task_id='test-task',command_id=cid)
        raw=self.vla.commands[cid]
        raw['result']['feedback_fault']=deepcopy(fault)
        raw['error']['details']={'feedback_fault':deepcopy(fault)}
        raw['exit_state']['body_stopped']=False;raw['result']['body_stopped']=False
        rt.observe_waiting_vla()
        status=rt.public_status()
        self.assertEqual(status['failure_detail']['feedback_fault'],fault)
        self.assertEqual(status['failure_history'][-1]['feedback_fault'],fault)
        self.assertEqual(status['failure_detail']['downstream']['error']['details']['feedback_fault'],fault)
        self.assertEqual(status['completed'],3)
        self.assertFalse(rt.record['stopped_confirmed']);self.assertFalse(rt.record['resources_cleared'])
        restored=create_runtime(self.config,self.port)
        self.assertEqual(restored.public_status()['failure_detail']['feedback_fault'],fault)
        restored.request_cancel()
        self.assertEqual(restored.public_status()['failure_history'][-1]['feedback_fault'],fault)
        self.assertEqual(self.vla.finish_posts,0)
        self.assertEqual(len(self.vla.posts),1)

    def test_old_failure_does_not_invent_first_fault(self):
        svc=self.service();self.terminal(svc);svc.runtime.observe_waiting_vla()
        self.assertNotIn('feedback_fault',svc.runtime.public_status()['failure_detail'])
        self.assertNotIn('feedback_fault',svc.runtime.public_status()['failure_history'][-1])


@unittest.skipUnless(shutil.which('node'),'Node required for diagnostic rendering')
class O6FaultPageTests(unittest.TestCase):
    def test_first_fault_display_keeps_units_nulls_and_escapes_peer_text(self):
        page=Path('src/entries/web/templates/index.html').read_text()
        source=page[page.index('        function taskText'):page.index('        function renderAttemptHistory')]
        source+=page[page.index('        function renderFeedbackFault'):page.index('        function recoveryAdviceDetails')]
        code=r'''
const assert=require('assert'),vm=require('vm'),fs=require('fs');
const data=JSON.parse(fs.readFileSync(0,'utf8'));
const ctx={document:{createElement:()=>({textContent:'',get innerHTML(){return this.textContent.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}})}};
vm.createContext(ctx);vm.runInContext(data.source,ctx);
const f=data.fault;let html=ctx.renderFeedbackFault(f,f.command_id);
for (const s of ['右手','can1','0x27','19:00:00','200.0 ms','211.0 ms','234.0 ms','总耗时包含','example-o6-instance'])assert.ok(html.includes(s),s);
assert.equal(ctx.renderFeedbackFault(undefined,f.command_id),'');
assert.equal(ctx.renderFeedbackFault(f,'other-command'),'');
f.message='<img src=x onerror=alert(1)>';f.elapsed_ms=null;f.last_valid_feedback_age_ms=null;f.errno=100;f.code='CAN_READ_FAILED';
html=ctx.renderFeedbackFault(f,f.command_id);assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;img'));
assert.ok(html.includes('总耗时：未提供'));assert.ok(html.includes('反馈年龄：未提供'));assert.ok(html.includes('errno=100'));
'''
        result=subprocess.run(['node','-e',code],input=json.dumps({'source':source,'fault':example()['result']['feedback_fault']}),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
