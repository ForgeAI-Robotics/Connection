"""Contract, persistence, and real HTTP integration for the two simulated downstreams."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import build_opener, ProxyHandler

from contracts.control_receipt import valid_receipt, VERSION
from contracts.tasks import CONTRACT_VERSION
from execution.reception_sim.server import create_app
from execution.reception_sim.store import Store
from shared.execution_profile import resolve

GOLDEN = {row['step_id']: row['body'] for row in json.loads(
    (Path(__file__).parents[1] / 'contracts/reception_wire.json').read_text())}
PROFILE = {'mode': 'simulation', 'modules': {
    'reception': {'mode': 'simulation', 'simulation_backend': 'reception_protocol'},
    'execution': {'mode': 'disabled'}, 'observation': {'mode': 'disabled'}}}


def nav_body():
    return deepcopy(GOLDEN['NAVIGATING_TO_TABLE2'])


def pick_body():
    body = deepcopy(GOLDEN['VLA_PICKING'])
    body['navigation_proof']['dream_command_id'] = nav_body()['command_id']
    return body


class ProtocolContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.clock = [time.time()]
        self.nav_app = create_app('nav', self.path, delay=2)
        self.nav = self.nav_app.test_client()
        self.vla_app = create_app('vla', self.path, delay=2, nav_get=lambda path: self.nav.get(path).json)
        self.vla = self.vla_app.test_client()
        for app in [self.nav_app, self.vla_app]:
            app.extensions['simulator'].clock = lambda: self.clock[0]

    def arrived(self):
        self.assertEqual(self.nav.post('/v1/navigation/goals', json=nav_body()).status_code, 202)
        self.clock[0] += 3
        return self.nav.get('/v1/commands/' + nav_body()['command_id']).json

    def test_accept_poll_terminal_and_restart_keep_original_transaction(self):
        accepted = self.nav.post('/v1/navigation/goals', json=nav_body())
        self.assertEqual(accepted.status_code, 202)
        path = accepted.json['status_url']
        self.assertEqual(self.nav.get(path).json['state'], 'accepted')
        self.clock[0] += 1
        self.assertEqual(self.nav.get(path).json['state'], 'arming')
        restarted = Store(self.path, clock=lambda: self.clock[0])
        self.clock[0] += 2
        result = restarted.command('nav', nav_body()['command_id'])
        self.assertTrue(result['result']['reached'])
        self.assertTrue(result['result']['navigation_stopped'])
        self.assertEqual(result['accepted_at'], accepted.json['accepted_at'])
        repeat = self.nav.post('/v1/navigation/goals', json=nav_body())
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json, result)
        body = nav_body(); body['task_id'] = 'foreign'
        self.assertEqual(self.nav.post('/v1/navigation/goals', json=body).json['error']['code'], 'COMMAND_ID_CONFLICT')

    def test_invalid_wire_never_creates_a_command(self):
        for key, value in [('goal_xyt', [True, 2, 3]), ('goal_xyt', [float('nan'), 2, 3]),
                           ('leg_index', True), ('contract_version', 'wrong'), ('frame_id', 'odom'),
                           ('require_final_orientation', False), ('route_phase', 'invented')]:
            body = nav_body(); body[key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.nav.post('/v1/navigation/goals', json=body).status_code, 400)
        self.assertEqual(self.nav.get('/v1/commands/' + nav_body()['command_id']).status_code, 404)

    def test_route_order_and_global_busy_are_enforced(self):
        late = deepcopy(GOLDEN['NAVIGATING_TO_TABLE1'])
        self.assertEqual(self.nav.post('/v1/navigation/goals', json=late).json['error']['code'], 'ROUTE_ORDER_INVALID')
        self.nav.post('/v1/navigation/goals', json=nav_body())
        other = nav_body(); other.update(command_id='other', task_id='other')
        self.assertEqual(self.nav.post('/v1/navigation/goals', json=other).status_code, 409)

    def test_vla_queries_nav_and_rejects_foreign_or_missing_proof(self):
        self.arrived()
        foreign = pick_body(); foreign['task_id'] = 'foreign'
        self.assertEqual(self.vla.post('/v1/vla/tasks', json=foreign).status_code, 202)
        failed = self.vla.get('/v1/vla/tasks/' + foreign['command_id']).json
        self.assertEqual(failed['state'], 'failed')
        self.assertFalse(failed['result']['action_started'])
        valid = pick_body(); valid['command_id'] = 'valid-pick'
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no remote calls')):
            self.assertEqual(self.vla.post('/v1/vla/tasks', json=valid).status_code, 202)

    def test_cancel_identity_stop_and_restart_do_not_turn_into_success(self):
        self.nav.post('/v1/navigation/goals', json=nav_body())
        request = {'task_id': 'foreign', 'command_id': nav_body()['command_id']}
        self.assertEqual(self.nav.post('/v1/navigation/cancel', json=request).status_code, 409)
        request['task_id'] = nav_body()['task_id']
        self.assertEqual(self.nav.post('/v1/navigation/cancel', json=request).status_code, 202)
        self.clock[0] += 10
        result = Store(self.path, clock=lambda: self.clock[0]).command('nav', request['command_id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertFalse(result['result']['reached'])
        self.assertTrue(result['result']['navigation_stopped'])

    def test_weak_evidence_and_failed_pick_stay_distinct(self):
        from brain.adapters.execution import interpret_observation
        self.arrived()
        for scenario in ['hand_state_only', 'pick_failure']:
            with self.subTest(scenario=scenario):
                state = Path(self.temp.name) / (scenario + '.sqlite3')
                source = Store(self.path)
                # Copy a stopped, arrived scene into an isolated fault experiment.
                import shutil
                shutil.copyfile(source.path, state)
                app = create_app('vla', state, delay=2, scenario=scenario,
                    nav_get=lambda path: self.nav.get(path).json)
                store = app.extensions['simulator']; store.clock = lambda: self.clock[0]
                self.assertEqual(app.test_client().post('/v1/vla/tasks', json=pick_body()).status_code, 202)
                self.clock[0] += 3
                raw = store.command('vla', pick_body()['command_id'])
                observation = interpret_observation(raw['command_id'], raw,
                    {'skill': 'pick', 'task_id': raw['task_id'], 'body': pick_body()})
                self.assertFalse(observation['evidence']['supports'])
                self.assertTrue(observation['stopped'])
                self.assertEqual(raw['state'], 'failed' if scenario == 'pick_failure' else 'succeeded')

    def test_unknown_ids_wrong_versions_and_unsupported_camera_do_not_succeed(self):
        self.assertEqual(self.vla.get('/v1/vla/tasks/unknown').status_code, 404)
        self.assertEqual(self.nav.get('/health', headers={'X-Contract-Version': 'bad'}).status_code, 400)
        self.assertEqual(self.vla.post('/v1/camera/snapshots', json={}).status_code, 501)
        self.assertFalse(self.vla.get('/v1/camera/status').json['ready'])

    def test_receipt_is_bound_to_identity_controller_and_freshness(self):
        now = datetime.now(timezone.utc)
        context = {'task_id': 't', 'source_command_id': 'c', 'kind': 'to_vla'}
        receipt = {**context, 'contract_version': VERSION, 'controller': 'manipulation',
                   'active_command_id': None, 'confirmed': True, 'observed_at': now.isoformat()}
        completed = (now - timedelta(seconds=1)).isoformat()
        self.assertTrue(valid_receipt(receipt, context, completed_at=completed, now=now))
        for key, value in [('task_id', 'other'), ('source_command_id', 'other'), ('confirmed', 1),
                           ('controller', 'navigation'), ('active_command_id', 'new'),
                           ('observed_at', (now - timedelta(seconds=20)).isoformat()),
                           ('observed_at', (now + timedelta(seconds=20)).isoformat())]:
            self.assertFalse(valid_receipt(dict(receipt, **{key: value}), context, completed_at=completed, now=now))


class ProtocolProcessTests(unittest.TestCase):
    """Use real TCP clients and separate interpreters, no BodyAdapter mocks."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.processes = []
        self.addCleanup(self.stop_processes)
        self.endpoints = {}
        for role, key in [('nav', 'dream'), ('vla', 'vla')]:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            self.endpoints[key] = f'http://127.0.0.1:{port}'
        for name in ['shared.execution_profile.PROTOCOL_URLS', 'shared.protocol_simulation.PROTOCOL_URLS']:
            patcher = patch(name, self.endpoints); patcher.start(); self.addCleanup(patcher.stop)
        profile = resolve(PROFILE, {}); profile['revision'] = 'protocol-test'
        patcher = patch('shared.execution_profile.applied_profile', return_value=profile)
        patcher.start(); self.addCleanup(patcher.stop)
        self.config = {'brain': {'capture_timeline': False, 'reasoning': {'recovery_advice': False}},
                       'reflection': {'enabled': False},
                       'reception_real': {'kernel_runtime_dir': str(self.root / 'tasks'), 'kernel_enabled': False}}

    def stop_processes(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)

    def start_pair(self, scenario='success', delay=.05):
        for role, key in [('nav', 'dream'), ('vla', 'vla')]:
            args = [sys.executable, '-m', 'execution.reception_sim', role, '--port', self.endpoints[key].rsplit(':', 1)[1],
                    '--state', str(self.root / 'sim.sqlite3'), '--delay', str(delay), '--scenario', scenario,
                    '--nav-url', self.endpoints['dream']]
            log = (self.root / (role + '.log')).open('w')
            self.addCleanup(log.close)
            env = dict(os.environ, FQPLANNER_LOG_ROOT=str(self.root / 'logs'))
            proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=env)
            self.processes.append(proc)
        opener = build_opener(ProxyHandler({}))
        for endpoint in self.endpoints.values():
            deadline = time.monotonic() + 10
            while True:
                try:
                    with opener.open(endpoint + '/health', timeout=.5) as response:
                        self.assertEqual(json.load(response)['status'], 'ok')
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        self.fail('Simulator failed to start: ' + '\n'.join(p.read_text() for p in self.root.glob('*.log')))
                    time.sleep(.05)

    def service(self):
        from brain.app import create_service
        return create_service(self.config, model=lambda _: (_ for _ in ()).throw(AssertionError('Exact SOP needs no LLM')))

    def test_two_processes_complete_via_public_brain_entry_with_frozen_wire(self):
        from brain.application import BrainApplication
        from brain.api.app import create_app as brain_app
        self.start_pair()
        service = self.service()
        application = BrainApplication(service).start()
        self.addCleanup(application.close)
        client = brain_app(application).test_client()
        response = client.post('/publish_task', json={'task': '开始接待', 'task_id': 'protocol-e2e', 'refresh': False})
        self.assertEqual(response.status_code, 200, response.json)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = client.get('/api/task_status').json
            if status.get('state') in {'succeeded', 'failed', 'recovery_required'}:
                break
            time.sleep(.05)
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual(status['completed'], status['total'])
        self.assertEqual(status['total'], 11)
        checks = {item['subtask']: item for item in status['subtask_list']}
        self.assertTrue(checks['VERIFYING_GRASP']['effect_verified'])
        self.assertTrue(checks['VERIFYING_PLACE']['effect_verified'])
        self.assertFalse(checks['DREAM_INSPECTING_TABLE2']['effect_verified'])
        record = service.runtime.snapshot()
        self.assertEqual(record['execution_backend'], 'reception_protocol')
        self.assertEqual(record['selection']['sop']['id'], 'reception.single_can')
        commands = []
        for step, bucket in record['steps'].items():
            for attempt in bucket.get('attempts') or []:
                body = attempt['request']['body']
                expected = deepcopy(GOLDEN[step]); expected.update(task_id=record['task_id'], command_id=attempt['command_id'])
                if 'navigation_proof' in expected:
                    source = 'NAVIGATING_TO_TABLE2' if body['operation'] == 'pick' else 'NAVIGATING_TO_TABLE1'
                    expected['navigation_proof']['dream_command_id'] = record['steps'][source]['attempts'][-1]['command_id']
                self.assertEqual(body, expected)
                commands.append(attempt['command_id'])
        self.assertEqual(len(commands), 6)
        self.assertEqual(record['holding'], None)
        self.assertEqual(record['object_location'], 'table_1')
        self.assertEqual(record['handoff_observation']['reason'], 'controller_confirmed')
        for verification, action in [('VERIFYING_GRASP', 'VLA_PICKING'), ('VERIFYING_PLACE', 'VLA_PLACING')]:
            self.assertEqual(record['steps'][verification]['status'], 'done')
            self.assertEqual(record['steps'][verification]['verified_attempt_id'],
                             record['steps'][action]['attempts'][-1]['attempt_id'])
        with Store(self.root / 'sim.sqlite3').transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commands').fetchone()[0], 6)

    def test_pause_continue_each_physical_step_submits_one_new_command(self):
        from brain.application import BrainApplication
        from brain.api.app import create_app as brain_app
        self.start_pair(delay=1)
        application = BrainApplication(self.service()).start()
        self.addCleanup(application.close)
        client = brain_app(application).test_client()
        client.post('/publish_task', json={'task': '开始接待', 'task_id': 'continue-each-step'})
        store = Store(self.root / 'sim.sqlite3')
        for step_id in (key for key in GOLDEN if key != 'DREAM_INSPECTING_TABLE2'):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                current = client.get('/api/task_status').json
                record = application.service.runtime.snapshot()
                command = record.get('open_command_id')
                if current['state'] == 'running' and record['phase'] == step_id and command:
                    try:
                        role = 'vla' if step_id in {'VLA_PICKING', 'VLA_PLACING'} else 'nav'
                        viewed = store.command(role, command)
                        if viewed['state'] not in {'succeeded', 'failed', 'cancelled'}:
                            break
                    except Exception:
                        pass
                time.sleep(.02)
            else:
                self.fail(str({"waiting_for": step_id, "state": current["state"],
                               "pending": record.get("pending_progress"),
                               "sim_logs": {p.name: p.read_text()[-3000:] for p in self.root.glob("*.log")}}))
            self.assertEqual(client.post('/api/task_pause', json={'task_id': current['task_id']}).json['state'], 'paused')
            self.assertEqual(store.command(role, command)['state'], 'cancelled')
            payload = {'task_id': current['task_id'], 'step_id': step_id, 'expected_command_id': command}
            resumed = client.post('/api/task_continue', json=payload).json
            self.assertTrue(resumed['retry_prepared'], resumed)
            self.assertEqual(resumed['command_id'], command + '-r1')
            duplicate = client.post('/api/task_continue', json=payload).json
            self.assertFalse(duplicate['accepted'], duplicate)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = client.get('/api/task_status').json
            if current['terminal']:
                break
            time.sleep(.03)
        self.assertEqual(current['state'], 'succeeded', current)
        self.assertEqual(current['completed'], 11)
        record = application.service.runtime.snapshot()
        self.assertEqual(set(record['dispatch_counts'].values()), {2})
        for step_id in (key for key in GOLDEN if key != 'DREAM_INSPECTING_TABLE2'):
            attempts = record['steps'][step_id]['attempts']
            self.assertEqual([a['outcome'] for a in attempts], ['cancelled', 'succeeded'])
            self.assertEqual(attempts[1]['authorized_by'], 'human_continue')
            self.assertEqual(store.command('vla' if step_id in {'VLA_PICKING', 'VLA_PLACING'} else 'nav',
                                           attempts[0]['command_id'])['state'], 'cancelled')
        with store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commands').fetchone()[0], 12)

    def test_nav_only_entry_drives_four_legs_over_http_and_never_calls_vla(self):
        from brain.application import BrainApplication
        from brain.api.app import create_app as brain_app
        self.start_pair(scenario='nav_only')
        application = BrainApplication(self.service()).start()
        self.addCleanup(application.close)
        client = brain_app(application).test_client()
        response = client.post('/publish_task', json={'task': '开始接待（仅导航）', 'task_id': 'nav-only-e2e'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json['accepted'], response.json)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = client.get('/api/task_status').json
            if status.get('state') in {'succeeded', 'failed', 'recovery_required'}:
                break
            time.sleep(.05)
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual(status['selection']['sop']['id'], 'reception.single_can.nav_only')
        self.assertEqual([item['step_id'] for item in status['subtask_list']],
                         ['INITIALIZING', 'FETCHING_WORLD', 'NAVIGATING_TO_TABLE2', 'NAVIGATING_TO_RELAY2',
                          'LATERAL_TO_RELAY3', 'NAVIGATING_TO_TABLE1'])
        with Store(self.root / 'sim.sqlite3').transaction() as db:
            rows = db.execute('SELECT id, role FROM commands ORDER BY rowid').fetchall()
        self.assertEqual({role for _, role in rows}, {'nav'})  # the VLA simulator received nothing
        self.assertEqual([c.rsplit('-', 1)[0] for c, _ in rows], ['nav-table2', 'nav-relay2', 'nav-relay3', 'nav-table1'])

    def test_missing_controller_receipt_is_optional_when_transport_is_idle(self):
        self.start_pair(scenario='missing_receipt')
        service = self.service(); service.publish('开始接待', 'no-receipt')
        self.assertEqual(service.status()['state'], 'succeeded')
        self.assertEqual(service.runtime.record['handoff_observation']['reason'], 'transport_ready_without_receipt')
        with Store(self.root / 'sim.sqlite3').transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commands').fetchone()[0], 6)

    def test_deployed_weak_evidence_blocks_without_regrasp(self):
        self.start_pair(scenario='hand_state_only')
        service = self.service(); service.publish('开始接待', 'weak-evidence')
        self.assertEqual(service.status()['state'], 'recovery_required')
        self.assertNotEqual(service.runtime.record['steps'].get('VERIFYING_GRASP', {}).get('status'), 'done')
        with Store(self.root / 'sim.sqlite3').transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commands').fetchone()[0], 2)

    def test_cancel_active_http_command_and_restart_preserve_original_result(self):
        from brain.adapters.dream_client import DreamClient
        self.start_pair(delay=30)
        dream = DreamClient(self.endpoints['dream'])
        original = nav_body()
        dream.submit_navigation(original)
        dream.cancel(original['task_id'], original['command_id'])
        before = dream.command(original['command_id'])
        self.assertEqual(before['state'], 'cancelled')
        self.stop_processes()
        self.start_pair(delay=.05)
        self.assertEqual(dream.command(original['command_id']), before)
        duplicate = dream.submit_navigation(original)
        self.assertEqual(duplicate['state'], 'cancelled')
        with Store(self.root / 'sim.sqlite3').transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commands').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
