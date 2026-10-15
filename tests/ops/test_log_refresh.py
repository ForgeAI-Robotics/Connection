"""Exercise the real panel log reader with delayed responses and timer ticks."""
from pathlib import Path
import subprocess
import unittest


class LogRefreshTests(unittest.TestCase):
    def run_browser(self, script):
        source = Path('src/ops/static/panel.js').read_text()
        reader = source[source.index('async function loadLogs('):source.index('async function refreshLayer(')]
        timers = source[source.index('setInterval(() => {'):]
        setup = r'''
const assert = require('node:assert/strict');
let selected = 'master', busy = true, logToken = 0, logAbort = null;
let activeLogRequest = '', renderedLog = '', logPath = '';
const nodes = {};
function $(id) { return nodes[id] ||= {textContent:'', scrollTop:0, scrollHeight:100,
  classList:{add(){},remove(){}}}; }
function currentItem() { return {id:selected}; }
function stickToBottom() { return true; }
const document = {hidden:false};
const intervals = [], deadlines = new Map(); let nextDeadline = 0;
function setInterval(fn, ms) { intervals.push({fn,ms}); }
function setTimeout(fn, ms) { deadlines.set(++nextDeadline, fn); return nextDeadline; }
function clearTimeout(id) { deadlines.delete(id); }
function refresh() { return new Promise(() => {}); } // Stalled health checks.
const calls = [];
function fetch(url, options) {
  return new Promise((resolve, reject) => {
    calls.push({url, options, resolve, reject});
  });
}
function reply(call, id, text) { call.resolve({ok:true, json:async()=>({id,text,path:'/log'})}); }
async function flush() { for(let i=0;i<8;i++) await Promise.resolve(); }
'''
        result = subprocess.run(['node', '-e', setup + reader + timers
                                 + '\n(async()=>{\n' + script
                                 + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_logs_update_within_one_second_despite_busy_action_and_stalled_health(self):
        self.run_browser('''
const fast = intervals.find(t=>t.ms<=1000);
assert(fast, 'No log refresh within one second');
for(const timer of intervals) timer.fn();
assert.equal(calls.length,1);
assert.equal(calls[0].options.cache,'no-store');
fast.fn(); assert.equal(calls.length,1, 'Duplicate in-flight request');
reply(calls[0],'master','received command'); await flush();
assert.equal($('log').textContent,'received command');
assert.equal(busy,true);
fast.fn(); reply(calls[1],'master','received command\\nprocessing'); await flush();
assert($('log').textContent.endsWith('processing'));
document.hidden=true; fast.fn(); assert.equal(calls.length,2);
assert.equal(deadlines.size,0);
''')

    def test_switch_discards_late_response_and_timeout_allows_retry(self):
        self.run_browser('''
const old = loadLogs(false);
selected='deploy'; const fresh=loadLogs(true);
assert(calls[0].options.signal.aborted);
reply(calls[1],'deploy','new service'); await fresh;
reply(calls[0],'master','stale output'); await old;
assert.equal($('log').textContent,'new service');
const stalled=loadLogs(false), call=calls[2];
call.options.signal.addEventListener('abort',()=>call.reject(new DOMException('aborted','AbortError')));
for(const expire of [...deadlines.values()]) expire();
await stalled;
assert.equal(activeLogRequest,'');
assert($('log').textContent.includes('超时'));
const retry=loadLogs(false); reply(calls[3],'deploy','recovered'); await retry;
assert.equal($('log').textContent,'recovered');
assert.equal(deadlines.size,0);
''')
