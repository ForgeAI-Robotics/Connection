"""SIMPLE physical reception service: contract parity, journal rules and the brain end to end.

The physics episode is replaced by a scripted stand-in; everything else is the real
facade (service.server handler over TCP), journal, worker loop and brain entry.
"""
from copy import deepcopy
import inspect
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from contracts import control_receipt, reception_lan, tasks
from shared.execution_profile import resolve
from simulation.backends.simple_o7.service import reception_contract, reception_episode, reception_worker
from simulation.backends.simple_o7.service.reception_store import Conflict, ReceptionStore
from simulation.backends.simple_o7.service.server import Bridge, handler

GOLDEN = {row['step_id']: row['body'] for row in json.loads(
    (Path(__file__).parents[1] / 'contracts/reception_wire.json').read_text())}
ROBOT_API = {'backends': {'simple_o7': {'url': 'http://127.0.0.1:1'}}}
PROFILE = {'mode': 'simulation', 'modules': {
    'reception': {'mode': 'simulation', 'simulation_backend': 'reception_simple'},
    'execution': {'mode': 'disabled'}, 'observation': {'mode': 'disabled'}}}


def nav(leg='NAVIGATING_TO_TABLE2', task='t1', command=None):
    body = deepcopy(GOLDEN[leg])
    body.update(task_id=task, command_id=command or f'{task}-{leg}')
    return body


def vla(step, proof, task='t1', command=None):
    body = deepcopy(GOLDEN[step])
    body.update(task_id=task, command_id=command or f'{task}-{step}')
    body['navigation_proof']['dream_command_id'] = proof
    return body


class FakeEpisode:
    """Scripted measured outcomes in the shape Episode.run_segment returns."""
    fail_at = None
    miss_once = None
    segments = []

    def __init__(self, output, seed=601, **options):
        self.output = output

    def run_segment(self, segment, cancelled=lambda: False, progress=None):
        type(self).segments.append(segment)
        if progress:
            progress('phase_of_' + segment)
        ok = segment != type(self).fail_at
        recoverable = False
        if segment == type(self).miss_once:
            type(self).miss_once, ok, recoverable = None, False, True
        after = dict(base_xyt=[-0.6, 0., 0.], can_xyz=[-0.3, 0.08, 0.19], lift_m=.13, grasp_contact=segment != 'place',
                     supported=segment == 'place', destination_table_contact=segment == 'place', lifted_gate=True,
                     placement_accepted=segment == 'place' and ok, hand_can_distance_m=.2, sim_time=1.)
        if segment.startswith('nav_'):
            checks = dict(sim_target=segment[4:].replace('table', 'table_'), sim_goal_xyt=[0, 0, 0], position_error_m=.02,
                          yaw_error_rad=.02, reached=ok, object_retained=True, stopped=True)
        elif segment == 'pick':
            checks = dict(object_grasped=ok, lift_m=.13)
        else:
            checks = dict(object_at_target=ok, released=ok, placement_xy_error_m=.01)
        return dict(segment=segment, outcome='succeeded' if ok else 'failed', error=None if ok else 'scripted failure',
                    before=after, after=after, frames=10, wall_seconds=0., phase_events=[{'phase': 'p'}],
                    episode_terminated=False, reward=0., checks=checks, recoverable=recoverable)

    def close(self):
        pass


