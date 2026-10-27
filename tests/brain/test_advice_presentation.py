import json
from pathlib import Path
import subprocess
import unittest

from shared.task_text import presentation_labels


class AdvicePresentationTests(unittest.TestCase):
    def run_browser(self, script):
        page = Path('src/entries/web/templates/index.html').read_text()
        code = page[page.index('        function renderAttemptHistory'):page.index("        let reportContext =")]
        setup = '''const assert=require('assert'); let taskControlPending=false, pendingTaskControl=null;
let displayedTask, taskReceiptId=null, taskNoticeContext=null, taskSubmitting=false;
const nodes={}; const document={getElementById(id){return nodes[id] ||= {style:{},innerHTML:'',textContent:''}}};
function updateTaskButtons(){}
function taskPhase(x){return TASK_LABELS.phases[x] || x;}
function taskReason(x){return x;}
function taskText(x){return String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
'''
        fixture = {'task_id': 't', 'terminal': False, 'active': True, 'execution_backend': 'reception_real',
                   'control_step_id': 'NAVIGATING_TO_TABLE1', 'control_command_id': 'nav-1',
                   'recovery_confirmation_enabled': True, 'state': 'recovery_required',
                   'failure_detail': {'message': 'terminal correction has a live nearby obstacle; holding stand',
                                      'code': 'NAVIGATION_TERMINAL_UNREACHABLE'},
                   'recovery_advice': {'task_id': 't', 'step_id': 'NAVIGATING_TO_TABLE1',
                                      'control_command_id': 'nav-1', 'advice_id': 'abc',
                                      'action': 'request_help', 'source': 'llm', 'applicable': True,
                                      'summary': '<img src=x onerror=alert(1)>', 'reason': 'LONG_MODEL_REASON'}}
        result = subprocess.run(['node', '-e', setup + '\nconst TASK_LABELS=' + json.dumps(presentation_labels())
                                 + ';\n' + code + '\nconst data=' + json.dumps(fixture) + ';\n' + script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compact_failure_and_escaped_details(self):
        self.run_browser('''
const html=renderRecoveryAdvice(data), visible=html.split('<details')[0];
for(const text of ['失败环节','导航到 1 号桌','失败原文','回包说明','附近障碍','建议','请现场人员检查并处理。']) assert(visible.includes(text), text);
for(const text of ['LONG_MODEL_REASON','来源','原命令结果是否未知']) assert(!visible.includes(text));
assert(html.includes('&lt;img')); assert(!html.includes('<img')); assert(!html.includes('disabled'));
renderTaskProgress(data);
assert(nodes['task-progress'].innerHTML.includes('任务 ID：t'));
assert(nodes['task-progress'].innerHTML.includes('任务环境：真机接待'));
data.recovery_confirmation_enabled=false;
assert(renderRecoveryAdvice(data).includes('失败原文'));
assert(!renderRecoveryAdvice(data).includes('onclick'));
''')

    def test_running_without_dispatch_is_not_shown_as_executing(self):
        self.run_browser('''
data.state='running';data.failure_detail=null;data.current_command={stage:'prepared',command_id:null};
renderTaskProgress(data);assert(nodes['task-progress'].innerHTML.includes('等待下发当前步骤'));
assert(!nodes['task-progress'].innerHTML.includes('执行中...'));
data.current_command={stage:'submitted',command_id:'nav-new'};
renderTaskProgress(data);assert(nodes['task-progress'].innerHTML.includes('等待动作执行结果'));
data.failure_history=[{step_id:'NAVIGATING_TO_TABLE2',message:'9882 rejected: terminal correction scan coverage is insufficient; largest_gap_deg=45.5'}];
data.attempt_summary={failure_count:1};
const history=renderAttemptHistory(data);
assert(history.includes('末端修正受阻：扫描覆盖不足'));
assert(history.includes('largest_gap_deg=45.5'));
''')

    def test_unknown_peer_fields_render_with_or_without_advice_and_escape_html(self):
        self.run_browser('''
data.failure_detail={code:'NAV_FUTURE_FAULT',message:'new reason <script>bad</script>',
 display_message:'回包：距目标 12.0 cm；原始错误：new reason',
 downstream:{state:'failed',error:{code:'NAV_FUTURE_FAULT',details:{new_sensor:{hint:'<img src=x>',value:42}}},
 result:{navigation_stopped:false,next_command_allowed:false,new_flag:'keep-me'}}};
const original=JSON.stringify(data);
let html=renderRecoveryAdvice(data);
for (const text of ['NAV_FUTURE_FAULT','12.0 cm','下游原始错误与结果','new_sensor','navigation_stopped','keep-me','&lt;img']) assert(html.includes(text),text);
assert(!html.includes('<script>bad'));assert(!html.includes('<img src=x>'));
assert.equal(JSON.stringify(data),original);
data.recovery_confirmation_enabled=false;
html=renderRecoveryAdvice(data);assert(html.includes('new_sensor'));assert(!html.includes('onclick'));
data.failure_history=[{...data.failure_detail,step_id:'NAVIGATING_TO_TABLE1',command_id:'old-cmd'}];
data.attempt_summary={failure_count:1};
html=renderAttemptHistory(data);assert(html.includes('new_sensor'));assert(html.includes('old-cmd'));assert(!html.includes('<img'));
// A legacy phrase match must not hide the server's numeric evidence.
data.failure_detail.message='terminal correction has a live nearby obstacle; holding stand';
assert(failureExplanation(data.failure_detail).includes('12.0 cm'));
''')

    def test_stale_or_consumed_advice_never_offers_current_action(self):
        self.run_browser('''
for (const patch of [{applicable:false},{task_id:'old'},{step_id:'old'},{control_command_id:'old'}]) {
 const old=data.recovery_advice; data.recovery_advice={...old,...patch};
 const html=renderRecoveryAdvice(data);
 assert(!html.includes('onclick')); assert(!html.includes('LONG_MODEL_REASON'));
 assert(renderAttemptHistory(data).includes('历史建议'));
 data.recovery_advice=old;
}
data.advice_confirmations=[{advice_id:'abc',action:'request_help',status:'processing'}];
assert(!renderRecoveryAdvice(data).includes('onclick'));
assert(renderAttemptHistory(data).includes('processing'));
''')

    def test_unknown_control_reply_blocks_advice_retry_but_allows_query(self):
        self.run_browser('''
displayedTask=data; pendingTaskControl={task_id:'t', command_id:'nav-1'};
data.recovery_advice.action='continue';
assert(renderRecoveryAdvice(data).includes('disabled'));
confirmRecoveryAdvice(); // Must return before confirmation or any network call.
data.recovery_advice.action='query_original';
assert(!renderRecoveryAdvice(data).includes('disabled'));
''')

    def test_recovery_plan_shows_exact_steps_escaped_reason_and_blocks_unknown_control(self):
        self.run_browser('''
data.recovery_advice.action='revise_plan';
data.recovery_advice.plan={step_order:['STEP_2','STEP_1'],observation_id:'obs-1'};
data.subtask_list=[{step_id:'STEP_1',subtask:'抓取 A'}, {step_id:'STEP_2',subtask:'<script>bad</script>'}];
let html=renderRecoveryAdvice(data);
assert(html.includes('确认按此方案恢复执行'));
assert(html.includes('obs-1'));
assert(html.includes('&lt;script&gt;bad'));
assert(!html.includes('<script>'));
assert(html.indexOf('&lt;script&gt;bad') < html.indexOf('抓取 A'));
displayedTask=data; pendingTaskControl={task_id:'t',command_id:'nav-1'};
assert(renderRecoveryAdvice(data).includes('disabled'));
confirmRecoveryAdvice();
data.recovery_advice.action='observe_scene';
assert(renderRecoveryAdvice(data).includes('读取现场并重新分析'));
''')

    def test_retry_step_advance_and_terminal_remove_old_failure_card(self):
        self.run_browser('''
renderTaskProgress(data);
assert(nodes['task-progress'].innerHTML.includes('class="recovery-advice"'));
taskNoticeContext=taskDisplayContext(data);
nodes['task-result'].textContent='控制请求未完成：当前环节或命令已变化';
data.state='running'; data.control_command_id='nav-1-r1'; data.failure_detail=null;
data.recovery_advice.applicable=false;
renderTaskProgress(data);
assert(!nodes['task-progress'].innerHTML.includes('class="recovery-advice"'));
assert.equal(nodes['task-result'].textContent,'');
data.control_step_id='NAVIGATING_TO_RELAY2'; renderTaskProgress(data);
assert(nodes['task-progress'].innerHTML.includes('当前步骤：导航到中转点 2'));
data.state='failed'; data.terminal=true; data.flow_finished=true;
renderTaskProgress(data);
assert(nodes['task-progress'].innerHTML.includes('流程结束（含人工跳过，未全部成功）'));
assert(!nodes['task-progress'].innerHTML.includes('class="recovery-advice"'));
assert(nodes['task-progress'].innerHTML.includes('历史建议'));
''')

    def test_late_poll_response_or_error_cannot_replace_newer_status(self):
        page = Path('src/entries/web/templates/index.html').read_text()
        code = page[page.index("        let reportContext ="):page.index('        // 接待完成确认报告')]
        script = '''const assert=require('assert');
let taskStatusRequest=0,taskStatusApplied=0,taskPollingTimer=1,taskReceiptId=null;
let taskControlPending=false,taskSubmitting=false,taskStatusKnown=true;
const pending=[], rendered=[];
function readTaskStatus(){return new Promise((resolve,reject)=>pending.push({resolve,reject}));}
function renderTaskProgress(data){rendered.push(data.state);}
function updateTaskButtons(){}
function markTaskStatus(){}
function markTaskStatusError(){throw new Error('stale error applied');}
function loadReceptionReport(){}
function stopTaskPolling(){}
function startTaskPolling(){}
const document={getElementById(){return {innerHTML:''};}};
''' + code + '''
(async()=>{
 const old=refreshTaskStatus(), fresh=refreshTaskStatus();
 pending[1].resolve({task_id:'t',state:'succeeded',terminal:true,all_done:true}); await fresh;
 pending[0].resolve({task_id:'t',state:'recovery_required',active:true}); await old;
 assert.deepEqual(rendered,['succeeded']);
 const failed=refreshTaskStatus(), next=refreshTaskStatus();
 pending[3].resolve({task_id:'new',state:'running',active:true}); await next;
 pending[2].reject(new Error('late network error')); await failed;
 assert.deepEqual(rendered,['succeeded','running']);
 const slow=refreshTaskStatus(), later=refreshTaskStatus();
 pending[4].resolve({task_id:'new',state:'verifying',active:true}); await slow;
 assert.equal(rendered.at(-1),'verifying');
 pending[5].resolve({task_id:'new',state:'succeeded',terminal:true,all_done:true}); await later;
 assert.equal(rendered.at(-1),'succeeded');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hybrid_completion_and_manipulation_are_explicitly_simulated(self):
        self.run_browser('''
data.execution_backend='reception_real_sim_manipulation';
data.state='succeeded'; data.terminal=true; data.all_done=true;
data.total=2; data.completed=2; data.failure_detail=null;
data.subtask_list=[
 {step_id:'NAVIGATING_TO_TABLE2',done:true,status:'success',robot_name:'FQrobot'},
 {step_id:'VLA_PICKING',done:true,status:'success',robot_name:'FQrobot',simulated:true}
];
renderTaskProgress(data);
const html=nodes['task-progress'].innerHTML;
assert(html.includes('混合流程完成（操控为模拟）'));
assert(html.includes('导航真机＋操控模拟（不含真实抓放）'));
assert(html.includes('抓取饮料（模拟）'));
assert(!html.includes('导航到 2 号桌（模拟）'));
assert(!html.includes('✓ 任务成功'));
''')

    def test_cancelled_task_is_collapsed_history_and_keeps_unresolved_warning(self):
        self.run_browser('''
data.state='cancelled'; data.terminal=true; data.active=false;
data.blocks_new_motion=false; data.command_unknown=false;
data.current_command={command_id:'nav-1',stage:'failed'};
data.subtask_list=[{step_id:'NAVIGATING_TO_TABLE1',status:'failure',subtask:'table1'}];
const before=JSON.stringify(data);
renderTaskProgress(data);
let html=nodes['task-progress'].innerHTML;
assert(html.startsWith('<div class="no-task">暂无执行中的任务</div>'));
assert(html.includes('<details data-task-detail="cancelled:t"><summary>上一轮已取消 · 查看记录</summary>'));
assert(!html.includes('<details open'));
assert(html.indexOf('执行失败')>html.indexOf('查看记录</summary>'));
assert.equal(nodes['task-result'].style.display,'none');
assert.equal(JSON.stringify(data),before);
data.command_unknown=true;data.blocks_new_motion=true;
renderTaskProgress(data);html=nodes['task-progress'].innerHTML;
assert(html.indexOf('原命令仍需核清')<html.indexOf('<details'));
// A new running task must display normally, outside the old history wrapper.
data.task_id='new';data.state='running';data.terminal=false;data.active=true;
renderTaskProgress(data);html=nodes['task-progress'].innerHTML;
assert(!html.includes('上一轮已取消'));assert(html.includes('正在处理当前步骤'));
''')
