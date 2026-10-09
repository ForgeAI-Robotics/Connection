import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.app import create_service
from brain.adapters.ports import DeskAdapter
from brain.packages.reception_mock import MockAdapter
from brain.adapters.execution import LookAdapter


class IndependentBrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = {'reception_real': {'kernel_runtime_dir': str(root / 'kernel'),
                                         'runtime_dir': str(root / 'old'), 'kernel_enabled': False}}
        self.world = {'milk_1': {'grasped': False, 'pos': [0, 0], 'category': 'milk'}}
        self.zones = {'milk_area': {'pos': [1, 1], 'radius': .1}}
        self.sent = []
        self.force_failure = False
        def perform(text):
            self.sent.append(text)
            if self.force_failure:
                return {'success': False}
            self.world['milk_1'].update(grasped=text.startswith('抓取'),
                                       pos=[0, 0] if text.startswith('抓取') else [1, 1])
            return {'success': True}
        self.port = DeskAdapter(perform=perform, world=lambda: self.world, zones=self.zones)
        def factory(backend):
            return MockAdapter() if backend == 'reception_mock' else LookAdapter(
                lambda _: json.dumps(['视野描述（test）：桌上有牛奶', {'_status': 'success'}])) if backend == 'camera' else self.port
        model = lambda _: json.dumps({'reasoning_explanation': 'move milk', 'subtask_list': [
            {'robot_name': 'FQrobot', 'subtask': '抓取 milk_1'},
            {'robot_name': 'FQrobot', 'subtask': '放置 milk_1 到 milk_area'}]})
        self.service = create_service(self.config, model=model, port_factory=factory)
        self.service._launch = self.service._drive
        select = patch('brain.service.select_backend',
                       side_effect=lambda config, package, mock=False: 'reception_mock' if mock else 'camera' if package == 'look' else 'desk')
        identity = patch('brain.service.target_identity', side_effect=lambda c, b: {'backend': b})
        select.start(); identity.start()
        self.addCleanup(select.stop); self.addCleanup(identity.stop)

    def test_generic_runs_without_legacy_agent_or_collaborator(self):
        self.assertFalse(hasattr(self.service, 'agent'))
        self.service.publish('把牛奶放好', 'newbrain0001')
        self.assertEqual(self.service.status()['state'], 'succeeded')
        self.assertEqual(self.world['milk_1']['pos'], [1, 1])
        self.assertEqual(self.service.transports, [])
        self.assertEqual(self.service.runtime.record['reasoning_explanation'], 'move milk')
        self.assertEqual(self.sent, ['抓取 milk_1', '放置 milk_1 到 milk_area'])

    def test_failed_grasp_does_not_dispatch_place(self):
        self.force_failure = True
        self.service.publish('把牛奶放好', 'newbrain0002')
        self.assertEqual(self.service.status()['state'], 'recovery_required')
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.world['milk_1']['grasped'])

    def test_mock_reception_and_look_share_runtime(self):
        self.service.publish('开始接待', 'newbrain0003', options={'mock': True, 'headcount': 2})
        self.assertEqual(self.service.status()['state'], 'succeeded')
        self.service.publish('桌上有什么', 'newbrain0004')
        self.assertEqual(self.service.status()['state'], 'succeeded')
        self.assertEqual(self.service.runtime.record['dispatch_counts'], {})
        self.assertEqual(self.service.runtime.belief('scene_description')['certain'], '桌上有牛奶')

    def test_restart_queries_original_command_only(self):
        self.force_failure = True
        self.service.publish('把牛奶放好', 'newbrain0005')
        command = self.service.runtime.record['open_command_id']
        new_sends = []
        restarted = create_service(self.config, model=lambda _: '{}',
            port_factory=lambda _: DeskAdapter(perform=lambda command: new_sends.append(command)))
        restarted._launch = restarted._drive
        # Restoring/querying is distinct from an explicit human continue,
        # which now authorizes a new attempt after stop/resource checks.
        runtime = restarted.attach()
        runtime.resume()
        restarted._drive(runtime)
        self.assertEqual(restarted.status()['state'], 'recovery_required')
        self.assertEqual(restarted.runtime.record['open_command_id'], command)
        self.assertEqual(new_sends, [])


if __name__ == '__main__':
    unittest.main()
