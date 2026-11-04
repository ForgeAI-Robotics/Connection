"""Runtime status/action receipts and panel semantics against isolated fakes."""
import copy
import json
from pathlib import Path
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from ops import vla_remote
from ops.app import create_app
from ops.control import _service_status
from ops.services import by_id
from tests.ops.vla_fixture import status, action_result


class VlaRuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        vla_remote.invalidate_status()
        self.addCleanup(vla_remote.invalidate_status)
        for target, value in [('ops.vla_remote.record', None), ('ops.services._record_probe', None)]:
            p = patch(target, return_value=value); p.start(); self.addCleanup(p.stop)

    @staticmethod
    def runner(value, returncode=0):
        def run(argv, timeout, env):
            return subprocess.CompletedProcess(argv, returncode, json.dumps(value), '')
        return run

    def test_manual_official_instance_is_seen_without_panel_session(self):
        with patch('ops.vla_remote._default_runner', side_effect=self.runner(status())), \
             patch('ops.tmuxctl.session_alive', side_effect=AssertionError('no panel ownership probe')):
            item = _service_status(by_id('vla'))
        self.assertEqual(item['state'], 'up')
        self.assertTrue(item['health']['runtime']['http']['managed'])
        self.assertTrue(item['health']['runtime']['http']['can_stop'])

    def test_http_stopped_does_not_mark_retained_peripherals_stopped(self):
        with patch('ops.vla_remote._default_runner', side_effect=self.runner(status(running=False))):
            item = _service_status(by_id('vla'))
        self.assertEqual(item['state'], 'stopped')
        self.assertEqual(item['health']['runtime']['camera']['state'], 'running')
        self.assertFalse(item['health']['runtime']['camera']['frames_verified'])
        self.assertIsNone(item['health']['runtime']['camera']['ready'])
        self.assertTrue(item['health']['runtime']['o6']['ready'])

    def test_stale_or_malformed_status_is_unknown_not_down_or_healthy(self):
        bad_time = status(); bad_time['observed_at'] = '2020-01-01T00:00:00+00:00'
        bad_bool = status(); bad_bool['camera']['ready'] = 'true'
        bad_fps = status(); bad_fps['camera']['streams']['ego_view']['fps'] = float('nan')
        for value in ({'status': 'ok'}, bad_time, bad_bool, bad_fps):
            with self.subTest(value=value):
                report = vla_remote.runtime_status(runner=self.runner(value), force=True)
                self.assertEqual(report['http']['state'], 'unknown')
                self.assertFalse(report['http']['can_stop'])
        with patch('ops.vla_remote._default_runner', side_effect=subprocess.TimeoutExpired('ssh', 15)):
            item = vla_remote.runtime_status(force=True)
        self.assertEqual(item['camera']['state'], 'unknown')

    def test_not_ready_is_partial_and_busy_stop_is_explicit(self):
        with patch('ops.vla_remote._default_runner', side_effect=self.runner(status(ready=False, can_stop=False))):
            item = _service_status(by_id('vla'))
        self.assertEqual(item['state'], 'partial')
        self.assertFalse(item['health']['runtime']['http']['can_stop'])
        self.assertIn('active task', item['health']['runtime']['http']['reason'])

    def test_camera_clock_fault_preserves_http_and_stop_state(self):
        value = status(ready=False)
        value['camera']['streams']['ego_view'].update(age_ms=-200, reason='source_clock_future')
        with patch('ops.vla_remote._default_runner', side_effect=self.runner(value)):
            item = _service_status(by_id('vla'))
        self.assertEqual(item['state'], 'partial')
        self.assertTrue(item['health']['runtime']['http']['can_stop'])
        self.assertEqual(item['health']['runtime']['camera']['streams']['ego_view']['reason'], 'source_clock_future')
        value['camera']['streams']['ego_view']['ready'] = True
        report = vla_remote.runtime_status(runner=self.runner(value), force=True)
        self.assertEqual(report['http']['state'], 'unknown')

    def test_concurrent_poll_singleflight_and_return_values_not_shared(self):
        entered = threading.Event(); release = threading.Event()
        calls = []
        def run(argv, timeout, env):
            calls.append(argv); entered.set(); self.assertTrue(release.wait(2))
            return subprocess.CompletedProcess(argv, 0, json.dumps(status()), '')
        with ThreadPoolExecutor(max_workers=5) as pool:
            jobs = [pool.submit(vla_remote.runtime_status, runner=run) for _ in range(5)]
            self.assertTrue(entered.wait(2)); release.set()
            reports = [j.result() for j in jobs]
        self.assertEqual(len(calls), 1)
        reports[0]['http']['state'] = 'stopped'
        self.assertTrue(all(r['http']['state'] == 'running' for r in reports[1:]))
        self.assertIn('status-json', calls[0][-1])

    def action_runner(self, result, *, banner=''):
        return lambda argv, timeout, env: subprocess.CompletedProcess(
            argv, 0, banner + 'VLA_RUNTIME_RESULT ' + json.dumps(result) + '\n', '')

    def test_exit_zero_without_verified_receipt_is_not_success(self):
        no_receipt = lambda argv, timeout, env: subprocess.CompletedProcess(argv, 0, 'STOPPED\n', '')
        with self.assertRaisesRegex(RuntimeError, '未返回唯一'):
            vla_remote.run('stop', runner=no_receipt)
        for result in [action_result('stop', confirmed=False), action_result('stop', ok=False),
                       action_result('start'), action_result('stop', status=status(running=True))]:
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                vla_remote.run('stop', runner=self.action_runner(result))

    def test_confirmed_stop_and_start_are_returned_and_cache_invalidated(self):
        vla_remote.runtime_status(runner=self.runner(status()))
        result = vla_remote.run('stop', runner=self.action_runner(action_result('stop')))
        self.assertTrue(result['confirmed'])
        self.assertIn('相机与 O6 保留', result['message'])
        self.assertIsNone(vla_remote._STATUS_CACHE)
        started = vla_remote.run('start', runner=self.action_runner(action_result()))
        self.assertTrue(started['status']['camera']['ready'])

    def test_action_lock_prevents_overlapping_stop_and_start(self):
        entered = threading.Event(); release = threading.Event()
        def run(argv, timeout, env):
            entered.set(); self.assertTrue(release.wait(2))
            return self.action_runner(action_result())(argv, timeout, env)
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(vla_remote.run, 'start', runner=run)
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(RuntimeError, '正在进行'):
                vla_remote.run('stop', runner=lambda *a: self.fail('must not send stop'))
            release.set(); self.assertTrue(job.result()['confirmed'])

    def test_endpoint_never_fabricates_confirmation_and_rejects_restart(self):
        app = create_app()
        self.addCleanup(lambda: [w.shutdown(wait=False) for w in app.extensions['workers']])
        client = app.test_client()
        with patch('ops.vla_remote.run', return_value='legacy text'):
            r = client.post('/api/services/vla/stop')
            self.assertEqual(r.status_code, 502)
            self.assertFalse(r.json['confirmed'])
        with patch('ops.vla_remote.run', return_value=action_result('stop')) as run:
            r = client.post('/api/services/vla/stop')
            self.assertTrue(r.json['confirmed']); self.assertIn('相机', r.json['message'])
            run.reset_mock()
            self.assertEqual(client.post('/api/services/vla/restart').status_code, 400)
            run.assert_not_called()


