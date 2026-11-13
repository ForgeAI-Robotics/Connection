"""Tianji head-only GET and the G1 bounded JPEG/source-time validator."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brain.adapters.http_client import HttpContractError
from brain.adapters.tianji_camera import TianjiHeadCamera
from brain.skills.tianji_manipulation import TianjiManipulationSkill
from scripts.tianji_manipulation import main as atomic_cli
from scripts.tianji_manipulation_offline import fixture_server


class TianjiHeadCameraTests(unittest.TestCase):
    def setUp(self):
        self.context = fixture_server()
        self.fixture = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.skill = TianjiManipulationSkill(self.fixture['base_url'],
            token_env=self.fixture['token_env'], offline=True)

    def test_head_frame_gets_verified_jpeg_without_any_motion_or_cancel(self):
        metadata, raw = self.skill.capture_head(expected_robot_serial='fixture-tianji')
        self.assertEqual(metadata['camera_key'], 'ego_view')
        self.assertEqual(hashlib.sha256(raw).hexdigest(), metadata['sha256'])
        self.assertEqual((metadata['width'], metadata['height']), (16, 12))
        self.assertTrue(metadata['simulated'])
        self.assertNotIn('image_base64', metadata)
        self.assertEqual(self.fixture['posts'], [])
        self.assertEqual(self.fixture['cancels'], [])

    def test_real_camera_does_not_accept_fixture_image(self):
        camera = TianjiHeadCamera(self.fixture['base_url'], token_env=self.fixture['token_env'])
        with self.assertRaises(HttpContractError):
            camera.capture()

    def test_formal_metadata_requires_head_camera_capability(self):
        caps = self.skill.port.vla.capabilities()
        caps.pop('camera')
        with patch.object(self.skill.port.vla, 'capabilities', return_value=caps):
            with self.assertRaises(ValueError):
                self.skill.check()

    def test_offline_camera_rejects_unmarked_real_scope(self):
        self.fixture['camera_overrides'].update(simulated=False, test_fixture=False)
        with self.assertRaises(HttpContractError):
            self.skill.capture_head()

    def test_head_contract_rejects_wrong_view_schema_hash_format_and_dimensions(self):
        for change in ({'camera_key': 'left_wrist'}, {'schema': 'fq/vla-camera-frame/v2'},
                       {'sha256': 'wrong'}, {'content_type': 'image/png'}, {'width': 999},
                       {'image_base64': 'not-base64'}, {'frame_id': ''}):
            self.fixture['camera_overrides'].clear()
            self.fixture['camera_overrides'].update(change)
            with self.subTest(change=change), self.assertRaises(HttpContractError):
                self.skill.capture_head()

    def test_old_frame_cannot_be_refreshed_by_current_response_time(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
        self.fixture['camera_overrides'].update(captured_at=old, age_ms=0, received_age_ms=0)
        with self.assertRaises(HttpContractError):
            self.skill.capture_head()

    def test_expired_or_unknown_age_and_wrong_source_are_rejected(self):
        for change in ({'age_ms': 501}, {'received_age_ms': 501}, {'age_ms': None},
                       {'service_instance_id': ''}, {'camera_receiver_instance_id': None}, {'robot_serial': ''}):
            self.fixture['camera_overrides'].clear()
            self.fixture['camera_overrides'].update(change)
            with self.subTest(change=change), self.assertRaises(HttpContractError):
                self.skill.capture_head()

    def test_post_navigation_boundary_and_repeated_source_frame_are_checked(self):
        earlier = (datetime.now(timezone.utc) - timedelta(seconds=.1)).isoformat()
        self.skill.capture_head(captured_after=earlier)
        future = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        with self.assertRaises(HttpContractError):
            self.skill.capture_head(captured_after=future)
        self.fixture['camera_overrides']['frame_id'] = 'same-source-frame'
        with self.assertRaises(HttpContractError):
            self.skill.capture_head(previous_frame_id='same-source-frame')

    def test_robot_identity_mismatch_is_not_a_valid_observation(self):
        with self.assertRaises(HttpContractError):
            self.skill.capture_head(expected_robot_serial='different-robot')

    def test_clock_timezone_and_future_frame_are_rejected(self):
        for change in ({'captured_at': '2026-11-13T14:00:00'},
                       {'captured_at': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}):
            self.fixture['camera_overrides'].clear()
            self.fixture['camera_overrides'].update(change)
            with self.assertRaises(HttpContractError):
                self.skill.capture_head()

    def test_invalid_local_constraints_and_token_route_are_rejected(self):
        for constraint in ({'captured_after': 'bad-time'}, {'previous_frame_id': ''}, {'expected_robot_serial': ''}):
            with self.assertRaises(ValueError):
                self.skill.capture_head(**constraint)
        with self.assertRaises(ValueError):
            TianjiHeadCamera(self.fixture['base_url'], token_env='')

    def test_cli_saves_new_jpeg_and_source_metadata_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'before-grasp.jpg'
            args = ['head-frame', '--base-url', self.fixture['base_url'], '--offline',
                    '--token-env', self.fixture['token_env'], '--output', str(image)]
            with patch('builtins.print'):
                self.assertEqual(atomic_cli(args), 0)
            metadata = json.loads(image.with_suffix('.json').read_text())
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), metadata['sha256'])
            original = image.read_bytes()
            with patch('builtins.print'):
                self.assertEqual(atomic_cli(args), 2)
            self.assertEqual(image.read_bytes(), original)
        self.assertFalse(self.fixture['posts'])


if __name__ == '__main__':
    unittest.main()
