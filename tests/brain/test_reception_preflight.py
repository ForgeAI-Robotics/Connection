import copy
import math
import unittest
from unittest.mock import Mock, patch

from brain.adapters.preflight import compare_navigation_contract, reception_preflight
from brain.packages.reception_skill import load_skill
from brain.adapters.http_client import HttpContractError


class ReceptionPreflightTests(unittest.TestCase):
    def setUp(self):
        self.config = {'reception_real': {'dream_base_url': 'http://nav.invalid',
                         'skill_file': 'src/brain/packages/definitions/reception_single_can_v2.yaml'}}
        self.skill = load_skill(self.config)
        self.contract = {'contract_version': self.skill.definition['contract_version'], 'frame_id': 'map',
                         'legs': [dict(p, require_final_orientation=True) for p in self.skill.definition['points'].values()]}

    def compare(self, contract):
        return compare_navigation_contract(self.skill.definition['points'], contract, self.skill.definition['contract_version'])

    def test_legacy_route_and_heading_mismatch_are_found_before_motion(self):
        contract = copy.deepcopy(self.contract)
        contract['legs'][0]['goal_xyt'][2] += math.pi
        contract['legs'].pop(1)
        blockers, _ = self.compare(contract)
        self.assertTrue(any('table2' in s and '朝向' in s for s in blockers))
        self.assertTrue(any('relay1' in s for s in blockers))

    def test_independent_goals_do_not_reintroduce_route_or_coordinate_enforcement(self):
        c = copy.deepcopy(self.contract)
        c['goal_admission'] = {'strategy': 'independent_map_goal', 'reviewed_coordinates_enforced': False,
                               'route_metadata_required': False, 'route_order_enforced': False}
        c['legs'][0]['goal_xyt'][1] += .4
        c['legs'].pop(1)
        blockers, warnings = self.compare(c)
        self.assertEqual(blockers, [])
        self.assertTrue(any('table2' in s for s in warnings))
        self.assertTrue(any('relay1' in s for s in warnings))
        c['frame_id'] = 'odom'
        self.assertTrue(self.compare(c)[0])

    def test_rounding_and_wrapped_yaw_match_but_nonfinite_does_not(self):
        c = copy.deepcopy(self.contract)
        c['legs'][0]['goal_xyt'][0] += 1e-7
        c['legs'][0]['goal_xyt'][2] += 2*math.pi
        self.assertEqual(self.compare(c)[0], [])
        c['legs'][0]['goal_xyt'][1] = float('nan')
        self.assertTrue(self.compare(c)[0])
        self.assertTrue(self.compare(None)[1])
        c['goal_admission'] = 'broken'
        self.assertTrue(self.compare(c)[0])

    @patch('brain.adapters.preflight.select_backend', return_value='reception_real')
    def test_preflight_reads_only_and_does_not_require_arm_to_submit_goal(self, _):
        client = Mock()
        client.status.return_value = {'interface_online': True, 'contract_version': self.contract['contract_version'],
                                      'motion_ready': False, 'motion_blockers': ['WAITING_OPERATOR_APPROVAL']}
        client.http.request_json.return_value = ({'agent_navigation_contract': self.contract}, 200, {})
        factory = Mock(return_value=client)
        before = copy.deepcopy(self.config)
        r = reception_preflight(self.config, '开始接待（导航真机＋操控模拟）', client_factory=factory)
        self.assertTrue(r['ready']); self.assertTrue(r['checked']); self.assertTrue(r['required'])
        self.assertEqual(self.config, before)
        self.assertTrue(r['warnings'])
        client.status.assert_called_once_with()
        client.http.request_json.assert_called_once_with('GET', '/total_scene_graph_latest.json')
        self.assertEqual(len(client.mock_calls), 2)
        client.http.request_json.return_value = ({'agent_navigation_contract': dict(self.contract, legs='invalid')}, 200, {})
        r = reception_preflight(self.config, '开始接待', client_factory=factory)
        self.assertFalse(r['checked'])
        self.assertTrue(any('未核实' in warning for warning in r['warnings']))
        client.status.side_effect = HttpContractError('offline')
        r = reception_preflight(self.config, '开始接待', client_factory=factory)
        self.assertFalse(r['ready']); self.assertIn('offline', r['blockers'][0])

    @patch('brain.adapters.preflight.select_backend', return_value='reception_simple')
    def test_simulation_and_non_reception_do_not_contact_real_navigation(self, _):
        factory = Mock()
        for task in ('查看现场', '开始接待（导航仿真＋操控模拟）'):
            r = reception_preflight(self.config, task, client_factory=factory)
            self.assertFalse(r['required']); self.assertFalse(r['checked'])
        factory.assert_not_called()
