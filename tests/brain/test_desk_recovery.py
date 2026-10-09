"""Regression for the persisted milk task: no-op navigation, restart and placement."""
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import Mock

from brain.adapters.execution import SimAdapter, parse_sim_action
from brain.adapters.ports import DeskAdapter
from brain.app import create_runtime, create_service
from brain.packages.desk import normalize_subtasks
from brain.packages.generic import steps_from_subtasks
from contracts.tasks import Rejected


class DeskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {'reception_real': {'kernel_runtime_dir': self.tmp.name}}
        self.world = {f'milk_{n}': {'pos': [0, 0, .75], 'category': 'milk', 'grasped': False} for n in [1, 2]}
        self.zones = {'milk_area': {'pos': [1, 1], 'radius': .1}}
        self.sent = []
        self.plan = [{'robot_name': 'FQrobot', 'subtask': text} for n in [1, 2] for text in
                     [f'抓取 milk_{n}', '导航到 milk_area', f'将 milk_{n} 放置到 milk_area']]

    def perform(self, text):
        self.sent.append(text)
        action = parse_sim_action(text)
        if action[0] == 'navigate':
            return {'success': True, 'result': '定点 demo,无需导航(no-op)'}
        obj = self.world[action[1]]
        if action[0] == 'grasp':
            if any(v['grasped'] for v in self.world.values()):
                return {'success': False, 'result': '手上已有物体'}
            obj['grasped'] = True
        else:
            if not obj['grasped']:
                return {'success': False, 'result': '未持有'}
            obj.update(grasped=False, pos=[1, 1, .75])
        return {'success': True, 'result': 'done'}

    def port(self, cls=DeskAdapter):
        return cls(perform=self.perform, world=lambda: self.world, zones=self.zones)

    def legacy_task(self):
        # Original adapter leaves Desk's completed, non-moving nav as UNKNOWN.
        runtime = create_runtime(self.config, self.port(SimAdapter), package='generic')
        runtime.open_task('milk-regression', task_desc='整理牛奶', phases=steps_from_subtasks(self.plan), execution_backend='desk')
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        self.assertTrue(self.world['milk_1']['grasped'])
        return runtime

    def test_original_task_resumes_after_restart_without_regrasp_or_new_attempt(self):
        previous = self.legacy_task()
        original = deepcopy(previous.record)
        service = create_service(self.config, model=Mock(side_effect=AssertionError('must not replan')),
                                 port_factory=lambda _: self.port())
        service.publish('整理牛奶', 'milk-regression', resume=True)
        final = service.runtime.record
        self.assertEqual(final['state'], 'succeeded')
        self.assertTrue(final['resources_cleared'])
        self.assertEqual(self.sent, [p['subtask'] for p in self.plan])
        self.assertTrue(all(v['pos'][:2] == [1, 1] and not v['grasped'] for v in self.world.values()))
        for step_id in original['steps']:
            self.assertEqual(final['steps'][step_id]['attempts'][0]['command_id'], original['steps'][step_id]['attempts'][0]['command_id'])
        self.assertTrue(all(len(s['attempts']) == 1 for s in final['steps'].values()))

    def test_missing_or_foreign_receipt_stays_unknown_even_when_world_looks_successful(self):
        previous = self.legacy_task()
        command = previous.record['open_command_id']
        request = previous._request_of(command)
        for mutation in ('identity', 'stopped', 'resources', 'backend', 'missing'):
            record = deepcopy(previous.record)
            progress = record['steps']['STEP_2']['attempts'][0]['progress']
            if mutation == 'identity': progress['evidence']['identity'] = 'foreign'
            if mutation == 'stopped': progress['publisher_stopped'] = False
            if mutation == 'resources': progress['evidence']['resources_released'] = False
            if mutation == 'backend': record['execution_backend'] = 'slaver:real'
            if mutation == 'missing': record['steps']['STEP_2']['attempts'][0].pop('progress')
            with self.subTest(mutation=mutation):
                port = self.port()
                port.restore_receipts(record)
                self.assertTrue(port.query(command, request)['timed_out'])
        self.assertEqual(len(self.sent), 2)

    def test_foreign_query_and_unknown_zone_do_not_reinterpret_noop_as_success(self):
        previous = self.legacy_task()
        command = previous.record['open_command_id']
        request = previous._request_of(command)
        port = self.port()
        port.restore_receipts(previous.record)
        self.assertTrue(port.query(command, dict(request, task_id='foreign'))['timed_out'])
        self.zones.clear()
        self.assertFalse(port.query(command, request)['evidence']['supports'])

    def test_complete_but_unverified_call_can_cancel_after_restart(self):
        previous = self.legacy_task()
        self.zones.clear()  # Unknown effect is not silently upgraded to success.
        recovered = create_runtime(self.config, self.port(), package='generic')
        self.assertTrue(recovered.request_cancel()['accepted'])
        recovered.settle_cancel()
        self.assertEqual(recovered.state, 'cancelled')
        self.assertEqual(len(self.sent), 2)

    def test_current_plan_normalizes_placement_and_omits_stationary_navigation(self):
        for text in ('将 milk_1 放置到 milk_area', '把 milk_1 放到 milk_area', '将 milk_1 放到 milk_area 上'):
            self.assertEqual(parse_sim_action(text), ('place', 'milk_1', 'milk_area'))
        result = normalize_subtasks(self.plan, self.world, self.zones)
        self.assertEqual([x['subtask'] for x in result], ['抓取 milk_1', '放置 milk_1 到 milk_area', '抓取 milk_2', '放置 milk_2 到 milk_area'])
        for text in ('去厨房煮牛奶', '导航到 kitchen', '放置 milk_1 到 unknown'):
            with self.assertRaises(Rejected):
                normalize_subtasks([*self.plan, {'subtask': text}], self.world, self.zones)

    def test_explicit_resume_does_not_overwrite_a_new_cancel(self):
        previous = self.legacy_task()
        port = self.port()
        recovered = create_runtime(self.config, port, package='generic')
        query = port.query
        def concurrent_query(*args):
            result = query(*args)
            # A newer cancellation races the I/O. It must keep dispatch closed.
            previous.request_cancel()
            return result
        port.query = concurrent_query
        recovered.resume()
        self.assertTrue(recovered.record['dispatch_closed'])
        recovered.drive()
        self.assertEqual(len(self.sent), 2)