class ContractParityTests(unittest.TestCase):
    def test_vendored_contract_matches_brain_contract(self):
        self.assertEqual(reception_contract.NAVIGATION_LEGS, reception_lan.NAVIGATION_LEGS)
        self.assertEqual(reception_contract.CONTRACT_VERSION, tasks.CONTRACT_VERSION)
        self.assertEqual(reception_contract.OBJECT_ID, tasks.OBJECT_ID)
        self.assertEqual(reception_contract.RECEIPT_VERSION, control_receipt.VERSION)
        self.assertEqual(reception_contract.CONTROLLERS, control_receipt.CONTROLLERS)
        self.assertEqual(inspect.getsource(reception_contract.validate_request),
                         inspect.getsource(reception_lan.validate_request))

    def test_profile_routes_both_roles_to_the_simple_service(self):
        routes = resolve(PROFILE, ROBOT_API)['routes']
        self.assertEqual(routes['reception']['backend'], 'reception_simple')
        self.assertEqual(routes['reception']['endpoints'], {'dream': 'http://127.0.0.1:1/reception/nav',
                                                            'vla': 'http://127.0.0.1:1/reception/vla'})
        with self.assertRaises(ValueError):
            resolve(PROFILE, {'backends': {}})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.alive = {}
        self.store = ReceptionStore(self.tmp.name, alive=lambda pid: self.alive.get(pid, False))
        self.launched = []

    def launch(self, task, episode):
        self.launched.append(task)
        self.alive[100 + len(self.launched)] = True
        return 100 + len(self.launched)

    def finish(self, command_id, segment, holding=None):
        self.store.claim('t1')
        changes = {'next': ['nav_table2', 'pick', 'nav_relay2', 'nav_relay3', 'nav_table1', 'place'].index(segment) + 1,
                   'state': 'ready'}
        if segment.startswith('nav_'):
            changes['last_navigation'] = command_id
        self.store.finish(command_id, 'succeeded', {'success': True}, session_changes=changes)

    def test_first_leg_creates_one_episode_and_duplicates_never_replay(self):
        value, code = self.store.submit('nav', nav(), self.launch)
        self.assertEqual((code, value['state']), (202, 'accepted'))
        again, code = self.store.submit('nav', nav(), self.launch)
        self.assertEqual((code, again['command_id']), (200, value['command_id']))
        self.assertEqual(self.launched, ['t1'])
        changed = nav(); changed['goal_xyt'] = [0, 0, 0]
        with self.assertRaises(Conflict) as caught:
            self.store.submit('nav', changed, self.launch)
        self.assertEqual(caught.exception.code, 'COMMAND_ID_CONFLICT')
        with self.assertRaises(Conflict) as caught:
            self.store.submit('nav', nav(task='t2'), self.launch)
        self.assertEqual(caught.exception.code, 'NAVIGATION_BUSY')

    def test_route_order_and_navigation_proof_are_enforced(self):
        with self.assertRaises(Conflict) as caught:
            self.store.submit('nav', nav('NAVIGATING_TO_RELAY2'), self.launch)
        self.assertEqual(caught.exception.code, 'ROUTE_ORDER_INVALID')
        self.store.submit('nav', nav(), self.launch)
        self.finish('t1-NAVIGATING_TO_TABLE2', 'nav_table2')
        with self.assertRaises(Conflict):
            self.store.submit('nav', nav('NAVIGATING_TO_RELAY2'), self.launch)
        rejected, code = self.store.submit('vla', vla('VLA_PICKING', 'someone-else'), self.launch)
        self.assertEqual((code, rejected['state']), (202, 'failed'))
        self.assertEqual(rejected['error']['code'], 'NAVIGATION_PROOF_INVALID')
        self.assertFalse(rejected['result']['action_started'])
        accepted, _ = self.store.submit('vla', vla('VLA_PICKING', 't1-NAVIGATING_TO_TABLE2', command='p2'), self.launch)
        self.assertEqual(accepted['state'], 'accepted')
        self.assertEqual(self.launched, ['t1'])

    def test_cancel_before_start_keeps_episode_and_worker_loss_is_failure(self):
        self.store.submit('nav', nav(), self.launch)
        cancelled = self.store.command('nav', 't1-NAVIGATING_TO_TABLE2', cancel_task='t1')
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertFalse(cancelled['result']['action_started'])
        retry, code = self.store.submit('nav', nav(command='t1-retry'), self.launch)
        self.assertEqual((code, retry['state']), (202, 'accepted'))
        self.assertEqual(self.launched, ['t1'])
        self.store.claim('t1')
        self.store.finish('t1-retry', 'failed', {'success': False},
                          session_changes={'state': 'failed', 'failure': 'scripted'})
        with self.assertRaises(Conflict) as caught:
            self.store.submit('nav', nav(command='t1-retry2'), self.launch)
        self.assertEqual(caught.exception.code, 'EPISODE_ENDED')
        self.store.submit('nav', nav(task='t3'), self.launch)
        self.store.claim('t3')
        self.alive.clear()
        lost = self.store.command('nav', 't3-NAVIGATING_TO_TABLE2')
        self.assertEqual(lost['state'], 'failed')
        self.assertEqual(lost['error']['code'], 'SIMULATION_WORKER_EXITED')
        self.assertFalse(lost['result']['reached'])

    def test_receipt_is_a_fresh_observation_of_the_live_episode(self):
        self.store.submit('nav', nav(), self.launch)
        self.finish('t1-NAVIGATING_TO_TABLE2', 'nav_table2')
        active, receipt = self.store.status()
        self.assertIsNone(active)
        context = {'task_id': 't1', 'source_command_id': 't1-NAVIGATING_TO_TABLE2', 'kind': 'to_vla'}
        completed = self.store.command('nav', 't1-NAVIGATING_TO_TABLE2')['completed_at']
        self.assertTrue(control_receipt.valid_receipt(receipt, context, completed_at=completed))


