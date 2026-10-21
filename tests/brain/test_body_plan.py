from copy import deepcopy
import tempfile
import unittest

from brain.skills.body_plan import compile_plan, segments
from brain.packages.reception_skill import load_skill, BASELINE_PATH
from brain.packages.registry import PackagePolicy
from contracts.tasks import Rejected
from simulation.backends.simple_o7.service.reception_store import ReceptionStore, Conflict


class BodyPlanTests(unittest.TestCase):
    def setUp(self):
        self.skill = load_skill(path=BASELINE_PATH.with_name('reception_single_can_v2.yaml'))
        self.snapshot = self.skill.snapshot()

    def compile(self, actions):
        return compile_plan({'supported': True, 'unsupported_requirements': [], 'actions': actions}, self.snapshot)

    def test_navigation_and_pick_prefix(self):
        self.assertEqual(segments(self.compile([{'skill': 'navigate', 'target': 'table2'}]).steps), ['nav_table2'])
        result = self.compile([{'skill': 'pick', 'object': 'cola_can_1'}])
        self.assertEqual(segments(result.steps), ['nav_table2', 'pick'])
        self.assertTrue(result.steps[1].requires_object_evidence)
        body = PackagePolicy().request(result.steps[1], 'task', 'pick-id', lambda _: 'actual-nav-id')['body']
        self.assertEqual(body['navigation_proof']['dream_command_id'], 'actual-nav-id')

    def test_transfer_derives_same_route_and_handoff(self):
        result = self.compile([{'skill': 'pick', 'object': 'cola_can_1'},
                               {'skill': 'place', 'object': 'cola_can_1', 'target': 'table_1'}])
        self.assertEqual(segments(result.steps), ['nav_table2', 'pick', 'nav_relay1', 'nav_relay2', 'nav_relay3', 'nav_table1', 'place'])
        self.assertEqual(result.steps[3].handoff_before, 'to_nav')
        self.assertTrue(result.steps[-2].requires_safe_idle)
        self.assertEqual(result.steps[-2].navigation_step_id, 'BODY_NAV_TABLE1')
        sop_actions = [s for s in self.skill.steps if s.kind in {'navigate', 'pick', 'place'}]
        body_actions = [s for s in result.steps if s.kind in {'navigate', 'pick', 'place'}]
        for planned, sop in zip(body_actions, sop_actions):
            make = lambda s: PackagePolicy().request(s, 'task', 'command', lambda _: 'original-nav')['body']
            self.assertEqual(make(planned), make(sop))

    def test_refuses_unknown_skills_objects_coordinates_and_missing_holding(self):
        for actions in ([{'skill': 'open_door'}], [{'skill': 'pick', 'object': 'milk'}],
                        [{'skill': 'navigate', 'target': 'kitchen'}],
                        [{'skill': 'navigate', 'target': 'table2', 'goal_xyt': [0, 0, 0]}],
                        [{'skill': 'place', 'object': 'cola_can_1', 'target': 'table_1'}],
                        [{'skill': 'navigate', 'target': 'table1'}, {'skill': 'pick', 'object': 'cola_can_1'}]):
            with self.subTest(actions=actions), self.assertRaises(Rejected):
                self.compile(actions)

    def test_modified_snapshot_is_rejected(self):
        changed = deepcopy(self.snapshot)
        changed['definition']['points']['table2']['goal_xyt'][0] = 10
        with self.assertRaises(Rejected):
            compile_plan({'actions': [{'skill': 'navigate', 'target': 'table2'}]}, changed)

    def test_partial_plan_cannot_hide_an_unsupported_goal(self):
        actions = [{'skill': 'navigate', 'target': 'table2'}]
        for supported, unsupported in ((False, ['开灯']), (True, ['开灯']), (None, [])):
            with self.assertRaises(Rejected):
                compile_plan(dict(supported=supported, unsupported_requirements=unsupported,
                                  actions=actions), self.snapshot)

    def test_sim_plan_is_nonmoving_immutable_and_instance_bound(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReceptionStore(root, include_relay1=True)
            body = dict(task_id='generic-1', instance_id=store.instance_id, segments=['nav_table2'])
            self.assertTrue(store.prepare_plan(body)['accepted'])
            self.assertEqual(store.sessions(), [])
            self.assertEqual(store.prepare_plan(body), store.prepare_plan(body))
            for changed in (dict(body, instance_id='other'), dict(body, segments=['nav_table2', 'pick']),
                            dict(body, task_id='new', segments=['nav_table2', 'nav_table1'])):
                with self.assertRaises(Conflict):
                    store.prepare_plan(changed)
            from tests.brain.test_simple_reception import nav
            store.submit('nav', nav(task='generic-1'), lambda *_: 999)
            session = store.worker_session('generic-1')
            self.assertEqual(session['segments'], ['nav_table2'])
            self.assertTrue(session['navigation_only'])


if __name__ == '__main__':
    unittest.main()
