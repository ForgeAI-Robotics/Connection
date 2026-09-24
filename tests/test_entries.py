import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from werkzeug.datastructures import FileStorage
from entries.web.app import create_app as create_web
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
                self.assertEqual(client.post('/publish_task', json={'task': '抓取', 'refresh': True, 'task_id': '../oops'}).status_code, 400)
                self.assertEqual(client.get('/api/auto_tools').status_code, 200)
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
create_app()
'''
        result = subprocess.run([sys.executable, '-c', script], cwd='/tmp', text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
