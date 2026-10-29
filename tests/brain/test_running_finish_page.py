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
   const elements={'vla-verifier':{value:'test-only'}, 'task-command-feedback':{textContent:''}};
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
   assert.equal(sent[0][1].headers.Authorization,'Bearer test-only');
   assert.ok(!sent[0][1].body.includes('test-only'));
   assert.equal(operation==='grasp_cola'?body.evidence.clear_of_fridge:body.evidence.hands_clear_of_door,true);
   ctx.displayedTask.pause_requested=true;await ctx.confirmVlaCompletion();assert.equal(sent.length,1);
  }
 }
})().catch(e=>{console.error(e);process.exit(1);});
'''
        result = subprocess.run(['node','-e',code],input=handler,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