class BrainEndToEndTests(unittest.TestCase):
    """Real brain entry -> DreamClient/VlaClient -> SIMPLE facade over TCP -> worker loop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        FakeEpisode.fail_at, FakeEpisode.miss_once, FakeEpisode.segments = None, None, []
        config = {'runtime': str(self.root / 'runtime'), 'source': str(self.root), 'config_path': ''}
        bridge = Bridge(config)
        self.bridge = bridge
        threads = []

        def launch(task, episode):
            store = bridge.reception.store
            thread = threading.Thread(target=reception_worker.run, args=(store, task, episode, 601),
                                      kwargs={'scene': bridge.reception.scene,
                                              'office_assets': bridge.reception.config.get('reception_office_assets')}, daemon=True)
            thread.start()
            threads.append(thread)
            return 4242

        bridge.reception.launcher = launch
        bridge.reception.store.alive = lambda pid: any(t.is_alive() for t in threads)
        patcher = patch.object(reception_episode, 'Episode', FakeEpisode)
        patcher.start(); self.addCleanup(patcher.stop)
        quiet = type('Quiet', (handler(bridge, 'token'),), {'log_message': lambda *a: None})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), quiet)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.store = bridge.reception.store
        url = f'http://127.0.0.1:{self.server.server_address[1]}'
        profile = resolve(PROFILE, {'backends': {'simple_o7': {'url': url}}})
        profile['revision'] = 'simple-test'
        self.profile = profile
        patcher = patch('shared.execution_profile.applied_profile', return_value=profile)
        patcher.start(); self.addCleanup(patcher.stop)
        self.config = {'brain': {'capture_timeline': False, 'reasoning': {'recovery_advice': False}},
                       'reflection': {'enabled': False},
                       'reception_real': {'kernel_runtime_dir': str(self.root / 'tasks'), 'kernel_enabled': False,
                                          'dream_poll_interval_sec': .05, 'vla_poll_interval_sec': .05}}

    def run_task(self, task_id, task='开始接待'):
        from brain.app import create_service
        from brain.application import BrainApplication
        from brain.api.app import create_app as brain_app
        service = create_service(self.config, model=getattr(self, 'model', None) or
                                 (lambda _: (_ for _ in ()).throw(AssertionError('no LLM'))))
        application = BrainApplication(service).start()
        self.addCleanup(application.close)
        self.client = brain_app(application).test_client()
        response = self.client.post('/publish_task', json={'task': task, 'task_id': task_id, 'refresh': False})
        self.assertEqual(response.status_code, 200, response.json)
        return service, self.wait()

    def wait(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = self.client.get('/api/task_status').json
            if status.get('state') in {'succeeded', 'failed', 'recovery_required'}:
                break
            time.sleep(.05)
        return status

    def test_brain_completes_reception_on_the_physical_episode_service(self):
        service, status = self.run_task('simple-e2e')
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual((status['completed'], status['total']), (11, 11))
        record = service.runtime.snapshot()
        self.assertEqual(record['execution_backend'], 'reception_simple')
        self.assertEqual(record['holding'], None)
        self.assertEqual(record['object_location'], 'table_1')
        self.assertEqual(FakeEpisode.segments, ['nav_table2', 'pick', 'nav_relay2', 'nav_relay3', 'nav_table1', 'place'])
        for step, bucket in record['steps'].items():
            for attempt in bucket.get('attempts') or []:
                expected = deepcopy(GOLDEN[step]); expected.update(task_id=record['task_id'], command_id=attempt['command_id'])
                body = attempt['request']['body']
                if 'navigation_proof' in expected:
                    expected['navigation_proof']['dream_command_id'] = body['navigation_proof']['dream_command_id']
                self.assertEqual(body, expected)
        session = self.store.sessions()[-1]
        self.assertEqual((session['state'], session['next']), ('completed', 6))

    def test_missed_navigation_goal_is_reissued_by_continue_on_the_same_episode(self):
        FakeEpisode.miss_once = 'nav_table1'
        service, status = self.run_task('simple-retry')
        self.assertEqual(status['state'], 'recovery_required', status)
        missed = service.runtime.snapshot()['steps']['NAVIGATING_TO_TABLE1']['attempts'][-1]['command_id']
        self.assertTrue(self.store.command('nav', missed)['result']['simulation']['retry_allowed'])
        response = self.client.post('/api/task_continue', json={'task_id': 'simple-retry'})
        self.assertEqual(response.status_code, 200, response.json)
        status = self.wait()
        self.assertEqual(status['state'], 'succeeded', status)
        record = service.runtime.snapshot()
        attempts = [a['command_id'] for a in record['steps']['NAVIGATING_TO_TABLE1']['attempts']]
        self.assertEqual(attempts, [missed, missed + '-r1'])
        self.assertEqual(FakeEpisode.segments, ['nav_table2', 'pick', 'nav_relay2', 'nav_relay3',
                                                'nav_table1', 'nav_table1', 'place'])
        self.assertEqual(self.store.command('nav', missed)['state'], 'failed')
        self.assertEqual(len(self.store.sessions()), 1)

    def test_physical_pick_failure_stops_the_flow_without_further_commands(self):
        FakeEpisode.fail_at = 'pick'
        service, status = self.run_task('simple-fail')
        self.assertEqual(status['state'], 'recovery_required', status)
        self.assertEqual(FakeEpisode.segments, ['nav_table2', 'pick'])
        record = service.runtime.snapshot()
        pick = record['steps']['VLA_PICKING']['attempts'][-1]['command_id']
        downstream = self.store.command('vla', pick)
        self.assertEqual(downstream['state'], 'failed')
        self.assertEqual(downstream['error']['code'], 'EMPTY_GRASP')
        self.assertFalse(downstream['result']['object_grasped'])
        self.assertIsNone(record['holding'])
        self.assertEqual(self.store.sessions()[-1]['state'], 'failed')

    def use_office_route(self, *, navigation_only=False):
        from brain.packages.reception_skill import BASELINE_PATH, load_skill
        from simulation.backends.simple_o7.service.reception_http import Reception
        path = BASELINE_PATH.with_name('reception_single_can_v2.yaml')
        skill = load_skill(path=path)
        self.config['reception_real']['skill_file'] = str(path)
        old = self.bridge.reception
        (self.root / 'scene.xml').write_text('<mujoco/>')
        self.bridge.reception = Reception({**old.config, 'reception_scene': 'office_v2',
            'reception_navigation_only': navigation_only,
            'reception_office_assets': str(self.root), 'reception_navigation_legs': skill.definition['points']},
            launcher=old.launcher)
        self.store = self.bridge.reception.store
        self.store.alive = old.store.alive
        return skill

    def test_new_office_route_executes_seven_segments_with_original_proofs(self):
        skill = self.use_office_route()
        service, status = self.run_task('office-five')
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual((status['completed'], status['total']), (12, 12))
        self.assertEqual(FakeEpisode.segments,
                         ['nav_table2', 'pick', 'nav_relay1', 'nav_relay2', 'nav_relay3', 'nav_table1', 'place'])
        session = self.store.sessions()[-1]
        self.assertEqual((session['state'], session['next']), ('completed', 7))
        self.assertEqual(session['segments'], FakeEpisode.segments)
        record = service.runtime.snapshot()
        self.assertEqual(record['object_location'], 'table_1')
        for step in skill.steps:
            if step.kind in {'pick', 'place'}:
                attempt = record['steps'][step.step_id]['attempts'][0]
                proof = record['steps'][step.navigation_step_id]['attempts'][0]['command_id']
                self.assertEqual(attempt['request']['body']['navigation_proof']['dream_command_id'], proof)

    def use_generic(self, actions):
        self.use_office_route()
        root = self.profile['routes']['reception']['endpoints']['dream'].removesuffix('/reception/nav')
        self.profile['routes']['execution'] = dict(mode='simulation', backend='simple_o7', available=True, url=root)
        self.config['brain']['reasoning']['semantic_selection'] = False
        self.model = lambda _: json.dumps({'supported': True, 'unsupported_requirements': [],
                                          'actions': actions, 'reasoning_explanation': 'test planner'})

    def test_generic_single_navigation_finishes_episode(self):
        self.use_generic([{'skill': 'navigate', 'target': 'table2'}])
        service, status = self.run_task('generic-nav', '去二号桌')
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual(FakeEpisode.segments, ['nav_table2'])
        self.assertEqual(self.store.sessions()[-1]['state'], 'completed')
        self.assertTrue(self.store.sessions()[-1]['navigation_only'])
        self.assertEqual(service.runtime.snapshot()['execution_backend'], 'body_simple')

    def test_generic_transfer_uses_common_proofs_and_verification(self):
        self.use_generic([{'skill': 'pick', 'object': 'cola_can_1'},
                          {'skill': 'place', 'object': 'cola_can_1', 'target': 'table_1'}])
        service, status = self.run_task('generic-transfer', '把可乐搬到一号桌')
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual(FakeEpisode.segments, ['nav_table2', 'pick', 'nav_relay1', 'nav_relay2', 'nav_relay3', 'nav_table1', 'place'])
        record = service.runtime.snapshot()
        self.assertEqual(record['object_location'], 'table_1')
        self.assertEqual(record['recovery_policy']['mode'], 'preserve_order')
        place = record['steps']['BODY_PLACE']['attempts'][0]
        nav_id = record['steps']['BODY_NAV_TABLE1']['attempts'][0]['command_id']
        self.assertEqual(place['request']['body']['navigation_proof']['dream_command_id'], nav_id)

    def test_real_generic_route_uses_same_wire_offline_and_no_slaver(self):
        self.use_generic([{'skill': 'pick', 'object': 'cola_can_1'},
                          {'skill': 'place', 'object': 'cola_can_1', 'target': 'table_1'}])
        self.profile['routes']['execution'] = dict(mode='real', backend='body_real', available=True, url='')
        endpoints = self.profile['routes']['reception']['endpoints']
        self.config['reception_real'].update(kernel_enabled=True, dream_base_url=endpoints['dream'], vla_base_url=endpoints['vla'])
        # These are localhost fake physics endpoints, never real robot addresses.
        service, status = self.run_task('real-wire-offline', '把可乐搬到一号桌')
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual(service.runtime.snapshot()['execution_backend'], 'body_real')
        self.assertEqual(service.transports, [])

    def test_generic_failed_navigation_retries_original_step_only_after_continue(self):
        self.use_generic([{'skill': 'pick', 'object': 'cola_can_1'}])
        FakeEpisode.miss_once = 'nav_table2'
        service, status = self.run_task('generic-retry', '去二号桌抓取可乐')
        self.assertEqual(status['state'], 'recovery_required', status)
        record = service.runtime.snapshot()
        original = record['steps']['BODY_NAV_TABLE2']['attempts'][0]['command_id']
        self.assertEqual(FakeEpisode.segments, ['nav_table2'])
        self.assertEqual(self.store.command('nav', original)['state'], 'failed')
        from brain.app import create_service
        restored = create_service(self.config, model=self.model).attach()
        attempt = restored.snapshot()['steps']['BODY_NAV_TABLE2']['attempts'][0]
        self.assertEqual(restored.port.query(original, attempt['request'])['terminal'], 'failed')
        self.assertEqual(FakeEpisode.segments, ['nav_table2'])
        response = self.client.post('/api/task_continue', json={'task_id': 'generic-retry'})
        self.assertEqual(response.status_code, 200, response.json)
        status = self.wait()
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual(FakeEpisode.segments, ['nav_table2', 'nav_table2', 'pick'])
        self.assertEqual(self.store.command('nav', original)['state'], 'failed')

    def test_cancel_during_generic_model_call_never_dispatches_late_plan(self):
        self.use_generic([{'skill': 'navigate', 'target': 'table2'}])
        entered, release = threading.Event(), threading.Event()
        def model(_):
            entered.set()
            release.wait(5)
            return json.dumps({'supported': True, 'unsupported_requirements': [],
                               'actions': [{'skill': 'navigate', 'target': 'table2'}]})
        from brain.app import create_service
        from brain.application import BrainApplication
        from brain.api.app import create_app
        service = create_service(self.config, model=model)
        application = BrainApplication(service).start()
        self.addCleanup(application.close)
        client = create_app(application).test_client()
        self.assertEqual(client.post('/publish_task', json={'task': '去二号桌', 'task_id': 'late-body'}).status_code, 200)
        self.assertTrue(entered.wait(3))
        self.assertEqual(client.post('/api/task_cancel', json={'task_id': 'late-body'}).status_code, 200)
        release.set()
        self.assertTrue(service.runtime.planning_done.wait(5))
        self.assertEqual(service.runtime.snapshot()['state'], 'cancelled')
        self.assertEqual(FakeEpisode.segments, [])
        self.assertEqual(self.store.sessions(), [])

    def test_office_navigation_only_has_five_segments_and_no_manipulation(self):
        self.use_office_route(navigation_only=True)
        service, status = self.run_task('office-nav-five', '开始接待（仅导航）')
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual((status['completed'], status['total']), (7, 7))
        self.assertEqual(FakeEpisode.segments, ['nav_table2', 'nav_relay1', 'nav_relay2', 'nav_relay3', 'nav_table1'])
        session = self.store.sessions()[-1]
        self.assertEqual((session['state'], session['next'], session['holding']), ('completed', 5, None))
        self.assertEqual(session['object_location'], 'table_2')
        self.assertEqual(service.runtime.snapshot()['selection']['sop']['id'], 'reception.single_can.nav_only')
        status, body = self.bridge.reception.handle('POST', '/reception/vla/v1/vla/tasks', {}, {})
        self.assertEqual(status, 501)
        self.assertEqual(body['error']['code'], 'UNSUPPORTED_CAPABILITY')

    def test_navigation_variant_is_rejected_on_full_reception_simulator(self):
        self.use_office_route()
        _, status = self.run_task('wrong-mode', '开始接待（仅导航）')
        self.assertEqual(status['state'], 'recovery_required')
        self.assertEqual(self.store.sessions(), [])

    def test_full_reception_is_rejected_on_navigation_only_simulator(self):
        self.use_office_route(navigation_only=True)
        _, status = self.run_task('wrong-full')
        self.assertEqual(status['state'], 'recovery_required')
        self.assertEqual(self.store.sessions(), [])

    def test_route_mismatch_fails_before_starting_any_episode(self):
        from brain.packages.reception_skill import BASELINE_PATH
        self.config['reception_real']['skill_file'] = str(BASELINE_PATH.with_name('reception_single_can_v2.yaml'))
        _, status = self.run_task('office-not-deployed')
        self.assertEqual(status['state'], 'recovery_required')
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(FakeEpisode.segments, [])

    def test_initialization_failure_returns_terminal_without_starting_action(self):
        with patch.object(reception_episode, 'Episode', side_effect=RuntimeError('CUDA initialization failed')):
            service, status = self.run_task('init-failure')
        self.assertEqual(status['state'], 'recovery_required')
        record = service.runtime.snapshot()
        command = record['steps']['NAVIGATING_TO_TABLE2']['attempts'][0]['command_id']
        result = self.store.command('nav', command)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error']['code'], 'SIMULATION_INIT_FAILED')
        self.assertIs(result['result']['action_started'], False)
        self.assertIsNone(self.store.status()[0])


if __name__ == '__main__':
    unittest.main()
