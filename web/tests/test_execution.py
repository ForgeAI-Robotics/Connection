import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from common.execution_profile import resolve, applied_profile, admission
from web.execution import Switcher, assert_idle


ROBOT = {'active_backend': 'desk', 'backends': {
    'desk': {'url': 'http://localhost:5008', 'enabled': True, 'provide_state': True, 'accept_action': True},
    'mujoco': {'url': 'http://localhost:5001', 'enabled': False},
    'mujoco_3dgs': {'url': 'http://localhost:5002', 'enabled': True}}}


class FakeServices:
    def __init__(self):
        self.up = {'redis', 'master', 'deploy', 'feishu', 'slaver', 'desk'}
        self.calls = []
        self.fail = None
        self.on_stop = None
        self.task = {'active': False}

    def running(self, n): return n in self.up
    def status(self): return self.task
    def stop(self, n):
        self.calls.append(('stop', n))
        self.up.discard(n)
        if self.on_stop: self.on_stop(n)
    def start(self, n):
        self.calls.append(('start', n))
        self.up.add(n)
    def targets_healthy(self, routes): pass
    def healthy(self, names, revision=None):
        self.calls.append(('healthy', tuple(names)))
        if revision and self.fail:
            raise RuntimeError(self.fail)


class ExecutionSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for d in ['master', 'robot_api', 'config']:
            (self.root / d).mkdir()
        (self.root / 'master/config.yaml').write_text('brain: {scheduler: runtime}\nreception_real: {kernel_enabled: false}\n')
        (self.root / 'robot_api/config.yaml').write_text(yaml.safe_dump(ROBOT))
        self.services = FakeServices()
        self.switcher = Switcher(self.root, self.services)
        self.ledger = self.root / 'master/sop/runtime/kernel/current_task.json'

    def test_redis_clear_configuration_is_rejected(self):
        (self.root / 'master/config.yaml').write_text('collaborator: {clear: true}\n')
        with self.assertRaisesRegex(ValueError, '清空 Redis'):
            self.switcher.apply({'mode': 'simulation'})
        self.assertEqual(self.services.calls, [])

    def test_global_and_module_overrides(self):
        result = resolve({'mode': 'real', 'modules': {
            'execution': {'mode': 'simulation', 'simulation_backend': 'desk'},
            'observation': {'mode': 'simulation', 'simulation_backend': 'mujoco_3dgs'}}}, ROBOT)
        self.assertEqual(result['routes']['reception']['backend'], 'reception_real')
        self.assertEqual(result['routes']['execution']['backend'], 'desk')
        self.assertEqual(result['routes']['observation']['backend'], 'mujoco_3dgs')

    def test_real_never_falls_back_to_simulation(self):
        routes = resolve({'mode': 'real'}, ROBOT)['routes']
        self.assertFalse(routes['execution']['available'])
        self.assertFalse(routes['observation']['available'])
        self.assertEqual(routes['execution']['mode'], 'real')
        self.assertTrue(routes['reception']['available'])

    def test_desk_does_not_claim_a_camera(self):
        self.assertFalse(resolve({'mode': 'simulation'}, ROBOT)['routes']['observation']['available'])

    def test_typos_fail_before_services_change(self):
        for config in [{'mode': 'sim'}, {'moduels': {}}, {'modules': {'execution': {'backed': 'desk'}}}]:
            with self.assertRaises(ValueError): self.switcher.apply(config)
        self.assertEqual(self.services.calls, [])

    def test_switch_refuses_every_unresolved_state(self):
        for state in ['running', 'paused', 'waiting_human', 'verifying', 'cancelling', 'recovery_required']:
            self.ledger_record({'state': state})
            with self.assertRaises(ValueError): self.switcher.apply({'mode': 'simulation'})
        self.assertEqual(self.services.calls, [])

    def ledger_record(self, record):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(record))

    def test_unknown_command_blocks_even_terminal_label(self):
        with self.assertRaises(ValueError): assert_idle({'state': 'failed', 'command_unknown': True})

    def test_old_reception_ledger_is_checked(self):
        p = self.root / 'master/sop/runtime/reception/current_task.json'
        p.parent.mkdir(parents=True); p.write_text(json.dumps({'state': 'RECOVERY_REQUIRED'}))
        with self.assertRaises(ValueError): self.switcher.apply({'mode': 'simulation'})
        self.assertEqual(self.services.calls, [])

    def test_apply_keeps_redis_remote_and_permission_unchanged(self):
        before = (self.root / 'master/config.yaml').read_text()
        result = self.switcher.apply({'mode': 'simulation', 'simulation_backend': 'mujoco'})
        self.assertEqual(json.loads(self.switcher.state.read_text()), result)
        self.assertEqual((self.root / 'master/config.yaml').read_text(), before)
        changes = [name for action, name in self.services.calls if action != 'healthy']
        self.assertNotIn('redis', changes)
        self.assertNotIn('dream', changes)
        self.assertNotIn('vla', changes)
        self.assertIn('mujoco', changes)
        self.assertFalse(self.switcher.block.exists())

    def test_second_ledger_check_catches_old_process_admission(self):
        self.services.on_stop = lambda n: self.ledger_record({'state': 'running'}) if n == 'master' else None
        with self.assertRaisesRegex(RuntimeError, '已恢复'):
            self.switcher.apply({'mode': 'real'})
        self.assertFalse(self.switcher.state.exists())
        self.assertEqual(self.services.up, {'redis', 'master', 'deploy', 'feishu', 'slaver', 'desk'})

    def test_failed_health_restores_previous_profile(self):
        old = self.switcher.apply({'mode': 'simulation'})
        # Fail the new generation only, allow verification of rollback.
        original = self.services.healthy
        self.services.healthy = lambda names, revision=None: (_ for _ in ()).throw(RuntimeError('bad health')) if revision and revision != old['revision'] else original(names, revision)
        with self.assertRaisesRegex(RuntimeError, '已恢复'):
            self.switcher.apply({'mode': 'simulation', 'simulation_backend': 'mujoco'})
        self.assertEqual(json.loads(self.switcher.state.read_text()), old)
        self.assertNotIn('mujoco', self.services.up)
        self.assertFalse(self.switcher.block.exists())

    def test_wrong_target_address_rolls_back_before_activation(self):
        self.services.targets_healthy = lambda routes: (_ for _ in ()).throw(RuntimeError('selected target offline'))
        with self.assertRaisesRegex(RuntimeError, 'selected target offline'):
            self.switcher.apply({'mode': 'simulation'})
        self.assertFalse(self.switcher.state.exists())

    def test_resource_release_is_required_even_after_terminal_label(self):
        with self.assertRaises(ValueError):
            assert_idle({'state': 'cancelled', 'resources_cleared': False})

    def test_failed_rollback_blocks_new_tasks(self):
        self.switcher.apply({'mode': 'simulation'})
        self.services.fail = 'unhealthy'
        with self.assertRaisesRegex(RuntimeError, '回退未完成'):
            self.switcher.apply({'mode': 'real'})
        self.assertTrue(self.switcher.block.exists())

    def test_admission_cannot_overlap_switch(self):
        lock = self.root / 'lock'
        with patch.dict('os.environ', {'FQ_EXECUTION_LOCK': str(lock), 'FQ_EXECUTION_BLOCK': str(self.root / 'blocked')}):
            with lock.open('a') as fd:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, '正在切换'):
                    with admission(): pass

    def test_applied_profile_wins_over_old_environment_and_pins_camera(self):
        from robot_api.config import load_robot_api_config
        from robot_api.runtime import RobotRuntime
        profile = resolve({'mode': 'simulation', 'modules': {
            'observation': {'simulation_backend': 'mujoco_3dgs'}}}, ROBOT)
        profile['revision'] = 'test'
        with patch('common.execution_profile.applied_profile', return_value=profile), \
             patch('robot_api.config._read_yaml', return_value=ROBOT), \
             patch.dict('os.environ', {'ROBOT_API_BACKEND': 'real', 'ROBOT_API_URL': 'http://wrong-body'}):
            config = load_robot_api_config()
            self.assertEqual(config.active_backend, 'desk')
            self.assertEqual(config.server_url, 'http://localhost:5008')
            runtime = RobotRuntime()
            self.assertEqual([b.name for b in runtime._vision_backends()], ['mujoco_3dgs'])
            self.assertIsNone(config.navigation)

    def test_disabled_real_has_no_action_or_vision_backend(self):
        from robot_api.config import load_robot_api_config
        from robot_api.runtime import RobotRuntime
        profile = resolve({'mode': 'real'}, ROBOT)
        with patch('common.execution_profile.applied_profile', return_value=profile), patch('robot_api.config._read_yaml', return_value=ROBOT):
            cfg = load_robot_api_config()
            self.assertEqual(cfg.action_backends(), [])
            self.assertEqual(RobotRuntime()._vision_backends(), [])
            self.assertFalse(RobotRuntime().execute('grasp_object', {'object_name': 'milk_1'})['success'])

    def test_cached_profile_does_not_hot_reload(self):
        file = self.root / 'applied.json'
        first = {'revision': 'a', 'routes': {'execution': {}}}
        file.write_text(json.dumps(first))
        with patch.dict('os.environ', {'FQ_EXECUTION_STATE': str(file)}):
            applied_profile.cache_clear()
            try:
                self.assertEqual(applied_profile()['revision'], 'a')
                file.write_text(json.dumps({'revision': 'b', 'routes': {'execution': {}}}))
                self.assertEqual(applied_profile()['revision'], 'a')
            finally:
                applied_profile.cache_clear()