class VlaPanelJavaScriptTests(unittest.TestCase):
    def browser(self, script):
        source = Path('src/ops/static/panel.js').read_text()
        component = source[source.index('function vlaComponentLines'):source.index('// 9882')]
        action = source[source.index('async function act('):source.index('async function loadLogs(')]
        setup = r'''
const assert=require('node:assert/strict');
const message={hidden:true,textContent:'',classList:{add(){},remove(){}}};
function $(id){return message;}
let busy=false,selectedLayer='robot';
const REMOTE_ACTION_LABEL={};
function selectService(){} function refresh(){} function loadLogs(){}
const window={confirm(){return true;}};
let response={ok:true,json:async()=>({ok:true,confirmed:true,message:'HTTP 已停止；相机和 O6 保留'})};
async function fetch(){return response;}
'''
        r = subprocess.run(['node', '-e', setup + component + action + '\n(async()=>{' + script +
                            '})().catch(e=>{console.error(e);process.exitCode=1;});'], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_false_completed_message_and_components_keep_unknown_scope(self):
        self.browser('''
await act({id:'vla',name:'VLA'},'stop');
assert(message.textContent.includes('相机和 O6 保留'));
response={ok:true,json:async()=>({ok:true})};
await act({id:'vla',name:'VLA'},'stop');
assert(!message.textContent.includes('完成'));
response={ok:true,json:async()=>({ok:false,error:'停止未确认'})};
await act({id:'vla',name:'VLA'},'stop');
assert.equal(message.textContent,'停止未确认');
const lines=vlaComponentLines({health:{runtime:{http:{state:'stopped',can_stop:false},
 camera:{state:'running',frames_verified:false,streams:{}},
 o6:{state:'running',ready:true},observed_at:'sample-time'}}});
assert(lines.includes('头部：帧状态未知'));
assert(lines.some(s=>s.includes('O6：运行中')));
assert(lines.some(s=>s.includes('sample-time')));
''')

    def test_partial_camera_status_preserves_each_view_evidence(self):
        self.browser('''
const runtime={http:{state:'running',ready:true,can_stop:true},
 camera:{state:'running',ready:false,frames_verified:false,streams:{
   ego_view:{ready:true,fps:30,age_ms:20},
   left_wrist:{ready:true,fps:29.8,age_ms:25},
   right_wrist:{ready:false,fps:0,age_ms:900,reason:'frame_stale'}
 }},o6:{state:'running',ready:true}};
let lines=vlaComponentLines({health:{runtime}});
assert(lines.some(s=>s.includes('三路未全部通过')));
assert(lines.some(s=>s.startsWith('头部：检查时帧新鲜')&&s.includes('30.0 FPS')));
assert(lines.some(s=>s.startsWith('左腕：检查时帧新鲜')&&s.includes('29.8 FPS')));
assert(lines.some(s=>s.startsWith('右腕：无新鲜帧')&&s.includes('frame_stale')));
runtime.camera.streams.right_wrist={ready:false,age_ms:-100,reason:'source_clock_future'};
lines=vlaComponentLines({health:{runtime}});
assert(lines.some(s=>s.startsWith('右腕：无新鲜帧')&&s.includes('source_clock_future')));
''')

    def test_stop_button_follows_fresh_capability_even_when_state_unchanged(self):
        source = Path('src/ops/static/panel.js').read_text()
        card = source[source.index('function card('):source.index('function updateBrainSummary(')]
        patch_card = source[source.index('function patchCard('):source.index('function render(')]
        script = r'''
const assert=require('node:assert/strict');
let selected='vla';
const STATE_LABEL={up:'通',unknown:'未知'}; const REMOTE_ACTION_LABEL={start:'启动',stop:'停止'};
const escapeHtml=s=>String(s);const healthLine=()=>'';const vlaComponentLines=()=>[];
function selectService(){} function act(){}
let replaced=null;
function element(){return {dataset:{},children:[],classList:{toggle(){}},
 appendChild(v){this.children.push(v);},append(v){this.children.push(v);},
 querySelectorAll(){return this.children;}, replaceWith(v){replaced=v;}};}
const document={createElement:element};
function actionButton(label,fn){return {textContent:label,onclick:fn,disabled:false};}
'''
        script += card + patch_card + r'''
const item={id:'vla',name:'VLA 服务',state:'up',remote_actions:['start','stop'],
 health:{runtime:{http:{can_stop:true}}}};
const first=card(item);
assert.equal(first.children[0].children[1].disabled,false);
item.health.runtime.http={can_stop:false,reason:'active task'};
patchCard(first,item);
assert(replaced);
assert.equal(replaced.children[0].children[1].disabled,true);
assert.equal(replaced.children[0].children[1].title,'active task');
item.state='unknown';
const unknown=card(item);
assert(unknown.children[0].children.every(b=>b.disabled));
'''
        r = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
