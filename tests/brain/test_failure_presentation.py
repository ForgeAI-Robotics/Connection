"""Failure receipts, recovery safety, and actual browser rendering regressions."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from brain.kernel.runtime import status_from_record
from shared.task_text import failure_text, presentation_labels
from tests.brain.test_kernel_reception import FakeBody, _runtime


class FailurePresentationTests(unittest.TestCase):
    def test_buttons_follow_status_and_cancel_task_label(self):
        page = Path('src/entries/web/templates/index.html').read_text()
        for name in ('start', 'start-nav', 'skip', 'pause', 'continue', 'cancel'):
            self.assertIn(f'id="task-{name}" disabled', page)
        self.assertIn('>取消任务</button>', page)
        code = page[page.index('        let taskSubmitting'):page.index('        // 发布任务')]
        script = '''const assert=require('assert'); const nodes={};
const document={getElementById(id){return nodes[id] ||= {}}};
''' + code + '''
updateTaskButtons();
assert(nodes['task-start'].disabled && nodes['task-cancel'].disabled);
taskStatusKnown=true; displayedTask={state:'cancelled',terminal:true,task_id:'t',blocks_new_motion:false};
updateTaskButtons();
assert(!nodes['task-start'].disabled && nodes['task-cancel'].disabled && nodes['task-continue'].disabled);
displayedTask={state:'running',terminal:false,task_id:'t',can_skip:true,blocks_new_motion:true};
updateTaskButtons();
assert(nodes['task-start'].disabled && !nodes['task-pause'].disabled && !nodes['task-skip'].disabled);
assert(!nodes['task-cancel'].disabled && nodes['task-continue'].disabled);
displayedTask={...displayedTask,state:'paused',can_resume:true}; updateTaskButtons();
assert(nodes['task-pause'].disabled && !nodes['task-continue'].disabled);
taskControlPending=true; updateTaskButtons();
assert(Object.values(nodes).every(n=>n.disabled));
taskControlPending=false; taskStatusKnown=false; updateTaskButtons();
assert(Object.values(nodes).every(n=>n.disabled));
'''
        subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)

    def test_known_failure_does_not_release_unconfirmed_resources(self):
        for released, stopped in ((True, True), (False, True), (True, False)):
            with self.subTest(released=released, stopped=stopped), tempfile.TemporaryDirectory() as root:
                port = FakeBody()
                port.outcomes['NAVIGATING_TO_TABLE2'] = dict(
                    terminal='failed', stopped=stopped, resources_released=released,
                    evidence={'identity_ok': True, 'time_ok': True})
                runtime = _runtime(root, port)
                runtime.drive()
                self.assertEqual(runtime.state, 'recovery_required')
                self.assertFalse(runtime.record['command_unknown'])
                self.assertEqual(runtime.record['stopped_confirmed'], stopped)
                self.assertEqual(runtime.record['resources_cleared'], released and stopped)
                original = runtime.record['open_command_id']
                self.assertTrue(original)
                if not (released and stopped):
                    from contracts.tasks import Rejected
                    with self.assertRaises(Rejected):
                        runtime.continue_current()
                    self.assertEqual(port.submits, [original])

    def test_uncertain_receipts_remain_unknown(self):
        for change in ({'timed_out': True}, {'evidence': {'identity_ok': False, 'time_ok': True}},
                       {'evidence': {'identity_ok': True, 'time_ok': False}}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                port = FakeBody()
                port.outcomes['NAVIGATING_TO_TABLE2'] = dict(
                    terminal='failed', stopped=True, resources_released=True,
                    evidence={'identity_ok': True, 'time_ok': True})
                port.outcomes['NAVIGATING_TO_TABLE2'].update(change)
                runtime = _runtime(root, port)
                runtime.drive()
                self.assertTrue(runtime.record['command_unknown'])
                self.assertFalse(runtime.record['resources_cleared'])

    def fixture(self):
        message = '9882 rejected /set-agent-world-goal: HTTP 400: {"error": "localization pose is not in inflated known-free space"}'
        bad = {'submitted': True, 'command_id': 'nav-original', 'verdict': 'FAIL', 'outcome': 'failed',
               'progress': {'terminal': 'failed', 'evidence': {'downstream': {'error': {
                   'code': 'NAVIGATION_NO_PATH', 'message': message}}}}}
        good = {'submitted': True, 'authorized_by': 'human_continue', 'command_id': 'nav-retry',
                'verdict': 'PASS', 'outcome': 'succeeded'}
        return {'state': 'succeeded', 'task_id': 't', 'cursor': 1,
                'phase_specs': [{'step_id': 'NAVIGATING_TO_TABLE2', 'kind': 'navigate'}],
                'steps': {'NAVIGATING_TO_TABLE2': {'attempts': [bad, good]}}}

    def test_success_retains_failures_without_marking_final_result_failed(self):
        status = status_from_record(self.fixture())
        self.assertTrue(status['all_done'])
        self.assertFalse(status['failed'])
        self.assertEqual(status['attempt_summary'], {'retry_count': 1, 'manual_retry_count': 1, 'failure_count': 1})
        self.assertIn('当前定位点', status['failure_history'][0]['display_message'])
        self.assertEqual(status['failure_history'][0]['command_id'], 'nav-original')

    def test_report_api_preserves_history_and_final_success(self):
        from unittest.mock import Mock
        from brain.api.app import create_app
        status = status_from_record(dict(self.fixture(), package='reception'))
        facade = Mock()
        facade.get_task_status.return_value = status
        report = create_app(Mock(), facade=facade).test_client().get('/api/reception/report').json['report']
        self.assertTrue(report['verdict'])
        self.assertEqual(report['attempt_summary']['failure_count'], 1)
        self.assertEqual(report['failure_history'], status['failure_history'])

    def test_translations_preserve_original_and_distinguish_start_and_goal(self):
        for raw, chinese in [('localization pose is not in inflated known-free space', '当前定位点'),
                             ('goal is not in inflated known-free space', '目标点'),
                             ('position_hard_failure', '末端位置'),
                             ('no_reliable_predictive_retry_window', '重试空间'),
                             ('terminal pose adjustment made no progress for 35.0s; navigation_state=HOLD_ARRIVAL_DWELL', '持续无进展')]:
            text = failure_text(raw)
            self.assertIn(chinese, text)
            self.assertIn(raw, text)
        self.assertIn('unseen_error', failure_text('unseen_error'))

    def test_leg_order_translation_uses_actual_prerequisite(self):
        for before, after, expected in [
            ('table2', 'relay2', '先成功到达2 号桌，才能前往中转点 2'),
            ('relay2', 'relay3', '先成功到达中转点 2，才能前往中转点 3'),
            ('relay3', 'table1', '先成功到达中转点 3，才能前往1 号桌'),
        ]:
            raw = f'{before} navigation must succeed under the same task_id before {after}'
            text = failure_text(raw, 'INVALID_LEG_ORDER')
            self.assertIn(expected, text)
            self.assertIn(raw, text)
        for raw in ('new order constraint', None):
            text = failure_text(raw, 'INVALID_LEG_ORDER')
            self.assertIn('具体前置步骤见原始错误', text)
            self.assertNotIn('中转点 3', text)

    def test_browser_omits_analysis_and_escapes_failure_history(self):
        page = Path('src/entries/web/templates/index.html').read_text()
        functions = page[page.index('        function renderAttemptHistory'):page.index('        let _prevAllDone')]
        reasons = page[page.index('        function taskReason'):page.index('        function showTaskResponse')]
        fixture = self.fixture()
        status = status_from_record(fixture)
        status.update(active=True, state='recovery_required', phase='NAVIGATING_TO_TABLE2',
                      recovery_advice={'applicable': True, 'summary': 'MODEL_ANALYSIS', 'action': 'request_help'},
                      reasoning='MODEL_THINKING')
        status['failure_detail'] = dict(status['failure_history'][0])
        status['failure_history'][0]['command_id'] = '<script>bad</script>'
        script = 'const TASK_LABELS=' + json.dumps(presentation_labels()) + ';\n'
        script += '''let displayedTask, taskReceiptId=null, taskControlPending=false, taskSubmitting=false; const nodes={};
const document={getElementById(id){return nodes[id] ||= {style:{},innerHTML:''}}};
function updateTaskButtons(){}
function taskText(x){return String(x ?? '').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
'''
        script += reasons + functions + '\nrenderTaskProgress(' + json.dumps(status) + ');console.log(nodes["task-progress"].innerHTML);'
        result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True).stdout
        for unwanted in ('MODEL_ANALYSIS', 'MODEL_THINKING', '分析：', '建议：', '<script>bad'):
            self.assertNotIn(unwanted, result)
        for wanted in ('当前定位点', 'localization pose', '历史失败 1 次', '人工继续触发 1 次', '&lt;script&gt;'):
            self.assertIn(wanted, result)

    def test_browser_progress_and_receipt_follow_rejection_retry_and_finish(self):
        page = Path('src/entries/web/templates/index.html').read_text()
        functions = page[page.index('        function renderAttemptHistory'):page.index('        let _prevAllDone')]
        reasons = page[page.index('        function taskReason'):page.index('        function showTaskResponse')]
        script = 'const TASK_LABELS=' + json.dumps(presentation_labels()) + ';\n'
        script += '''const assert=require('assert');
let displayedTask, taskReceiptId='round2', taskControlPending=false, taskSubmitting=false;
const nodes={};
const document={getElementById(id){return nodes[id] ||= {style:{},innerHTML:'',textContent:''}}};
function updateTaskButtons(){}
function taskText(x){return String(x ?? '').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
'''
        script += reasons + functions + '''
const data={task_id:'round2',active:true,state:'recovery_required',completed:3,total:6,
phase:'NAVIGATING_TO_RELAY2',failure_detail:{kind:'rejected',code:'INVALID_LEG_ORDER',display_message:'同一任务先到2号桌'},
subtask_list:[{done:true,status:'success'},{done:true,status:'success'},
{done:true,status:'skipped'},{done:false,status:'failure'},{done:false},{done:false}]};
nodes['task-result']={style:{},textContent:'当前环节的新尝试已准备'};
renderTaskProgress(data);
assert(nodes['task-progress'].innerHTML.includes('成功 2/6 · 跳过 1 · 未完成 3'));
assert(nodes['task-progress'].innerHTML.includes('width:33%'));
assert(nodes['task-progress'].innerHTML.includes('✕'));
assert(nodes['task-result'].textContent.includes('指令被拒绝，未开始执行'));
assert(!nodes['task-result'].textContent.includes('新尝试已准备'));
data.state='running'; data.failure_detail=null;
renderTaskProgress(data);
assert(nodes['task-result'].textContent.includes('执行中'));
assert(!nodes['task-result'].textContent.includes('同一任务先到2号桌'));
data.state='failed'; data.flow_finished=true;
renderTaskProgress(data);
assert(nodes['task-result'].textContent.includes('含人工跳过，未全部成功'));
data.state='succeeded'; data.all_done=true;
data.subtask_list=Array.from({length:6},()=>({done:true,status:'success'}));
renderTaskProgress(data);
assert(nodes['task-progress'].innerHTML.includes('成功 6/6 · 跳过 0 · 未完成 0'));
assert(nodes['task-result'].textContent.includes('任务成功'));
taskControlPending=true; nodes['task-result'].textContent='正在发送控制请求...';
renderTaskProgress(data);
assert.equal(nodes['task-result'].textContent,'正在发送控制请求...');
taskControlPending=false; data.task_id='another-task';
renderTaskProgress(data);
assert.equal(nodes['task-result'].style.display,'none');
'''
        subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
