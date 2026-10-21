"""Exercise the shipped environment selector, including delayed status replies."""
import json
from pathlib import Path
import subprocess
import unittest

from shared.execution_profile import resolve


class EnvironmentSelectorTests(unittest.TestCase):
    def run_browser(self, script):
        setup = r'''
const assert = require('node:assert/strict');
const nodes = {};
function el(id) { return nodes[id] ||= {textContent:'', hidden:false, disabled:false,
  classList:{toggle(){}}}; }
const choices = ['simulation', 'real'].map(value=>({value,checked:false}));
const document = {hidden:false, getElementById:el, querySelectorAll:()=>choices, addEventListener(){}};
const timers = [];
function setInterval(fn) { timers.push(fn); }
const calls = [], posts = [];
function fetch(url, options={}) {
  return new Promise(resolve=> {
    if(options.body) posts.push(JSON.parse(options.body));
    calls.push({url,options,resolve});
  });
}
function reply(call, data, ok=true) { call.resolve({ok,json:async()=>JSON.parse(JSON.stringify(data))}); }
async function flush() { for(let i=0;i<16;i++) await Promise.resolve(); }
function choose(mode) { for(const c of choices) c.checked=c.value===mode; choices.find(c=>c.checked).onchange(); }
function profile(mode) {
  return {revision:mode, routes:{
    reception:{mode, backend:mode==='real'?'reception_real':'reception_simple',available:true},
    execution:{mode, backend:mode==='real'?'unavailable':'simple_o7',available:mode!=='real'},
    observation:{mode, backend:mode==='real'?'unavailable':'simple_o7',available:false}
  }};
}
const current = {applied:profile('real'),draft:{mode:'simulation'},applying:false,
  real_motion_enabled:true,last_apply:{state:'succeeded',revision:'real'}};
'''
        source = Path('src/ops/static/execution.js').read_text()
        result = subprocess.run(['node', '-e', setup + source + '\n(async()=>{\n' + script +
                                 '\nconsole.log(JSON.stringify(posts));\n})().catch(e=>{console.error(e);process.exitCode=1;});'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_selecting_does_not_switch_until_apply_and_both_modes_resolve_correctly(self):
        posts = self.run_browser('''
reply(calls[0],current); await flush();
assert.equal(choices.find(c=>c.checked).value,'real'); // Applied profile wins over draft.
choose('simulation');
timers[0](); reply(calls[1],current); await flush();
assert.equal(choices.find(c=>c.checked).value,'simulation'); // Poll preserves selection.
assert.equal(posts.length,0);
const first=el('exec-apply').onclick();
assert(el('exec-choices').disabled);
await el('exec-apply').onclick(); assert.equal(posts.length,1); // No duplicate submission.
reply(calls[2],{accepted:true}); await flush();
reply(calls[3],{...current,applied:profile('simulation'),real_motion_enabled:false,
  last_apply:{state:'succeeded',revision:'simulation'}}); await first;
assert(el('exec-status').textContent.includes('当前已生效：SIMPLE 仿真'));
choose('real'); const second=el('exec-apply').onclick();
reply(calls[4],{accepted:true}); await flush(); reply(calls[5],current); await second;
assert(el('exec-status').textContent.includes('当前已生效：真机'));
assert(!el('exec-choices').disabled);
''')
        robot = {'backends': {'simple_o7': {'url': 'http://simple.test:18770'}}}
        simulation, real = [resolve(value, robot)['routes'] for value in posts]
        self.assertEqual(simulation['reception']['backend'], 'reception_simple')
        self.assertEqual(simulation['reception']['endpoints']['dream'], 'http://simple.test:18770/reception/nav')
        self.assertEqual(simulation['execution']['backend'], 'simple_o7')
        self.assertFalse(simulation['observation']['available'])
        self.assertEqual(real['reception']['backend'], 'reception_real')
        self.assertNotIn('endpoints', real['reception'])
        self.assertTrue(real['execution']['available'])
        self.assertEqual(real['execution']['backend'], 'body_real')

    def test_failure_keeps_actual_environment_and_stale_poll_cannot_unlock_switch(self):
        self.run_browser('''
reply(calls[0],current); await flush();
timers[0](); // Delayed old status read.
choose('simulation'); const apply=el('exec-apply').onclick();
reply(calls[2],{accepted:true}); await apply;
reply(calls[1],current); await flush();
assert(el('exec-choices').disabled); // Pre-submission status cannot unlock controls.
timers[0](); reply(calls[3],{...current,applying:true}); await flush();
assert(el('exec-status').textContent.includes('正在切换'));
timers[0](); reply(calls[4],{...current,last_apply:{state:'failed',error:'目标服务不可用'}}); await flush();
assert(el('exec-status').textContent.includes('当前已生效：真机'));
assert(el('exec-feedback').textContent.includes('目标服务不可用'));
assert(!el('exec-choices').disabled);
assert.equal(posts.length,1);
''')

    def test_unknown_profile_is_not_mislabeled_and_unavailable_status_disables_apply(self):
        self.run_browser('''
const mixed=profile('simulation'); mixed.routes.execution.backend='desk';
reply(calls[0],{...current,applied:mixed}); await flush();
assert(!choices.some(c=>c.checked));
assert(el('exec-status').textContent.includes('其他配置'));
assert(el('exec-apply').disabled);
choose('real'); assert(!el('exec-apply').disabled);
timers[0](); reply(calls[1],{error:'unreachable'},false); await flush();
assert(el('exec-apply').disabled);
assert(el('exec-status').textContent.includes('无法确认'));
assert.equal(posts.length,0);
''')

    def test_rejected_submission_message_survives_unchanged_poll(self):
        self.run_browser('''
reply(calls[0],current); await flush();
choose('simulation'); const apply=el('exec-apply').onclick();
reply(calls[1],{error:'任务尚未核清'},false); await flush();
reply(calls[2],current); await apply;
assert(el('exec-feedback').textContent.includes('任务尚未核清'));
assert.equal(choices.find(c=>c.checked).value,'simulation');
assert(el('exec-status').textContent.includes('当前已生效：真机'));
''')
