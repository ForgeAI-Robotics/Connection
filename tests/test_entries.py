import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from werkzeug.datastructures import FileStorage
from entries.web.app import create_app as create_web
from entries.voice.app import create_app as create_voice
from brain.api.app import create_app
from brain.app import create_service
from brain.application import BrainApplication
from brain.learning.demo import DemoLearning


class EntryTests(unittest.TestCase):
    def test_web_forwards_identity_and_failure_without_retry(self):
        client = Mock()
        client.request.return_value = Mock(content=b'{"accepted":false}', status_code=409,
                                          headers={'content-type': 'application/json'})
        app = create_web(client=client).test_client()
        result = app.post('/api/publish_task', json={'task': '抓取牛奶', 'task_id': 'given'})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(client.request.call_args.args, ('POST', '/publish_task'))
        self.assertEqual(client.request.call_args.kwargs['json']['task_id'], 'given')
        client.request.side_effect = TimeoutError('lost reply')
        self.assertEqual(app.post('/api/publish_task', json={'task': '抓取牛奶'}).status_code, 503)
        self.assertEqual(client.request.call_count, 2)
        for page in ('/', '/teach', '/reception'):
            self.assertEqual(app.get(page).status_code, 200)

    def test_skip_route_requires_both_identities_and_web_forwards_them(self):
        facade = Mock()
        facade.control.return_value = {'accepted': True, 'skipped_step_id': 'step-1'}
        app = create_app(Mock(), facade=facade).test_client()
        for payload in ({}, {'task_id': 't'}, {'step_id': 'step-1'},
                        {'task_id': 't', 'step_id': 123}):
            self.assertEqual(app.post('/api/task_skip', json=payload).status_code, 400)
        facade.control.assert_not_called()
        self.assertTrue(app.post('/api/task_skip', json={'task_id': 't', 'step_id': 'step-1'}).json['accepted'])
        facade.control.assert_called_once_with('skip', task_id='t', step_id='step-1')
        proxy = Mock()
        proxy.request.return_value = Mock(content=b'{"accepted":true}', status_code=200,
                                          headers={'content-type': 'application/json'})
        web = create_web(client=proxy).test_client()
        self.assertEqual(web.post('/api/task_skip', json={'task_id': 't', 'step_id': 'step-1'}).status_code, 200)
        self.assertEqual(proxy.request.call_args.args, ('POST', '/api/task_skip'))
        self.assertEqual(proxy.request.call_args.kwargs['json'], {'task_id': 't', 'step_id': 'step-1'})

    def test_brain_api_read_and_cancel_work_without_web(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {'CONNECTION_WORKSPACE': root}):
            config = {'brain': {'capture_timeline': False}, 'reflection': {'enabled': False},
                      'reception_real': {'kernel_runtime_dir': root + '/kernel', 'kernel_enabled': False}}
            application = BrainApplication(create_service(config, model=lambda _: '{}')).start()
            try:
                client = create_app(application).test_client()
                self.assertTrue(client.get('/health').json['ready'])
                self.assertEqual(client.get('/api/task_status').status_code, 200)
                self.assertEqual(client.post('/api/task_cancel').status_code, 200)
                intent = client.post('/api/task_intent', json={'task': '停止'}).json
                self.assertEqual(intent['intent'], 'control')
                self.assertFalse(intent['needs_llm_route'])
                for text in ('停止', '取消任务', '暂停', '继续'):
                    result = client.post('/publish_task', json={'task': text, 'task_id': 'not-a-new-task'}).json
                    self.assertEqual(result['intent'], 'control')
                    self.assertTrue(result['no_op'])
                self.assertIsNone(application.status().get('task_id'))
                self.assertEqual(client.post('/publish_task', json={'task': '抓取', 'refresh': True, 'task_id': '../oops'}).status_code, 400)
                self.assertEqual(client.get('/api/auto_tools').status_code, 200)
                sops = client.get('/api/sops').json
                self.assertEqual(sops['packages']['reception']['id'], 'reception.single_can')
            finally:
                self.assertTrue(application.close())

    def test_demo_is_a_candidate_and_old_result_cannot_be_saved_as_new_job(self):
        with tempfile.TemporaryDirectory() as root:
            learner = DemoLearning(root, processor=lambda *a: {'task_specific': ['物体在桌上'], 'global_rules': []})
            try:
                job = learner.submit(FileStorage(io.BytesIO(b'video'), filename='../../escape.mp4'), 'test')
                for _ in range(100):
                    if not learner.status().get('running'):
                        break
                    time.sleep(.01)
                self.assertEqual(learner.save(job['job_id'])['status'], 'unevaluated')
                self.assertFalse((Path(root) / 'reception_sop.yaml').exists())
                with self.assertRaises(ValueError):
                    learner.save('other-job')
                self.assertEqual(len(list((Path(root) / 'demonstration_candidates').glob('*.json'))), 1)
            finally:
                learner.close()

    def test_voice_forwards_recognized_text_without_filtering(self):
        client = Mock()
        client.request.return_value = Mock(
            content=b'{"accepted":true,"status":"success"}',
            status_code=200,
            headers={"content-type": "application/json"},
        )
        app = create_voice(client=client).test_client()
        result = app.post("/publish_task", json={"task": "  请开始接待  ", "task_id": "given"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.request.call_args.args, ("POST", "/publish_task"))
        body = client.request.call_args.kwargs["json"]
        self.assertEqual(body["task"], "请开始接待")
        self.assertEqual(body["task_id"], "given")
        self.assertTrue(body["refresh"])
        headers = client.request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-FQ-Source"], "voice")
        self.assertEqual(headers["X-FQ-Via"], "voice")
        self.assertTrue(headers["X-FQ-Client"])
        other = app.post("/publish_task", json={"task": "去一号桌拿可乐"})
        self.assertEqual(other.status_code, 200)
        self.assertEqual(client.request.call_args.kwargs["json"]["task"], "去一号桌拿可乐")
        self.assertEqual(len(client.request.call_args.kwargs["json"]["task_id"]), 32)
        self.assertEqual(app.post("/publish_task", json={"task": "   "}).status_code, 400)
        self.assertEqual(app.post("/publish_task", json={}).status_code, 400)
        client.request.side_effect = TimeoutError("lost reply")
        failed = app.post("/publish_task", json={"task": "开始接待"})
        self.assertEqual(failed.status_code, 503)
        self.assertTrue(failed.json["unavailable"])
        self.assertEqual(app.get("/health").json, {"ready": True, "entry": "voice"})

    def test_entries_import_no_brain_or_robot_code(self):
        import subprocess, sys
        script = '''
import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('brain', 'robot_api', 'redis', 'master')):
            raise RuntimeError(fullname)
sys.meta_path.insert(0, Deny())
from entries.web.app import create_app
from entries.feishu.bridge import FeishuBridge
from entries.voice.app import create_app as create_voice
create_app()
create_voice()
'''
        result = subprocess.run([sys.executable, '-c', script], cwd='/tmp', text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
