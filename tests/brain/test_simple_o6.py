"""O6 physical proof must differ from O7, and incomplete samples must not pass."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np

from simulation.backends.simple_o7.service.o6 import collect
from simulation.backends.simple_o7.service.store import Journal, Conflict
from simulation.backends.simple_o7.manage import manage
from brain.adapters.simple_o7 import SimpleO7Adapter, WIRE_VERSION, plan_steps


class O6Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.report = dict(task="simple/G1O6CanGraspMP-v0", task_passed=True, terminated=True,
                           truncated=False, stable_grasp_achieved=True, initial_target=[0, 0, 0],
                           base_tilt_degrees=3, frames=62)
        for key in ("self_penetration_m", "grasp_penetration_m", "object_environment_penetration_m",
                    "substep_max_self_penetration_m", "substep_max_target_penetration_m",
                    "substep_max_robot_environment_penetration_m"):
            self.report[key] = .001
        self.times = np.arange(62) * .02
        self.poses = np.tile([0, 0, .12, 1, 0, 0, 0], (62, 1))
        self.contacts = np.tile([False, True, False], (62, 1))

    def result(self):
        (self.root / 'report.json').write_text(json.dumps(self.report))
        np.savez(self.root / 'final_physics_state.npz', qpos=[0], qvel=[0], ctrl=[0])
        np.savez(self.root / 'diagnostic_trajectory.npz', time=self.times, object_pose=self.poses,
                 contact_state=self.contacts, contact_state_names=np.array(
                     ['target_table_contact', 'target_grasp_contact', 'receiver_contact']))
        return collect(self.root)

    def test_o6_native_proof_passes_without_o7_lowering_claim(self):
        result, sample, _ = self.result()
        self.assertTrue(result['pick_hold_passed'])
        self.assertNotIn('lowering_verified', result)
        self.assertTrue(self.view(result, sample)['evidence']['supports'])
        self.assertEqual(plan_steps({'subtask_list': [{'subtask': '抓取 can'}]}).steps[0].object_id, 'can')

    def view(self, result, sample):
        raw = dict(command_id='one', task_id='task', step_id='STEP_1', action='pick_hold_can', object_id='can',
                   state='succeeded', created_at='2026-09-24T10:00:00+00:00', updated_at='2026-09-24T10:01:00+00:00',
                   stopped=True, resources_released=True, result=result, observation=sample)
        return SimpleO7Adapter('http://test')._view('one', dict(task_id='task', step_id='STEP_1', body={'subtask': '抓取 can'}), raw)

    def test_reported_success_cannot_replace_contact_or_support_evidence(self):
        for terminal_contact in ([True, True, False], [False, False, False]):
            with self.subTest(contact=terminal_contact):
                self.contacts[-1] = terminal_contact
                result, _, _ = self.result()
                self.assertFalse(result['pick_hold_passed'])

    def test_brief_hold_or_ballistic_lift_cannot_pass(self):
        self.contacts[:30, 1] = False
        result, _, _ = self.result()
        self.assertFalse(result['pick_hold_passed'])
        self.contacts[:, 1] = True
        self.poses[-1, 2] += .01
        result, _, _ = self.result()
        self.assertFalse(result['pick_hold_passed'])

    def test_substep_collision_or_nan_cannot_hide_behind_good_frames(self):
        for value in (.004, float('nan')):
            self.report['substep_max_target_penetration_m'] = value
            result, sample, _ = self.result()
            self.assertFalse(result['pick_hold_passed'])
            self.assertFalse(self.view(dict(result, pick_hold_passed=True), sample)['evidence']['supports'])

    def test_task_mismatch_missing_samples_and_failed_report_do_not_pass(self):
        self.report['task_passed'] = False
        self.assertFalse(self.result()[0]['pick_hold_passed'])
        self.report['task'] = 'simple/G1O6CanPickMP-v0'
        with self.assertRaisesRegex(ValueError, 'task_mismatch'):
            self.result()
        self.report['task'] = 'simple/G1O6CanGraspMP-v0'
        self.times[-1] = self.times[-2]
        with self.assertRaisesRegex(ValueError, 'invalid_physical_samples'):
            self.result()

    def test_stop_guard_blocks_busy_service_and_new_admission(self):
        journal = Journal(self.root / 'runtime')
        request = dict(contract_version=WIRE_VERSION, command_id='one', task_id='task', step_id='STEP_1',
                       action='pick_hold_can', object_id='can', scene_revision='test')
        journal.create(request)
        runner = Mock()
        with self.assertRaises(Conflict):
            manage('stop', self.root, runner)
        runner.assert_not_called()
        journal.update('one', state='failed', stopped=True, resources_released=True)
        manage('stop', self.root, runner)
        runner.assert_called_once_with(['docker', 'stop', 'connection-simple-o7'], check=True, timeout=30)
        # Existing IDs still resolve while fenced; new commands cannot race a stop.
        self.assertFalse(journal.create(request)[1])
        with self.assertRaisesRegex(Conflict, 'executor_stopping'):
            journal.create(dict(request, command_id='two', task_id='other'))
        manage('start', self.root, runner)
        self.assertTrue(journal.create(dict(request, command_id='two', task_id='other'))[1])
