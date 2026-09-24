"""Profile dispatch tests: environment overrides cannot change the body protocol."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
from shared.execution_profile import resolve
from brain.adapters.ports import select_backend, target_identity, build_port, MotionGate
from contracts.tasks import Rejected


class ProfileRoutingTests(unittest.TestCase):
    def test_reception_profile_overrides_old_environment_but_not_motion_permission(self):
        profile = resolve({'mode': 'real'}, {})
        config = {'reception_real': {'kernel_enabled': False}}
        with patch('shared.execution_profile.applied_profile', return_value=profile), \
             patch.dict('os.environ', {'RECEPTION_MODE': 'mock'}), \
             patch('brain.adapters.ports.BodyAdapter.from_config') as body:
            self.assertEqual(select_backend(config, 'reception'), 'reception_real')
            port = build_port(config, 'reception_real')
            self.assertIsInstance(port, MotionGate)
            self.assertFalse(port.submit('nav-test', {})['accepted'])
            body.return_value.submit.assert_not_called()

    def test_global_real_refuses_unimplemented_modules(self):
        with patch('shared.execution_profile.applied_profile', return_value=resolve({'mode': 'real'}, {})):
            for package in ['generic', 'desk', 'look']:
                with self.assertRaisesRegex(Rejected, '不会回落仿真'):
                    select_backend({}, package)

    def test_disabled_reception_cannot_be_bypassed_by_demo(self):
        p = resolve({'modules': {'reception': {'mode': 'disabled'}}}, {'backends': {'desk': {'url': 'http://localhost:5008'}}})
        with patch('shared.execution_profile.applied_profile', return_value=p):
            with self.assertRaises(Rejected): select_backend({}, 'reception', mock=True)

    def test_camera_identity_pins_observation_source(self):
        raw = {'backends': {'mujoco': {'url': 'http://localhost:5001'}, 'mujoco_3dgs': {'url': 'http://localhost:5002'}}}
        first = resolve({'simulation_backend': 'mujoco'}, raw)
        second = resolve({'simulation_backend': 'mujoco_3dgs'}, raw)
        with patch('shared.execution_profile.applied_profile', return_value=first):
            a = target_identity({}, 'camera')
        with patch('shared.execution_profile.applied_profile', return_value=second):
            b = target_identity({}, 'camera')
        self.assertNotEqual(a, b)
