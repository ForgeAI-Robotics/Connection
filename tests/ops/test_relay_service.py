import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from ops import relay_service as relay
from ops.app import create_app
from ops.services import Service
from brain.storage.body_control import ControlStore
from contracts.body_control import VERSION, REVISION, stamp


class RelayServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = dict(enabled=True, relay_base_url='http://192.0.2.1:8092',
                            robot_serial='robot-test', action_endpoint='tcp://192.0.2.1:5556')
        self.config = dict(reception_real=dict(kernel_runtime_dir=str(self.root), body_control=self.control))
        self.settings = patch.object(relay, 'settings', return_value=(self.config, self.control, self.control['relay_base_url']))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.service = Service(id='relay', name='relay', layer='robot', controllable=True,
                               window='relay', match='/test/relay.py', log_service='relay')
        self.state = dict(schema=1, epoch=0, current=None, busy=None, leases={}, requests={},
                          binding=dict(robot_serial=self.control['robot_serial'],
                                       action_endpoint=self.control['action_endpoint'],
                                       relay=self.control['relay_base_url']))
        self.store = ControlStore(self.root)
        self.store.write(self.state)
        self.status = dict(contract_version=VERSION, coordination_revision=REVISION,
                           robot_serial='robot-test', action_endpoint='tcp://192.0.2.1:5556',
                           relay_instance_id='instance', updated_at=stamp(),
                           enabled=False, bound=False, forwarding_enabled=False,
                           action_port_released=True, recovery_required=False, error=None,
                           execution_readiness={'ready': False})
        monitor = patch.object(relay, '_record_probe')
        monitor.start()
        self.addCleanup(monitor.stop)

    def test_status_uses_authenticated_get_and_preserves_idle_vs_readiness(self):
        from io import BytesIO
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=BytesIO(json.dumps(self.status).encode()))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(relay, 'service_token', return_value='private-test-token'), \
             patch.object(relay, 'build_opener', return_value=opener):
            health = relay.health(self.service)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url, 'http://192.0.2.1:8092/v1/relay/status')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-test-token')
        self.assertTrue(health['ok'])
        self.assertIn('转发关闭', health['detail'])
        self.assertIn('执行未就绪', health['detail'])
        self.assertNotIn('private-test-token', json.dumps(health))

    def test_invalid_identity_and_stale_status_are_not_healthy(self):
        from io import BytesIO
        for changes in ({'robot_serial': 'another-robot'}, {'coordination_revision': 'old'},
                        {'updated_at': '2000-01-01T00:00:00+00:00'}):
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=BytesIO(json.dumps(dict(self.status, **changes)).encode()))
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with self.subTest(changes=changes), patch.object(relay, 'service_token', return_value='token'), \
                 patch.object(relay, 'build_opener', return_value=opener):
                self.assertFalse(relay.health(self.service)['ok'])

    def test_missing_credentials_never_sends_request(self):
        with patch.object(relay, 'service_token', return_value=''), patch.object(relay, 'build_opener') as opener:
            self.assertFalse(relay.health(self.service)['ok'])
            opener.assert_not_called()

    def test_start_existing_relay_never_kills_or_restarts_it(self):
        with patch.object(relay, 'matching_pids', return_value=[123]), \
             patch.object(relay, 'read_status', return_value=self.status), \
             patch.object(relay.tmuxctl, 'start_session') as start, \
             patch.object(relay.tmuxctl, 'stop_unmanaged') as stop:
            relay.start(self.service)
            start.assert_not_called()
            stop.assert_not_called()

    def test_safe_idle_stop_uses_existing_process_control(self):
        before = self.store.path.read_bytes()
        with patch.object(relay, 'matching_pids', side_effect=[[123], []]), \
             patch.object(relay, 'read_status', return_value=self.status), \
             patch.object(relay.tmuxctl, 'pane_pid', return_value=999), \
             patch.object(relay.tmuxctl, 'stop_session') as session, \
             patch.object(relay.tmuxctl, 'stop_unmanaged') as unmanaged:
            relay.stop(self.service)
            unmanaged.assert_called_once_with(self.service)
            session.assert_not_called()  # A different process owns the named session.
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_busy_unknown_or_unreachable_relay_never_stops(self):
        for changes in ({'bound': True}, {'forwarding_enabled': True}, {'enabled': True},
                        {'action_port_released': None}, {'recovery_required': True},
                        {'error': {'code': 'UNKNOWN'}}):
            with self.subTest(changes=changes), patch.object(relay, 'matching_pids', return_value=[123]), \
                 patch.object(relay, 'read_status', return_value=dict(self.status, **changes)), \
                 patch.object(relay.tmuxctl, 'stop_session') as stop:
                with self.assertRaises(RuntimeError):
                    relay.stop(self.service)
                stop.assert_not_called()
        with patch.object(relay, 'matching_pids', return_value=[123]), \
             patch.object(relay, 'read_status', side_effect=OSError('unreachable')), \
             patch.object(relay.tmuxctl, 'stop_unmanaged') as stop:
            with self.assertRaises(OSError):
                relay.stop(self.service)
            stop.assert_not_called()

    def test_unresolved_ledger_and_cancelled_open_command_block_stop(self):
        for changes in ({'current': 'old'}, {'busy': 'request'},
                        {'leases': {'old': {'state': 'recovery_required'}}},
                        {'requests': {'old': {'receipt': {'state': 'unconfirmed'}}}}):
            self.store.write(dict(self.state, **changes))
            with self.subTest(changes=changes), patch.object(relay, 'read_status') as status:
                with self.assertRaises(RuntimeError):
                    relay.stop(self.service)
                status.assert_not_called()
        self.store.write(self.state)
        (self.root / 'current_task.json').write_text(json.dumps(dict(
            task_id='task', state='cancelled', open_command_id='old-command')))
        with self.assertRaises(RuntimeError):
            with relay.maintenance_guard():
                pass

    def test_guard_holds_runtime_writer_lock_during_stop(self):
        import fcntl
        with relay.maintenance_guard(), (self.root / 'writer.lock').open('a') as stream:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_changed_target_cannot_use_old_ledger_to_allow_stop(self):
        self.control['action_endpoint'] = 'tcp://192.0.2.2:5556'
        with self.assertRaisesRegex(RuntimeError, '配置不匹配'):
            with relay.maintenance_guard():
                pass

    def test_confirmed_release_keeps_historical_failure_without_blocking_maintenance(self):
        self.state['leases']['old'] = {'state': 'released', 'evidence': {'exit_confirmed': True}}
        self.state['requests']['old-request'] = dict(body={'lease_id': 'old'}, receipt={'state': 'unconfirmed'})
        self.store.write(self.state)
        before = self.store.path.read_bytes()
        with relay.maintenance_guard():
            pass
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_named_session_is_stopped_only_when_relay_owns_its_pane(self):
        with patch.object(relay, 'matching_pids', side_effect=[[123], []]), \
             patch.object(relay, 'read_status', return_value=self.status), \
             patch.object(relay.tmuxctl, 'pane_pid', return_value=123), \
             patch.object(relay.tmuxctl, 'stop_session') as session, \
             patch.object(relay.tmuxctl, 'stop_unmanaged') as unmanaged:
            relay.stop(self.service)
            session.assert_called_once_with(self.service)
            unmanaged.assert_not_called()

    def test_start_refuses_occupied_session_without_killing_it(self):
        with patch.object(relay, 'matching_pids', return_value=[]), \
             patch.object(relay.tmuxctl, 'session_alive', return_value=True), \
             patch.object(relay.tmuxctl, 'start_session') as start:
            with self.assertRaisesRegex(RuntimeError, '其他进程占用'):
                relay.start(self.service)
            start.assert_not_called()

    def test_wrong_process_endpoint_is_rejected(self):
        args = b'\0'.join(part.encode() for part in ['python', '/test/relay.py', '--control-host',
                                                   '192.0.2.2', '--control-port', '8092'])
        with patch.object(relay, 'iter_matching_pids', return_value=[123]), \
             patch.object(Path, 'read_bytes', return_value=args):
            with self.assertRaises(RuntimeError):
                relay.matching_pids(self.service)

    def test_launcher_uses_existing_profile_and_no_forwarding_command(self):
        script = self.root / 'g1_navigation_vla_bridge/run_sequential_navigation_action_relay.sh'
        script.parent.mkdir()
        script.touch()
        with patch.object(relay, 'project_root', return_value=self.root), \
             patch('shared.log_setup.create_process_log_path', return_value=self.root / 'relay.log'):
            command = relay.start_command()
        import subprocess
        checked = subprocess.run(['bash', '-n'], input=command, text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('load_robot_network_config.sh', command)
        self.assertIn('EXPECTED_NX_SERIAL', command)
        self.assertIn('exec bash', command)
        self.assertNotIn('enable_navigation', command)
        self.assertNotIn('acquire', command)

    def test_restart_refusal_never_starts_new_process(self):
        app = create_app()
        self.addCleanup(lambda: [pool.shutdown() for pool in app.extensions['workers']])
        with patch('ops.app.by_id', return_value=self.service), \
             patch.object(relay, 'stop', side_effect=RuntimeError('原授权未核清')), \
             patch.object(relay, 'start') as start, \
             patch('shared.execution_profile.admission', return_value=__import__('contextlib').nullcontext()):
            response = app.test_client().post('/api/services/relay/restart')
        self.assertEqual(response.status_code, 400)
        self.assertIn('原授权未核清', response.get_json()['error'])
        start.assert_not_called()

    def test_logs_and_status_delegate_without_control_calls(self):
        from ops.control import _logs, _service_status
        with patch.object(relay, 'logs', return_value=('relay output', '/log', None)):
            self.assertEqual(_logs(self.service, 20), ('relay output', '/log', None))
        from dataclasses import replace
        with patch('ops.control._brain_state', return_value='stopped'):
            view = _service_status(replace(self.service, health=lambda _: {'ok': True, 'detail': '管理在线'}), include_pids=False)
        self.assertEqual(view['state'], 'unmanaged')


class RelayMonitorTests(unittest.TestCase):
    def setUp(self):
        from ops import services
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict('os.environ', {'FQPLANNER_LOG_ROOT': self.tmp.name})
        env.start(); self.addCleanup(env.stop)
        services._LAST_MONITOR.clear()
        self.addCleanup(services._LAST_MONITOR.clear)
        settings = patch.object(relay, 'settings', return_value=({}, {}, 'http://relay.invalid'))
        settings.start(); self.addCleanup(settings.stop)
        self.service = Service(id='relay', name='relay', layer='robot', controllable=True, log_service='relay')

    def test_initial_and_changed_readiness_are_persisted_without_poll_spam(self):
        status = dict(relay_instance_id='relay-1', recovery_required=False, forwarding_enabled=False,
                      bound=False, action_port_released=True,
                      execution_readiness={'ready': False, 'blockers': ['BODY_OR_O6_HOLD_UNCONFIRMED']})
        with patch.object(relay, 'read_status', return_value=status):
            relay.health(self.service)
            status['updated_at'] = 'another-sample'
            relay.health(self.service)
            status['execution_readiness'] = {'ready': True, 'blockers': []}
            relay.health(self.service)
            relay.health(self.service)
            status.update(forwarding_enabled=True, upstream_fresh=True)
            status['execution_readiness'] = {'ready': False, 'blockers': ['NEW_ENABLE_REQUIRES_IDLE']}
            health = relay.health(self.service)
            self.assertNotIn('执行未就绪', health['detail'])
        path = next(Path(self.tmp.name).glob('*/relay/monitor.log'))
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 3, lines)
        self.assertIn('BODY_OR_O6_HOLD_UNCONFIRMED', lines[0])
        self.assertIn('执行准备已就绪', lines[1])
        self.assertIn('转发已开启', lines[2])
        self.assertIn('上游新鲜', lines[2])
        self.assertIn('relay_instance_id=relay-1', lines[0])

    def test_restarted_instance_and_connection_recovery_are_visible(self):
        state = dict(relay_instance_id='first', recovery_required=False, forwarding_enabled=False,
                     bound=False, action_port_released=True, execution_readiness={'ready': True})
        with patch.object(relay, 'read_status', return_value=state):
            relay.health(self.service)
            state['relay_instance_id'] = 'second'
            relay.health(self.service)
        with patch.object(relay, 'read_status', side_effect=OSError('connection refused')):
            relay.health(self.service)
            relay.health(self.service)
        with patch.object(relay, 'read_status', return_value=state):
            relay.health(self.service)
        lines = next(Path(self.tmp.name).glob('*/relay/monitor.log')).read_text().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn('second', lines[1])
        self.assertIn('监测恢复', lines[-1])

    def test_log_view_contains_process_output_and_monitor_without_changing_files(self):
        from shared.log_setup import append_monitor_log
        process = Path(self.tmp.name) / 'process.log'
        process.write_text('relay management starting; forwarding starts closed\n')
        monitor = append_monitor_log('relay', '执行准备已就绪')
        before = (process.read_bytes(), monitor.read_bytes())
        with patch.object(relay, 'matching_pids', return_value=[]), \
             patch('ops.services.latest_log_file', return_value=process):
            text, path, _ = relay.logs(self.service, 20)
        self.assertIn('relay management starting', text)
        self.assertIn('执行准备已就绪', text)
        self.assertIn('面板监测', text)
        self.assertIn('monitor.log', path)
        self.assertEqual((process.read_bytes(), monitor.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
