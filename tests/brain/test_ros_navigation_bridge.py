from copy import deepcopy
import unittest
import json
from pathlib import Path
import tempfile
from contracts.reception_lan import NAVIGATION_LEGS
from simulation.backends.ros_navigation.bridge import Bridge, translate


class NavigationBridgeTests(unittest.TestCase):
    def setUp(self):
        self.goals = {str(i): [float(i), 11.8, 1.57] for i in range(1, 5)}
        self.body = dict(contract_version='fq/reception-lan/v1', task_id='task', command_id='command',
                         frame_id='map', require_final_orientation=True, **deepcopy(NAVIGATION_LEGS['relay3']))

    def test_only_scene_coordinates_change(self):
        result = translate(self.body, NAVIGATION_LEGS, self.goals)
        self.assertEqual(result['goal_xyt'], self.goals['3'])
        self.assertEqual({k: v for k, v in result.items() if k != 'goal_xyt'},
                         {k: v for k, v in self.body.items() if k != 'goal_xyt'})
        self.assertEqual(self.body['goal_xyt'], NAVIGATION_LEGS['relay3']['goal_xyt'])

    def test_wrong_mode_frame_goal_or_missing_orientation_never_forwarded(self):
        for key, value in [('motion_mode', 'forward_path'), ('frame_id', 'odom'),
                           ('goal_xyt', [0, 0, 0]), ('require_final_orientation', False),
                           ('command_id', '../unsafe'), ('leg_index', True)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                translate({**self.body, key: value}, NAVIGATION_LEGS, self.goals)

    def test_manipulation_is_rejected_without_upstream_call(self):
        bridge = Bridge({'instance_id': 'test'})
        bridge.upstream = lambda *_: self.fail('Must not submit manipulation')
        code, result = bridge.handle('POST', '/reception/vla/v1/tasks', {})
        self.assertEqual(code, 501)
        self.assertEqual(result['error']['code'], 'UNSUPPORTED_CAPABILITY')

    def test_failed_command_and_unknown_result_are_not_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge({'instance_id': 'test', 'events': str(Path(tmp) / 'events.jsonl')})
            for code, raw in [(200, {'state': 'failed', 'command_id': 'command',
                                    'result': {'success': False, 'reached': False}}),
                              (404, {'error': {'code': 'COMMAND_NOT_FOUND'}})]:
                original = deepcopy(raw)
                bridge.upstream = lambda *_, c=code, v=raw: (c, deepcopy(v))
                actual_code, actual = bridge.handle('GET', '/reception/nav/v1/commands/command')
                self.assertEqual(actual_code, code)
                actual.pop('simulation')
                self.assertEqual(actual, original)
            events = [json.loads(line) for line in (Path(tmp) / 'events.jsonl').read_text().splitlines()]
            self.assertEqual([row['status'] for row in events], [200, 404])


if __name__ == '__main__':
    unittest.main()
