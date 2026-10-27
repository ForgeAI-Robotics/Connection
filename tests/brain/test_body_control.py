"""Offline control authority / adapter tests. No deployed endpoints are contacted."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from brain.adapters.body_control import ControlDriver, ControlledBodyAdapter
from brain.adapters.http_client import HttpContractError
from brain.kernel.body_control import BodyControl
from brain.storage.tasks import KernelStore
from contracts.body_control import VERSION, REVISION, ControlError, digest, stamp
from contracts.tasks import CONTRACT_VERSION


class Immediate:
    def submit(self, fn, *args):
        result = Future()
        try:
            result.set_result(fn(*args))
        except Exception as exc:
            result.set_exception(exc)
        return result


class Wire:
    def __init__(self):
        self.calls, self.operations, self.commands, self.tasks = [], {}, {}, {}
        self.nav = dict(interface_online=True, active_command_id='', active_command_state=None,
                        output_fenced=True, hold_verified=True, navigation_transport_ready=False,
                        controller_instance_id='sonic-test', proof_context={})
        self.vla = dict(active_command_id='', policy_running=False, output_fenced=True,
                        action_port_released=True, hold_verified=True, holding=None,
                        recovery_required=False, policy_state_uncertain=False, controller_instance_id='sonic-test')
        self.relay = dict(bound=False, forwarding_enabled=False, action_port_released=True,
                          action_endpoint='tcp://192.0.2.10:5556', upstream_fresh=False)
        self.drop_action, self.corrupt_action = None, None
        self.entered, self.unblock = threading.Event(), threading.Event()
        self.block_action = None
        self.stale = False

    def client(self, service):
        return SimpleNamespace(request_json=lambda method, path, payload=None, **kw:
                               (self.call(service, method, path, payload), 200, {}))

    def observed(self, value):
        return dict(deepcopy(value), robot_serial='test-robot',
                    updated_at='2000-01-01T00:00:00+00:00' if self.stale else stamp())

    def nav_command(self, body):
        raw = dict(deepcopy(body), state='succeeded', completed_at=stamp(),
                   result=dict(success=True, reached=True, navigation_stopped=True))
        self.commands[body['command_id']] = raw
        self.nav['proof_context'] = dict(dream_command_id=body['command_id'], target_id=body['target_id'],
            valid=True, pose_within_tolerance=True, map_id='map-test', localization_epoch='loc-test', navigation_epoch='nav-test')
        return raw

    def call(self, service, method, path, payload=None):
        self.calls.append((service, method, path, deepcopy(payload)))
        clean = path.split('?')[0]
        if method == 'POST':
            action = payload['action']
            if action == self.block_action:
                self.entered.set()
                if not self.unblock.wait(5):
                    raise RuntimeError('test blocked too long')
            result = dict(deepcopy(payload), state='succeeded', completed=True)
            ref = dict(control_authorization=payload['control_authorization'], task_id=payload['task_id'],
                       command_id=payload['command_id'])
            if action == 'yield':
                self.nav.update(active_command_id='', output_fenced=True)
                result.update(navigation_stopped=True, output_fenced=True, hold_verified=True)
            elif action == 'close':
                self.relay.update(bound=False, forwarding_enabled=False, action_port_released=True, upstream_fresh=False)
                self.relay.update(ref)
                result.update(self.relay)
            elif action == 'enable_navigation':
                self.relay.update(bound=True, forwarding_enabled=True, action_port_released=False, upstream_fresh=False)
                self.relay.update(ref)
                result.update(self.relay)
            elif action == 'prepare':
                self.nav['output_fenced'] = False
                self.relay['upstream_fresh'] = True
                result.update(mode_ready=True, hold_verified=True, navigation_motion_authorized=False)
                if payload.get('required_holding'):
                    result['holding'] = payload['required_holding']
            elif action == 'stop':
                result.update(policy_stopped=True, publisher_stopped=True, output_fenced=True,
                              action_port_released=True)
            else:
                raise AssertionError((service, method, path))
            result = self.observed(result)
            if action == self.corrupt_action:
                result['control_authorization']['ownership_epoch'] += 100
            self.operations[payload['request_id']] = result
            if action == self.drop_action:
                raise HttpContractError('reply lost after execution')
            return deepcopy(result)
        if '/requests/' in clean:
            return deepcopy(self.operations[clean.rsplit('/', 1)[1]])
        if service == 'nav' and clean.startswith('/v1/commands/'):
            return deepcopy(self.commands[clean.rsplit('/', 1)[1]])
        if service == 'vla' and clean.startswith('/v1/vla/tasks/'):
            return self.observed(self.tasks[clean.rsplit('/', 1)[1]])
        if service == 'nav' and clean == '/v1/status':
            return self.observed(self.nav)
        if service == 'vla' and clean == '/v1/vla/control/status':
            return self.observed(self.vla)
        if service == 'relay' and clean == '/v1/relay/status':
            return self.observed(self.relay)
        raise AssertionError((service, method, path))


def configuration(root):
    return {'reception_real': {'kernel_enabled': True, 'kernel_runtime_dir': str(root),
        'dream_base_url': 'http://nav.invalid', 'vla_base_url': 'http://vla.invalid',
        'body_control': dict(enabled=True, robot_serial='test-robot', action_endpoint='tcp://192.0.2.10:5556',
            relay_base_url='http://relay.invalid', operation_timeout_sec=.03, poll_interval_sec=.001)}}


def nav_request(command='nav-1'):
    return dict(skill='navigate', task_id='test-task', body=dict(contract_version=CONTRACT_VERSION,
        task_id='test-task', command_id=command, target_id='fridge', route_phase='', leg_index=1,
        frame_id='map', goal_xyt=[0, 0, 0], motion_mode='forward_path', require_final_orientation=True))


def vla_request(command='open-1', operation='open_fridge', source='nav-1'):
    return dict(skill=operation, task_id='test-task', body=dict(contract_version=CONTRACT_VERSION,
        task_id='test-task', command_id=command, operation=operation,
        object_id='fridge_1' if operation == 'open_fridge' else 'cola_can_1', target_area='fridge',
        navigation_proof=dict(dream_command_id=source, target_id='fridge', state='succeeded')))


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'BRAIN_CONTROL_TOKEN': 'test-secret-control'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.root = Path(self.temp.name)
        self.config = configuration(self.root)
        self.wire = Wire()
        self.driver = ControlDriver(self.config, clients={k:self.wire.client(k) for k in ('nav','vla','relay')})
        self.authority = BodyControl(self.config, self.root, self.driver, Immediate())
        self.addCleanup(lambda: self.authority.close())
        self.store = KernelStore(self.root)

    def schedule(self, request):
        old = self.store.load_state() or {'revision': 0, 'steps': {}}
        command = request['body']['command_id']
        old.update(task_id=request['body']['task_id'], state='running', execution_backend='reception_real',
                   execution_target={'dream':'http://nav.invalid','vla':'http://vla.invalid'},
                   dispatch_closed=False, control_request='', open_command_id=command)
        old['steps'][command] = {'attempts':[{'attempt_id':command+'-a1','command_id':command,'request':deepcopy(request)}]}
        self.store.save_state(old)

    def register(self, request):
        self.schedule(request)
        row = self.authority.register(request)
        if request['skill'] != 'navigate':
            self.authority.record_submission(request, request['body'])
        return row

    def acquire_body(self, row, request_id='acq-1'):
        return dict(self.authority.identity(row), request_id=request_id, action='acquire',
                    navigation_proof=row['request']['body']['navigation_proof'])

    def acquire_vla(self, command='open-1', operation='open_fridge'):
        row = self.register(vla_request(command, operation))
        reply = self.authority.submit(self.acquire_body(row, 'acq-'+command))
        self.assertEqual(reply['state'], 'authorized', reply)
        return reply

    def release_vla(self, grant, request_id='release-1'):
        exit_state = dict(policy_stopped=True, publisher_stopped=True, output_fenced=True,
                          action_port_released=True, hold_verified=True, holding=None)
        self.wire.tasks[grant['command_id']] = dict(task_id=grant['task_id'], command_id=grant['command_id'],
            control_authorization=self.authority.reference(grant), exit_state=exit_state)
        body = dict({k:grant[k] for k in ('contract_version','coordination_revision','robot_serial','action_endpoint',
                                          'task_id','command_id','lease_id','authority_id','ownership_epoch')},
                    request_id=request_id, action='release', policy_stopped=True,
                    controller_instance_id=grant['controller_instance_id'],
                    exit_state=exit_state, exit_state_sha256=digest(exit_state))
        return body

    def seed_proof(self):
        self.wire.nav_command(nav_request()['body'])

    def test_unscheduled_request_cannot_grant_or_run_io(self):
        with self.assertRaises(ControlError): self.authority.register(nav_request())
        self.assertEqual(self.wire.calls, [])

    def test_nav_to_nav_ends_old_grant_without_vla_or_mode_reinitialization(self):
        first = nav_request(); self.schedule(first)
        grant1 = self.authority.prepare_navigation(first)
        self.wire.nav_command(first['body'])
        second = nav_request('nav-2'); self.schedule(second)
        grant2 = self.authority.prepare_navigation(second)
        self.assertGreater(grant2['ownership_epoch'], grant1['ownership_epoch'])
        self.assertTrue(self.authority.released('test-task', 'nav-1'))
        self.assertEqual([c[3]['action'] for c in self.wire.calls if c[1]=='POST'],
                         ['enable_navigation','prepare','yield','close','enable_navigation','prepare'])
        observed = self.authority.query(grant1['lease_id'], observation_id='new-read')
        self.assertFalse(observed['authorization_current'])
        self.assertEqual(observed['owner'], 'navigation')

    def test_nav_vla_vla_nav_never_restores_nav_on_vla_release(self):
        req = nav_request(); self.schedule(req)
        self.authority.prepare_navigation(req); self.wire.nav_command(req['body'])
        first = self.acquire_vla()
        before = len([c for c in self.wire.calls if c[1]=='POST'])
        released = self.authority.submit(self.release_vla(first))
        self.assertEqual(released['state'], 'released', released)
        self.assertNotIn('navigation_port_ready', released)
        second = self.acquire_vla('grasp-1', 'grasp_cola')
        self.assertGreater(second['ownership_epoch'], first['ownership_epoch'])
        self.assertEqual(len([c for c in self.wire.calls if c[1]=='POST']), before)
        self.authority.submit(self.release_vla(second, 'release-2'))
        req = nav_request('delivery'); self.schedule(req)
        self.authority.prepare_navigation(req)
        self.assertTrue(self.wire.relay['bound'])

    def test_same_request_is_historical_and_changed_request_conflicts(self):
        self.seed_proof()
        row = self.register(vla_request())
        body = self.acquire_body(row)
        result = self.authority.submit(body)
        self.assertEqual(result['state'], 'authorized')
        self.authority.submit(self.release_vla(result))
        count = len(self.wire.calls)
        self.assertEqual(self.authority.submit(body), result)
        self.assertEqual(len(self.wire.calls), count)
        with self.assertRaises(ControlError):
            self.authority.submit(dict(body, requirements={'changed':True}))
        self.assertFalse(self.authority.query(row['lease_id'], observation_id='current')['authorization_current'])

    def test_lost_post_reply_queries_original_without_resending(self):
        self.wire.drop_action = 'enable_navigation'
        req = nav_request(); self.schedule(req)
        self.authority.prepare_navigation(req)
        posts = [c for c in self.wire.calls if c[1]=='POST' and c[3]['action']=='enable_navigation']
        gets = [c for c in self.wire.calls if c[0]=='relay' and '/requests/' in c[2]]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(gets), 1)
        self.assertIn(posts[0][3]['request_id'], gets[0][2])

    def test_bad_echo_or_stale_status_never_grants(self):
        self.wire.corrupt_action = 'prepare'
        req = nav_request(); self.schedule(req)
        with self.assertRaises(ControlError): self.authority.prepare_navigation(req)
        row = self.authority.register(req)
        self.assertFalse(self.authority.query(row['lease_id'], observation_id='check')['authorization_current'])
        count = len(self.wire.calls)
        with self.assertRaises(ControlError): self.authority.prepare_navigation(req)
        self.assertEqual(count, len(self.wire.calls))

    def test_empty_ledger_is_not_evidence_of_idle(self):
        self.wire.vla['output_fenced'] = None
        req = nav_request(); self.schedule(req)
        with self.assertRaises(ControlError): self.authority.prepare_navigation(req)
        self.assertFalse(any(c[1]=='POST' for c in self.wire.calls))

    def test_release_rejects_changed_digest_and_missing_hold(self):
        self.seed_proof(); grant = self.acquire_vla()
        body = self.release_vla(grant)
        with self.assertRaises(ControlError): self.authority.submit(dict(body, exit_state_sha256='bad'))
        body['exit_state']['hold_verified'] = False
        body['exit_state_sha256'] = digest(body['exit_state'])
        reply = self.authority.submit(body)
        self.assertEqual(reply['state'], 'unconfirmed')
        self.assertFalse(self.authority.released('test-task', 'open-1'))

    def test_controller_instance_change_cannot_release_using_old_hold_evidence(self):
        self.seed_proof(); grant = self.acquire_vla()
        body = self.release_vla(grant)
        self.wire.vla['controller_instance_id'] = 'restarted-sonic'
        reply = self.authority.submit(body)
        self.assertEqual(reply['state'], 'unconfirmed')
        self.assertFalse(self.authority.released('test-task', 'open-1'))

    def test_restart_invalidates_current_but_preserves_history_and_allows_exit(self):
        self.seed_proof(); grant = self.acquire_vla()
        release = self.release_vla(grant)
        original_id = self.authority.authority_id
        self.authority.close()
        self.authority = BodyControl(self.config, self.root, self.driver, Immediate())
        observed = self.authority.query(grant['lease_id'], observation_id='after-restart')
        self.assertNotEqual(original_id, observed['authority_id'])
        self.assertFalse(observed['authorization_current'])
        self.assertEqual(observed['issued_authority_id'], original_id)
        new = self.register(vla_request('new-task'))
        self.assertEqual(self.authority.submit(self.acquire_body(new, 'new-acquire'))['state'], 'unconfirmed')
        self.assertEqual(self.authority.submit(release)['state'], 'released')
        self.assertEqual(self.authority.query(grant['lease_id'], request_id='acq-open-1')['state'], 'authorized')

    def test_same_resource_directory_has_one_authority(self):
        self.authority.start()
        other = BodyControl(self.config, self.root, self.driver, Immediate())
        with self.assertRaises(OSError): other.start()
        other.close()

    def test_cancellation_during_prepare_cannot_commit_late_authorization(self):
        self.wire.block_action = 'prepare'
        req = nav_request(); self.schedule(req)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.authority.prepare_navigation, req)
            self.assertTrue(self.wire.entered.wait(2))
            task = self.store.load_state(); task.update(state='cancelled', dispatch_closed=True)
            self.store.save_state(task)
            self.wire.unblock.set()
            with self.assertRaises(ControlError): pending.result(2)
        row = next(iter(self.authority.store.read()['leases'].values()))
        self.assertFalse(self.authority.query(row['lease_id'], observation_id='late')['authorization_current'])
        self.assertTrue(self.wire.relay['bound'])  # Partial physical effects are not hidden.

    def test_http_auth_identity_and_historical_get_never_mutate(self):
        from flask import Flask
        from brain.api.body_control import register_body_control
        self.seed_proof(); row = self.register(vla_request())
        app = SimpleNamespace(body_control=self.authority, start=lambda:None)
        http = Flask(__name__)
        register_body_control(http, app)
        client = http.test_client()
        path = '/v1/vla-handoff/leases/'+row['lease_id']
        body = self.acquire_body(row)
        self.assertEqual(client.post(path+'/acquire', json=body).status_code, 401)
        headers = {'Authorization':'Bearer test-secret-control'}
        result = client.post(path+'/acquire', json=body, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['state'], 'authorized')
        count = len(self.wire.calls)
        observed = client.get(path+'?observation_id=read-001', headers=headers)
        self.assertTrue(observed.json['authorization_current'])
        self.assertEqual(observed.headers['Cache-Control'], 'no-store')
        self.assertEqual(len(self.wire.calls), count)
        self.assertEqual(client.get(path+'?observation_id=x&observation_id=y', headers=headers).status_code, 400)
        self.assertNotIn('test-secret-control', (self.root/'body_control.json').read_text())

    def test_stale_time_is_rejected_before_any_control_write(self):
        self.wire.stale = True
        req = nav_request(); self.schedule(req)
        with self.assertRaises(ControlError): self.authority.prepare_navigation(req)
        self.assertFalse(any(c[1]=='POST' for c in self.wire.calls))

    def test_nested_peer_receipt_preserves_identity_and_unknown_facts(self):
        row = self.register(vla_request())
        row['ownership_epoch'] = 1
        payload = dict(self.driver.payload(row, 'stop'), request_id='stop-test')
        record = dict(request=deepcopy(payload), status='completed', updated_at=stamp(),
                      exit_state={'policy_stopped':True,'publisher_stopped':None})
        result = self.driver.operation_receipt(record,payload)
        self.assertEqual(result['control_authorization'],payload['control_authorization'])
        self.assertIsNone(result['publisher_stopped'])
        with self.assertRaises(ControlError):self.driver.require(result,'publisher_stopped')
        changed=deepcopy(record);changed['request']['ownership_epoch']=True
        with self.assertRaises(ControlError):self.driver.operation_receipt(changed,payload)
        changed=deepcopy(record);changed['command_id']='foreign-command'
        with self.assertRaises(ControlError):self.driver.operation_receipt(changed,payload)
        changed=deepcopy(record);changed['policy_stopped']=False
        with self.assertRaises(ControlError):self.driver.operation_receipt(changed,payload)

    def test_nav_evidence_mapping_does_not_invent_robot_or_holding_facts(self):
        request=nav_request();row=self.register(request);row['ownership_epoch']=1
        payload=dict(self.driver.payload(row,'prepare'),request_id='nav-prepare-test')
        association={key:payload[key] for key in ('task_id','command_id','control_authorization')}
        receipt=dict(association,contract_version=VERSION,coordination_revision=REVISION,
            request_id=payload['request_id'],action='prepare',state='completed',accepted=True,
            evidence={'mode_confirmed':True,'posture_preserved':True,'navigation_not_started':True},
            execution_authority=False,historical=False,current_local_state='prepared',current_local_association=association)
        viewed=self.driver.operation_receipt(receipt,payload,'nav')
        self.assertTrue(viewed['mode_ready'])
        self.assertNotIn('hold_verified',viewed)
        self.assertNotIn('robot_serial',viewed)
        with self.assertRaises(ControlError):self.driver.operation_receipt(dict(receipt,historical=True),payload,'nav')
        viewed=self.driver.operation_receipt(dict(receipt,current_local_state='preparing'),payload,'nav')
        self.assertEqual(viewed['state'],'pending')

    def test_release_can_omit_redundant_echo_but_current_controller_must_match(self):
        self.seed_proof();grant=self.acquire_vla()
        body=self.release_vla(grant);body.pop('controller_instance_id')
        self.assertEqual(self.authority.submit(body)['state'],'released')

    def test_acquire_rejects_callers_stale_authority_before_hardware_io(self):
        self.seed_proof();row=self.register(vla_request())
        body=dict(self.acquire_body(row),authority_id='another-brain-instance')
        with self.assertRaises(ControlError):self.authority.submit(body)
        self.assertFalse(self.wire.calls)

    def test_stop_callback_can_release_while_stop_request_is_in_flight(self):
        self.seed_proof(); grant = self.acquire_vla()
        release = self.release_vla(grant)
        original = self.wire.call
        def call(service, method, path, payload=None):
            if method == 'POST' and payload['action'] == 'stop':
                self.assertEqual(self.authority.submit(release)['state'], 'released')
            return original(service, method, path, payload)
        self.wire.call = call
        result = self.authority.stop_vla('test-task', 'open-1')
        self.assertEqual(result['state'], 'stopped', result)
        self.assertTrue(self.authority.released('test-task', 'open-1'))
        count = len(self.wire.calls)
        self.authority.stop_vla('test-task', 'open-1')
        self.assertEqual(len(self.wire.calls), count)

    def test_business_submission_intent_is_exact_and_never_replayed(self):
        req = nav_request(); self.schedule(req)
        ref = self.authority.prepare_navigation(req)
        body = dict(req['body'], control_authorization=ref)
        self.authority.record_submission(req, body)
        with self.assertRaises(ControlError): self.authority.record_submission(req, body)
        saved = self.authority.store.read()['leases'][ref['lease_id']]['business_submission']['body']
        self.assertEqual(saved, body)
        self.assertNotIn('control_authorization', self.store.load_state()['steps']['nav-1']['attempts'][0]['request']['body'])

    def test_concurrent_acquire_is_single_operation_and_current_queries_do_not_wait(self):
        req = nav_request(); self.schedule(req)
        self.authority.prepare_navigation(req); self.wire.nav_command(req['body'])
        row = self.register(vla_request()); body = self.acquire_body(row)
        self.wire.block_action = 'yield'
        with ThreadPoolExecutor(1) as pool:
            self.authority.executor = pool
            try:
                first = self.authority.submit(body)
                self.assertEqual(first['state'], 'pending')
                self.assertTrue(self.wire.entered.wait(2))
                self.assertEqual(self.authority.submit(body)['state'], 'pending')
                with self.assertRaises(ControlError):
                    self.authority.submit(dict(body, request_id='different-acquire'))
                self.assertFalse(self.authority.query(row['lease_id'], observation_id='while-pending')['authorization_current'])
            finally:
                self.wire.unblock.set()
        self.assertEqual(self.authority.query(row['lease_id'], request_id=body['request_id'])['state'], 'authorized')
        self.assertEqual(len([c for c in self.wire.calls if c[1]=='POST' and c[3]['action']=='yield']), 1)

    def test_application_owns_enabled_authority_and_disabled_route_is_inert(self):
        from brain.app import create_service
        from brain.application import BrainApplication
        from brain.api.app import create_app
        separate = configuration(self.root/'application')
        separate.update(brain={'capture_timeline':False}, reflection={'enabled':False})
        service = create_service(separate, model=lambda _: '')
        application = BrainApplication(service)
        self.addCleanup(application.close)
        self.assertIs(service.body_control, application.body_control)
        client = create_app(application).test_client()
        headers = {'Authorization':'Bearer test-secret-control'}
        self.assertEqual(client.get('/v1/vla-handoff/capabilities', headers=headers).status_code, 200)
        self.assertTrue(service._port('reception_real').requires_authoritative_handoff)
        config = configuration(self.root/'disabled')
        config['reception_real']['body_control']['enabled'] = False
        app2 = BrainApplication(create_service(config, model=lambda _: ''))
        self.addCleanup(app2.close)
        response = create_app(app2).test_client().get('/v1/vla-handoff/capabilities', headers=headers)
        self.assertEqual(response.status_code, 501)
        self.assertFalse((self.root/'disabled'/'body_control.json').exists())


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        from brain.app import create_service
        from brain.packages.reception_skill import BASELINE_PATH
        from tests.brain.test_native_vla import FakeDream, FakeVla
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'BRAIN_CONTROL_TOKEN':'test-secret-control',
            'FQ_EXECUTION_STATE':str(root/'execution.json'), 'FQ_EXECUTION_LOCK':str(root/'execution.lock'),
            'FQ_EXECUTION_BLOCK':str(root/'execution.blocked')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.config = configuration(root/'tasks')
        self.config.update(brain={'reception_backend':'real','capture_timeline':False,
                                  'reasoning':{'recovery_advice':False}}, reflection={'enabled':False})
        self.config['reception_real']['skill_file'] = str(BASELINE_PATH.with_name('reception_single_can_v2.yaml'))
        self.wire = Wire()
        self.driver = ControlDriver(self.config, clients={k:self.wire.client(k) for k in ('nav','vla','relay')})
        self.authority = BodyControl(self.config, root/'tasks', self.driver, Immediate())
        self.addCleanup(self.authority.close)
        outer = self

        class Nav(FakeDream):
            def status(self):
                return outer.wire.observed(outer.wire.nav)

            def submit_navigation(self, body):
                outer.wire.nav_command(body)
                self.commands = outer.wire.commands
                return {'accepted': True}

        class Vla(FakeVla):
            def control_status(self):
                result = super().control_status()
                active = result['active_command_id']
                running = bool(active and self.commands[active]['state'] not in {'acquiring_action_port'})
                result.update(policy_running=running, output_fenced=not running, hold_verified=True,
                              action_port_released=not running, navigation_handoff_ready=False,
                              controller_instance_id='sonic-test',
                              action_port={'navigation_port_ready':False})
                if getattr(self, 'holding_proof', None):
                    result.update(deepcopy(self.holding_proof))
                    result['current_holding'] = self.holding
                return result

            def submit_task(self, body):
                super().submit_task(body)
                self.commands[body['command_id']]['state'] = 'acquiring_action_port'
                request = outer.service.runtime._request_of(body['command_id'])
                row = outer.authority.register(request)
                grant = outer.authority.submit(dict(outer.authority.identity(row), action='acquire',
                    request_id='acq-'+body['command_id'], navigation_proof=body['navigation_proof']))
                if grant['state'] != 'authorized':
                    raise RuntimeError(str(grant))
                self.commands[body['command_id']].update(state='running',
                    control_authorization=outer.authority.reference(grant))
                return {'accepted':True}

            def finish(self, task_id, command_id, observed, authorization):
                super().finish(task_id, command_id, observed, authorization)
                if self.commands[command_id]['operation'] == 'grasp_cola':
                    from tests.brain.holding_fixture import holding_status
                    self.holding_proof = holding_status(task_id, command_id,
                        self.commands[command_id]['control_authorization'], observed=observed)
                self.exit_control(command_id)
                return self.task(command_id)

            def cancel(self, task_id, command_id):
                super().cancel(task_id, command_id)
                self.exit_control(command_id)
                return {'accepted':True}

            def exit_control(self, command):
                task = self.commands[command]
                task['result'].update(navigation_port_ready=False, publisher_stopped=True)
                task['exit_state'] = dict(policy_stopped=True, publisher_stopped=True, output_fenced=True,
                    action_port_released=True, hold_verified=True, holding=self.holding)
                row = outer.authority.store.read()['leases'][task['control_authorization']['lease_id']]
                result = outer.authority.submit(dict(outer.authority.identity(row),
                    **outer.authority.reference(row), action='release', request_id='release-'+command,
                    controller_instance_id=row['evidence']['controller_instance_id'],
                    policy_stopped=True, exit_state=task['exit_state'], exit_state_sha256=digest(task['exit_state'])))
                if result['state'] != 'released':
                    raise RuntimeError(str(result))

        self.nav, self.vla = Nav(), Vla()
        self.wire.tasks = self.vla.commands
        original = self.wire.call

        def call(service, method, path, payload=None):
            if service == 'vla' and method == 'GET' and path.startswith('/v1/vla/control/status'):
                self.wire.calls.append((service, method, path, None))
                return self.wire.observed(self.vla.control_status())
            return original(service, method, path, payload)
        self.wire.call = call
        self.port = ControlledBodyAdapter(self.nav, self.vla, self.authority)
        self.service = create_service(self.config, model=lambda _:self.fail('No LLM in fixed workflow'),
                                      port_factory=lambda _:self.port)

    def finish(self):
        from tests.brain.test_native_vla import evidence
        runtime = self.service.runtime
        command = runtime.record['open_command_id']
        self.service.vla_effect('joint-reception', command, 'pause')
        self.service.vla_effect('joint-reception', command, 'finish',
            evidence=evidence(self.vla.task(command)['operation']), authorization='Bearer test-only')

    def test_whole_runtime_open_grasp_delivery_keeps_nav_transport_closed_between_vla(self):
        self.service.publish('开始接待', 'joint-reception', options={'reflect':False})
        runtime = self.service.runtime
        self.assertEqual(runtime.state, 'waiting_human', runtime.public_status())
        self.assertEqual(len(self.nav.commands), 1)
        self.assertFalse(self.wire.relay['bound'])
        nav_writes = len([c for c in self.wire.calls if c[1]=='POST'])
        self.finish()
        self.service.control('continue', task_id='joint-reception')
        self.assertEqual(runtime.state, 'waiting_human', runtime.public_status())
        self.assertEqual([x['operation'] for x in self.vla.posts], ['open_fridge','grasp_cola'])
        self.assertEqual(len([c for c in self.wire.calls if c[1]=='POST']), nav_writes)
        self.finish()
        self.service.control('continue', task_id='joint-reception')
        self.assertEqual(runtime.state, 'succeeded', runtime.public_status())
        self.assertEqual(len(self.nav.commands), 5)
        self.assertFalse(self.wire.relay['bound'])
        self.assertIsNone(self.authority.store.read()['current'])
        self.assertTrue(all(v['state']=='released' for v in self.authority.store.read()['leases'].values()))
        self.assertEqual(len(self.vla.posts), 2)
        self.assertNotIn('test-secret-control', json.dumps(runtime.record))

    def test_current_holding_loss_blocks_delivery_without_restoring_relay(self):
        self.service.publish('开始接待', 'joint-reception', options={'reflect':False})
        self.finish(); self.service.control('continue', task_id='joint-reception')
        self.finish(); self.vla.holding = None
        self.service.control('continue', task_id='joint-reception')
        self.assertEqual(self.service.runtime.state, 'recovery_required')
        self.assertEqual(len(self.nav.commands), 1)
        self.assertFalse(self.wire.relay['bound'])

    def test_manual_skip_cannot_synthesize_control_handoff(self):
        self.service.publish('开始接待', 'joint-reception', options={'reflect':False})
        runtime = self.service.runtime
        runtime.request_pause()
        runtime.skip_current(step_id='VLA_OPEN_FRIDGE')
        self.wire.stale = True
        answer = runtime._handoff('VLA_GRASP_COLA', 'to_vla')
        self.assertFalse(answer['confirmed'])
        self.assertNotEqual(answer['reason'], 'manual_skip')
        self.assertEqual(len(self.vla.posts), 1)

    def test_navigation_business_refusal_releases_preparation_without_claiming_execution(self):
        def reject(body):
            raise HttpContractError('rejected', status_code=409, payload={'success':False,
                'error':{'code':'SAFETY_NOT_READY','message':'fixture refuses navigation',
                         'details':{'task_id':body['task_id'],'command_id':body['command_id'],
                                    'action_started':False,'action_state_uncertain':False}}})
        with patch.object(self.nav, 'submit_navigation', side_effect=reject):
            self.service.publish('开始接待', 'joint-reception', options={'reflect':False})
        runtime = self.service.runtime
        self.assertEqual(runtime.state, 'recovery_required')
        attempts = runtime.record['steps']['NAVIGATING_TO_FRIDGE']['attempts']
        self.assertIn('submission_rejected', attempts[-1]['progress']['evidence'])
        self.assertFalse(self.wire.relay['bound'])
        self.assertIsNone(self.authority.store.read()['current'])

    def test_repeated_business_submit_is_not_reclassified_as_not_started(self):
        self.service.publish('开始接待', 'joint-reception', options={'reflect':False})
        runtime = self.service.runtime
        command = runtime.record['open_command_id']
        request = runtime._request_of(command)
        result = self.port.submit(command, request)
        self.assertTrue(result['unclear'])
        self.assertNotIn('submission_rejected', result)
        self.assertEqual(len(self.vla.posts), 1)


if __name__ == '__main__':
    unittest.main()
