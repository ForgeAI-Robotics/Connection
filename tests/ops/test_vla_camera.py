"""Execute generated camera wrappers against fake tmux/profile/script only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ops import vla_camera
from ops.app import create_app
from ops.services import by_id


class CameraLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "camera project's workspace"
        self.root.mkdir()
        self.runtime = Path(self.temp.name) / 'runtime'
        self.bin = Path(self.temp.name) / 'bin'; self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH'],
                        CAMERA_TEST_ROOT=str(self.root), CAMERA_TEST_TRACE=str(self.root/'calls.jsonl'))
        (self.root/'scripts').mkdir()
        (self.root/'scripts/select_robot.sh').write_text('export NX_HOST=127.0.0.1 NX_CAMERA_PORT=5555\nprintf "%s %s" "$1" "$2" > "$CAMERA_TEST_ROOT/profile"\n')
        script = self.root / vla_camera.SCRIPT; script.parent.mkdir(parents=True)
        script.write_text('printf "%s %s %s" "$CAMERA_EGO_ENABLED" "$CAMERA_LEFT_WRIST_ENABLED" "$CAMERA_RIGHT_WRIST_ENABLED" > "$CAMERA_TEST_ROOT/flags"\necho CAMERA_SCRIPT_STARTED\nexit "${CAMERA_TEST_EXIT:-0}"\n')
        self.executable('python3', '#!/bin/bash\nexit "${CAMERA_TEST_PORT_CLOSED:-1}"\n')
        self.executable('tmux', '#!'+sys.executable+'\n'+'''import json,os,subprocess,sys
a=sys.argv[1:]
with open(os.environ['CAMERA_TEST_TRACE'],'a') as f:f.write(json.dumps(a)+'\\n')
if a[0]=='has-session':raise SystemExit(0 if os.environ.get('CAMERA_TEST_EXISTS')=='1' else 1)
if a[0]=='show-options':print(os.environ.get('CAMERA_TEST_OWNER','connection-vla-camera-G1-wireless-v1'))
if a[0]=='new-session' and os.environ.get('CAMERA_TEST_RUN')=='1':subprocess.run(['bash','-c',a[-1]],check=False)
''')
        for p in (patch.object(vla_camera.vla_remote, 'settings', return_value={'remote_dir':str(self.root/'g1_brain_vla_bridge')}),
                  patch.object(vla_camera,'RUN_DIR',str(self.runtime))):
            p.start(); self.addCleanup(p.stop)

    def executable(self, name, text):
        p=self.bin/name;p.write_text(text);p.chmod(0o700)

    def run_script(self, action, **env):
        script,_=vla_camera._remote_script(action)
        return subprocess.run(['bash','-c',script],env=dict(self.env,**env),text=True,capture_output=True,timeout=10)

    def calls(self):
        p=self.root/'calls.jsonl'
        return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []

    def test_exact_three_camera_command_profile_and_quoted_workspace(self):
        r=self.run_script('start',CAMERA_TEST_RUN='1')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual((self.root/'profile').read_text(),'G1 wireless')
        self.assertEqual((self.root/'flags').read_text(),'True True True')
        self.assertIn('CAMERA_SCRIPT_STARTED',(self.runtime/'start.log').read_text())
        calls=self.calls()
        self.assertEqual(sum(c[0]=='new-session' for c in calls),1)
        self.assertTrue(all('run_agent_vla_runtime.sh' not in str(c) for c in calls))

    def test_existing_session_and_existing_port_never_start_second_camera(self):
        r=self.run_script('start',CAMERA_TEST_EXISTS='1')
        self.assertEqual(r.returncode,0,r.stderr)
        r=self.run_script('start',CAMERA_TEST_PORT_CLOSED='0')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertFalse(any(c[0]=='new-session' for c in self.calls()))
        self.assertFalse((self.root/'flags').exists())

    def test_foreign_session_refused_for_start_and_stop(self):
        for action in ('start','stop'):
            r=self.run_script(action,CAMERA_TEST_EXISTS='1',CAMERA_TEST_OWNER='somebody-else')
            self.assertNotEqual(r.returncode,0)
        self.assertFalse(any(c[0] in {'kill-session','send-keys','new-session'} for c in self.calls()))

    def test_stop_without_owned_session_does_not_touch_external_camera(self):
        r=self.run_script('stop')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('未停止外部相机',r.stdout)
        self.assertFalse(any(c[0] in {'kill-session','send-keys'} for c in self.calls()))

    def test_start_script_failure_is_returned_and_logs_remain_readable(self):
        r=self.run_script('start',CAMERA_TEST_RUN='1',CAMERA_TEST_EXIT='7')
        self.assertEqual(r.returncode,7,r.stderr)
        logs=self.run_script('logs')
        self.assertIn('CAMERA_SCRIPT_STARTED',logs.stdout)
        self.assertIn('退出码：7',logs.stdout)


class CameraPanelTests(unittest.TestCase):
    def test_card_routes_only_to_camera_launcher(self):
        app=create_app()
        self.addCleanup(lambda:[w.shutdown(wait=False) for w in app.extensions['workers']])
        client=app.test_client()
        with patch('ops.vla_camera.run') as camera, patch('ops.vla_remote.run') as vla, \
                patch('ops.app._service_status',return_value={'id':'vla_camera'}):
            self.assertTrue(client.post('/api/services/vla_camera/start').get_json()['ok'])
            camera.assert_called_once_with('start');vla.assert_not_called()
        with patch('ops.vla_camera.logs',return_value=('camera output','camera.log',None)):
            self.assertEqual(client.get('/api/services/vla_camera/logs').get_json()['text'],'camera output')
        html=client.get('/').get_data(as_text=True)
        self.assertIn('"id": "vla_camera"',html)
        data=json.loads(html.split('<script id="initial-services" type="application/json">')[1].split('</script>')[0])
        camera=next(s for s in data['robot'] if s['id']=='vla_camera')
        self.assertIn('先启动头部',camera['note'])

    def test_tcp_connectivity_is_not_reported_as_three_camera_frames(self):
        with patch('ops.services._tcp_open',return_value=True), patch('ops.services._record_probe'):
            result=vla_camera.health(by_id('vla_camera'))
        self.assertTrue(result['ok']);self.assertFalse(result['frames_verified'])
        self.assertEqual(result['scope'],'tcp_connectivity_only')


if __name__=='__main__':unittest.main()
