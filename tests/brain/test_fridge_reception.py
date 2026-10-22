"""Reception keeps the original delivery route after verified open/grasp."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brain.app import create_service
from brain.adapters.execution import BodyAdapter
from brain.packages.reception_skill import BASELINE_PATH, compile_skill, from_snapshot, load_skill
from brain.packages.registry import PackagePolicy, reception_catalog
from contracts.tasks import Rejected
from tests.brain.test_native_vla import FakeDream, FakeVla, capabilities, evidence


class FridgeDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.skill = load_skill(path=BASELINE_PATH.with_name('reception_single_can_v2.yaml'))

    def test_one_reviewed_point_and_same_original_navigation_proof(self):
        steps = self.skill.phases(real=True)
        actions = [s for s in steps if s.kind not in {'local', 'verify'}]
        self.assertEqual([s.kind for s in actions], ['navigate', 'open_fridge', 'grasp_cola'] + ['navigate'] * 4)
        self.assertEqual([s.key for s in actions if s.kind == 'navigate'],
                         ['table2','relay1','relay2','relay3','table1'])
        self.assertEqual(actions[0].navigation, self.skill.navigation_points['table2'])
        self.assertEqual(actions[0].navigation['goal_xyt'], [-3.658864, 4.024073, -2.963158])
        self.assertEqual(actions[0].display_name, '导航到冰箱前')
        self.assertEqual(actions[1].navigation_step_id, actions[2].navigation_step_id)
        bodies = [PackagePolicy().request(s, 'task', 'command', lambda _: 'original-nav')['body'] for s in actions[1:3]]
        self.assertEqual(bodies[0]['navigation_proof'], bodies[1]['navigation_proof'])
        self.assertEqual(bodies[0]['target_area'], 'table_2')
        self.assertEqual([b['object_id'] for b in bodies], ['fridge_1', 'cola_can_1'])
        self.assertTrue(all(s.requires_safe_idle and s.requires_object_evidence for s in actions[1:3]))
        self.assertEqual(actions[2].writes, 'in_gripper')
        self.assertTrue(all(s.required_holding == 'cola_can_1' for s in actions[3:]))
        self.assertTrue(actions[-1].requires_safe_idle)

    def test_definition_rejects_extra_navigation_skipped_open_and_wrong_proof(self):
        for mutate in (
            lambda d: d['real_workflow']['steps'].insert(2, deepcopy(d['real_workflow']['steps'][0])),
            lambda d: d['real_workflow']['steps'].pop(1),
            lambda d: d['real_workflow']['steps'][2].update(action='place'),
            lambda d: d['real_workflow']['steps'][2].update(after_navigation='other-nav'),
            lambda d: d['real_workflow']['steps'][0].update(point='table1'),
            lambda d: d['real_workflow']['steps'][2].update(object_id='other_can'),
            lambda d: d['real_workflow']['steps'][1].update(verification_id='VLA_GRASP_COLA'),
            lambda d: d['real_workflow']['steps'].pop(),
            lambda d: d['real_workflow']['steps'][3].update(point='relay2'),
        ):
            definition = deepcopy(self.skill.definition)
            mutate(definition)
            with self.assertRaises(Rejected): compile_skill(definition)

    def test_old_snapshot_keeps_legacy_steps_and_new_snapshot_is_frozen(self):
        definition = deepcopy(self.skill.definition)
        definition.pop('real_workflow')
        old = compile_skill(definition)
        self.assertFalse(from_snapshot(old.snapshot()).real_steps)
        self.assertIn('place', [s.kind for s in old.phases()])
        snapshot = self.skill.snapshot()
        definition = deepcopy(self.skill.definition)
        definition['points']['table2']['goal_xyt'][0] += 1
        self.assertEqual(from_snapshot(snapshot).real_steps, self.skill.real_steps)
        historical = deepcopy(self.skill.definition)
        historical['real_workflow']['sop']['version'] = '1'
        historical['real_workflow']['steps'] = historical['real_workflow']['steps'][:3]
        frozen = compile_skill(historical).snapshot()
        self.assertEqual([s.kind for s in from_snapshot(frozen).real_steps if s.kind not in {'local','verify'}],
                         ['navigate','open_fridge','grasp_cola'])


class FridgeReceptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {key:str(root / name) for key,name in (
            ('FQ_EXECUTION_STATE','execution.json'), ('FQ_EXECUTION_LOCK','execution.lock'),
            ('FQ_EXECUTION_BLOCK','execution.blocked'))})
        env.start(); self.addCleanup(env.stop)
        self.config = {'brain': {'reception_backend':'real', 'capture_timeline':False,
                                'reasoning':{'recovery_advice':False}},
            'reception_real': {'kernel_runtime_dir':str(root/'tasks'), 'kernel_enabled':True,
                'skill_file':str(BASELINE_PATH.with_name('reception_single_can_v2.yaml')),
                'dream_base_url':'http://nav.invalid', 'vla_base_url':'http://vla.invalid'},
            'reflection':{'enabled':False}}
        self.nav, self.vla = FakeDream(), FakeVla()
        self.port = BodyAdapter(self.nav, self.vla)
        self.service = self.build()

    def build(self):
        return create_service(self.config, model=lambda _: self.fail('Exact reception must not call a model'),
                              port_factory=lambda _:self.port)

    def publish(self):
        return self.service.publish('开始接待', 'fridge-reception', options={'reflect':False})

    def finish_current(self):
        command = self.service.runtime.record['open_command_id']
        self.service.vla_effect('fridge-reception', command, 'pause')
        self.service.vla_effect('fridge-reception', command, 'finish',
            evidence=evidence(self.vla.task(command)['operation']), authorization='Bearer test-only')

    def test_start_reception_runs_open_and_grasp_without_second_navigation(self):
        self.publish()
        runtime = self.service.runtime
        self.assertEqual(runtime.state, 'waiting_human', runtime.public_status())
        self.assertEqual(runtime.record['selection']['sop']['id'], 'reception.fridge_pick')
        self.assertEqual(runtime.record['selection']['workflow_variant'], 'real')
        self.assertEqual([p['operation'] for p in self.vla.posts], ['open_fridge'])
        self.finish_current()
        self.service.control('continue', task_id='fridge-reception')
        self.assertEqual(runtime.state, 'waiting_human')
        self.assertEqual([p['operation'] for p in self.vla.posts], ['open_fridge','grasp_cola'])
        self.assertEqual(len(self.nav.commands), 1)
        self.assertEqual(self.vla.posts[0]['navigation_proof'], self.vla.posts[1]['navigation_proof'])
        self.finish_current()
        self.service.control('continue', task_id='fridge-reception')
        self.assertEqual(runtime.state, 'succeeded', runtime.public_status())
        self.assertEqual(runtime.record['holding'], 'cola_can_1')
        self.assertEqual(len(self.nav.commands), 5)
        self.assertEqual([r['leg_index'] for r in self.nav.commands.values()], [1,2,3,4,5])
        self.assertEqual(len(self.vla.posts), 2)

    def test_skipped_grasp_cannot_start_delivery(self):
        self.publish(); self.finish_current()
        self.service.control('continue', task_id='fridge-reception')
        runtime = self.service.runtime
        runtime.request_pause()
        runtime.skip_current(step_id='VLA_GRASP_COLA')
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        self.assertEqual(len(self.nav.commands), 1)
        self.assertIn('持物', runtime.record['blocked_reason'])

    def test_vla_lost_holding_blocks_delivery(self):
        self.publish(); self.finish_current()
        self.service.control('continue', task_id='fridge-reception')
        self.finish_current()
        self.vla.status_override['holding'] = None
        self.service.control('continue', task_id='fridge-reception')
        self.assertEqual(self.service.status()['state'], 'recovery_required')
        self.assertEqual(len(self.nav.commands), 1)

    def test_delivery_navigation_failure_blocks_remaining_route(self):
        original = self.nav.submit_navigation
        def submit(body):
            response = original(body)
            if body['leg_index'] == 3:
                self.nav.commands[body['command_id']].update(state='failed',
                    result=dict(success=False, reached=False, navigation_stopped=True))
            return response
        with patch.object(self.nav, 'submit_navigation', side_effect=submit):
            self.publish(); self.finish_current()
            self.service.control('continue', task_id='fridge-reception')
            self.finish_current()
            self.service.control('continue', task_id='fridge-reception')
        self.assertEqual(self.service.status()['state'], 'recovery_required')
        self.assertEqual([r['leg_index'] for r in self.nav.commands.values()], [1,2,3])
        self.assertEqual(self.service.runtime.record['holding'], 'cola_can_1')

    def test_final_arrival_with_lost_holding_cannot_report_success(self):
        original = self.nav.submit_navigation
        def submit(body):
            result = original(body)
            if body['leg_index'] == 5:
                self.vla.status_override['holding'] = None
            return result
        with patch.object(self.nav, 'submit_navigation', side_effect=submit):
            self.publish(); self.finish_current()
            self.service.control('continue', task_id='fridge-reception')
            self.finish_current()
            self.service.control('continue', task_id='fridge-reception')
        self.assertEqual(len(self.nav.commands), 5)
        self.assertEqual(self.service.status()['state'], 'recovery_required')

    def test_untrained_grasp_is_rejected_before_any_navigation(self):
        caps = capabilities(); caps['operations']['grasp_cola']['configured'] = False
        with patch.object(self.vla, 'capabilities', return_value=caps): self.publish()
        self.assertEqual(self.service.status()['state'], 'recovery_required')
        self.assertEqual(self.nav.commands, {})
        self.assertEqual(self.vla.posts, [])

    def test_legacy_vla_cannot_silently_receive_new_reception(self):
        with patch.object(self.vla, 'capabilities', return_value=None): self.publish()
        self.assertEqual(self.service.status()['state'], 'recovery_required')
        self.assertEqual(self.nav.commands, {})
        self.assertEqual(self.vla.posts, [])

    def test_cancel_and_skipped_open_do_not_start_grasp(self):
        self.publish()
        runtime = self.service.runtime
        runtime.request_pause()
        runtime.skip_current(step_id='VLA_OPEN_FRIDGE')
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        self.assertEqual([p['operation'] for p in self.vla.posts], ['open_fridge'])
        self.service.control('cancel', task_id='fridge-reception')
        self.assertEqual(self.service.status()['state'], 'cancelled')

    def test_restart_does_not_reload_point_or_repeat_navigation(self):
        self.publish(); self.finish_current()
        snapshot = deepcopy(self.service.runtime.record['selection'])
        self.config['reception_real']['skill_file'] = str(Path(self.temp.name)/'missing.yaml')
        self.service = self.build()
        self.service.control('continue', task_id='fridge-reception')
        self.assertEqual(self.service.runtime.record['selection'], snapshot)
        self.assertEqual(len(self.nav.commands), 1)
        self.assertEqual([p['operation'] for p in self.vla.posts], ['open_fridge','grasp_cola'])

    def test_catalog_and_explicit_hybrid_share_workflow_but_distinguish_backend(self):
        sops, variants, skill = reception_catalog(self.config)
        self.assertEqual(sops['reception']['id'], 'reception.fridge_pick')
        self.assertEqual(reception_catalog(self.config, mock=True)[0]['reception']['id'], 'reception.single_can')
        chosen = self.service.reasoner.select('开始接待（导航真机＋操控模拟）', {})
        self.assertTrue(chosen['sop']['id'].endswith('.sim_manipulation'))
        self.assertEqual(chosen['workflow_variant'], 'real_sim_manipulation')
        self.assertEqual(chosen['sop']['id'], skill.real_sop['id'] + '.sim_manipulation')
        self.assertIn('place', [s.kind for s in skill.phases()])


if __name__ == '__main__': unittest.main()
