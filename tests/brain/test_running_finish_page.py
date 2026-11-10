"""Exercise the actual confirmation handler with a fake browser and HTTP response."""
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node required for browser-handler check')
class RunningFinishPageTests(unittest.TestCase):
    def test_click_is_bound_to_original_command_and_lost_reply_never_reposts(self):
        page = Path('src/entries/web/templates/index.html').read_text()
        handler = page[page.index('        const vlaFinishIntents'):page.index('        function updateTaskButtons')]
        code = r'''
const assert = require('assert');
const vm = require('vm');
const fs = require('fs');
const handler = fs.readFileSync(0, 'utf8');
(async () => {
 for (const lost of [false, true]) {
  for (const operation of ['open_fridge','grasp_cola']) {
   const sent=[]; let queries=0;
   const elements={'task-command-feedback':{textContent:''}};
   const ctx={crypto:require('crypto').webcrypto, Uint8Array, Map, Date,
      displayedTask:{task_id:'task-one',state:'waiting_human',vla_confirmation:{command_id:'command-one',
         state:'running',operation,object_id:operation==='grasp_cola'?'cola_can_1':'fridge_1'}},
      taskStatusKnown:true,taskControlPending:false,document:{getElementById:id=>elements[id]},
      vlaEffectControl:async action=>{assert.equal(action,'query');queries++;},refreshTaskStatus:async()=>{},
      fetch:async (url,options)=>{sent.push([url,options]);if(lost)throw Error('lost reply');
         return {json:async()=>({accepted:true})};}};
   vm.createContext(ctx);vm.runInContext(handler,ctx);
   await ctx.confirmVlaCompletion();await ctx.confirmVlaCompletion();
   assert.equal(sent.length,1);assert.equal(queries,1);
   const body=JSON.parse(sent[0][1].body);
   assert.equal(sent[0][0],'/api/task_vla/finish');assert.equal(body.command_id,'command-one');
   assert.equal(body.task_id,'task-one');assert.equal(body.auto_advance,true);
   assert.equal(body.evidence.source,'operator_observation');assert.ok(body.evidence.reference);
   assert.ok(Number.isFinite(Date.parse(body.evidence.observed_at)));
   assert.equal(sent[0][1].headers.Authorization,undefined);
   assert.ok(!sent[0][1].body.includes('test-only'));
   assert.equal(operation==='grasp_cola'?body.evidence.clear_of_fridge:body.evidence.hands_clear_of_door,true);
   ctx.displayedTask.pause_requested=true;await ctx.confirmVlaCompletion();assert.equal(sent.length,1);
  }
 }
})().catch(e=>{console.error(e);process.exit(1);});
'''
        result = subprocess.run(['node','-e',code],input=handler,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('id="vla-verifier"', page)

    def test_auto_grasp_renders_one_step_without_manual_completion_button(self):
        from tests.brain.test_advice_presentation import AdvicePresentationTests
        AdvicePresentationTests().run_browser("""
const vlaFinishIntents=new Map();
data.state='waiting_human';data.blocked_reason='vla_effect_confirmation';data.failure_detail=null;
data.vla_confirmation={command_id:'grasp-1',operation:'grasp_cola',execution_mode:'direct',state:'running',auto_verification:true};
data.subtask_list=[{step_id:'VLA_GRASP_COLA',subtask:'抓取可乐并后退撤离',display_name:'抓取可乐并后退撤离',status:'pending'}];
data.total=1;data.phase='VLA_GRASP_COLA';
renderTaskProgress(data);
const html=nodes['task-progress'].innerHTML;
assert(html.includes('自动核验中'));assert(html.includes('自动核验左右手均持有可乐和双臂回位保持'));
assert(!html.includes('确认后退撤离完成且可乐仍在手'));
assert(!html.includes('onclick=\"confirmVlaCompletion()\"'));
""")

    def test_normal_effect_wait_is_not_rendered_as_recovery_advice(self):
        page = Path('src/entries/web/templates/index.html').read_text()
        handler = page[page.index('        function awaitingVlaEffect'):page.index('        function renderRecoveryAdvice')]
        code = r'''
const assert=require('assert'),vm=require('vm'),fs=require('fs');
const ctx={};vm.createContext(ctx);vm.runInContext(fs.readFileSync(0,'utf8'),ctx);
const data={state:'waiting_human',blocked_reason:'vla_effect_confirmation',
 vla_confirmation:{state:'running',execution_mode:'direct'}};
assert.equal(ctx.awaitingVlaEffect(data),true);
assert.equal(ctx.awaitingVlaEffect({...data,state:'recovery_required'}),false);
assert.equal(ctx.awaitingVlaEffect({...data,pause_requested:true}),false);
assert.equal(ctx.awaitingVlaEffect({...data,failure_detail:{message:'failed'}}),false);
assert.equal(ctx.awaitingVlaEffect({...data,vla_confirmation:{...data.vla_confirmation,observation_error:'timeout'}}),false);
assert.equal(ctx.awaitingVlaEffect({...data,vla_confirmation:{...data.vla_confirmation,finish_rejection:{}}}),false);
'''
        result=subprocess.run(['node','-e',code],input=handler,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("if (awaitingVlaEffect(data)) return '';",page)