class ExecutionPanelTests(unittest.TestCase):
    def test_preview_never_applies(self):
        from web.app import app
        with patch('web.execution.Switcher') as cls:
            cls.return_value.preview.return_value = {'routes': {}}
            result = app.test_client().post('/api/execution/preview', json={'mode': 'real'})
            self.assertEqual(result.status_code, 200)
            cls.return_value.apply.assert_not_called()

    def test_parallel_apply_is_refused(self):
        from web.app import app
        from unittest.mock import Mock
        job = Mock()
        job.done.return_value = False
        with patch.dict(app.extensions['switch'], {'job': job}), patch('web.execution.Switcher') as cls:
            cls.return_value.preview.return_value = {'routes': {}}
            response = app.test_client().post('/api/execution/apply', json={'mode': 'real'})
            self.assertEqual(response.status_code, 409)
            cls.return_value.apply.assert_not_called()

    def test_bad_configuration_does_not_enqueue_apply(self):
        from connection.ops.app import create_app
        from unittest.mock import Mock
        pool = Mock()
        app = create_app(switch_pool=pool)
        with patch('web.execution.Switcher') as cls:
            cls.return_value.preview.side_effect = ValueError('bad config')
            response = app.test_client().post('/api/execution/apply', json={})
            self.assertEqual(response.status_code, 400)
            pool.submit.assert_not_called()


class ObservationPageTests(unittest.TestCase):
    def test_unavailable_observation_returns_explicit_error_without_request(self):
        from unittest.mock import Mock
        from connection.brain.api.app import create_app
        application = Mock()
        application.service.config = {}
        app = create_app(application)
        with patch('connection.brain.api.views.observation_url', side_effect=ValueError('当前观察模块不可用')), \
             patch('connection.brain.api.views.requests.request') as get:
            response = app.test_client().get('/api/robot_status')
            self.assertEqual(response.status_code, 503)
            self.assertIn('不可用', response.json['error'])
            get.assert_not_called()
