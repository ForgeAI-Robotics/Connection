"""Office geometry identity and physical-reception interface regression."""
import json
from pathlib import Path
import tempfile
import unittest

from simulation.backends.simple_o7.service.office_layout import office_to_local, prepare_room, SIM_GOALS
from simulation.backends.simple_o7.service.reception_http import Reception
from simulation.backends.simple_o7.service.reception_worker import nav_result
from simulation.backends.simple_o7.service.render_reception import attempt_ranges


class OfficeSceneTests(unittest.TestCase):
    def test_video_keeps_failed_attempt_separate_from_successful_retry(self):
        failed = dict(segment='nav_relay2', frames=10, outcome='succeeded', checks={'reached': False})
        passed = dict(segment='nav_relay2', frames=4, outcome='succeeded', checks={'reached': True})
        report = {'segments': [failed, passed]}
        commands = {'nav_relay2': {'attempt_count': 2, 'attempts': [
            {'command_id': 'original'}, {'command_id': 'original-r1', 'verdict': 'PASS'}]}}
        ranges = attempt_ranges(report, commands)
        self.assertEqual([(r[0], r[1]) for r in ranges], [(0, 10), (10, 14)])
        self.assertFalse(ranges[0][2]['checks']['reached'])
        self.assertEqual([r[3]['command_id'] for r in ranges], ['original', 'original-r1'])
        self.assertNotIn('verdict', ranges[0][3])
        legacy = attempt_ranges(report, {'nav_relay2': {'command_id': 'original-r1', 'attempt_count': 2}})
        self.assertEqual([r[3] for r in legacy], [{}, {}])

    def test_office_frame_preserves_height_and_meeting_door_position(self):
        self.assertEqual(office_to_local([10.8, 11.55, .75]), [0., 0., 0.])
        local = office_to_local([6.25, 11.82727, .75])
        for got, expected in zip(local[:2], SIM_GOALS['relay2'][:2]):
            self.assertAlmostEqual(got, expected)

    def test_manifest_uses_authored_collision_dimensions_without_visual_obstacles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'scene.xml'
            xml = '''<mujoco><worldbody>
              <geom name="floor" type="box" pos="0 0 -.05" size="10 10 .05"/>
              <geom name="meeting_table" type="box" pos="2 10 .375" size="2 .6 .375"/>
              <geom name="door" type="box" pos="6.25 11.55 1" size=".1 .4 1"/>
              <geom name="paint" type="box" pos="0 0 1" size=".1 .4 1" contype="0" conaffinity="0"/>
            </worldbody></mujoco>'''
            source.write_text(xml)
            room = json.loads(prepare_room(root, root).read_text())
            self.assertEqual([b['name'] for b in room['boxes']], ['office_door'])
            self.assertEqual(room['boxes'][0]['size'], [.8, .2, 2.])
            for got, expected in zip(room['boxes'][0]['position'], [0., 4.55, .25]):
                self.assertAlmostEqual(got, expected)
            self.assertEqual(len(room['source_sha256']), 64)
            self.assertEqual(source.read_text(), xml)

    def test_office_health_and_world_identify_selected_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'scene.xml').write_text('<mujoco/>')
            reception = Reception({'runtime': tmp, 'reception_scene': 'office_v2',
                                   'reception_office_assets': tmp})
            for role in ('nav', 'vla'):
                _, health = reception.health(role)
                self.assertEqual(health['simulation']['scene'], 'office_v2')
                self.assertEqual(len(health['simulation']['scene_source_sha256']), 64)
                self.assertEqual(health['simulation']['semantic_targets']['table_1']['stance_xyt'],
                                 list(SIM_GOALS['table_1']))
            _, world = reception.handle('GET', '/reception/nav/v1/world', None, {})
            self.assertEqual(world['simulation_frame'], 'simple_office_v2')

    def test_unknown_or_unconfigured_scene_is_rejected(self):
        for options in ({'reception_scene': 'missing'}, {'reception_scene': 'office_v2'}):
            with self.assertRaises(ValueError):
                Reception({'runtime': '/unused', **options})

    def test_failed_office_navigation_keeps_measured_failure_and_frame(self):
        entry = {'scene': 'office_v2', 'outcome': 'failed', 'error': 'not_at_goal',
                 'segment': 'nav_relay3', 'frames': 12, 'phase_events': [],
                 'after': {'base_xyt': [0, 0, 0], 'sim_time': 1},
                 'checks': {'reached': False, 'object_retained': True, 'sim_target': 'relay3',
                            'sim_goal_xyt': [1, 2, 0], 'position_error_m': 2., 'yaw_error_rad': 0.}}
        ok, result, error = nav_result(entry)
        self.assertFalse(ok)
        self.assertFalse(result['reached'])
        self.assertEqual(result['simulation']['frame'], 'simple_office_v2')
        self.assertEqual(error[0], 'NAVIGATION_EXECUTION_FAILED')


if __name__ == '__main__':
    unittest.main()
