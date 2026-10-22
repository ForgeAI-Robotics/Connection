from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from brain.adapters.execution import BodyAdapter, interpret_observation
from brain.adapters.http_client import HttpContractError
from brain.adapters.vla_client import VlaClient
from brain.skills.native_vla import catalog, compile_plan, require_operations
from brain.storage.tasks import KernelStore
from contracts.tasks import CONTRACT_VERSION, Rejected
from contracts.vla import validate_evidence
from tests.factories import TaskRuntime


def now():
    return datetime.now(timezone.utc).isoformat()


def capabilities():
    return dict(contract_version=CONTRACT_VERSION, real_action_enabled=True, operations={
        key: {'configured': key in {'open_fridge', 'grasp_cola'}}
        for key in ('open_fridge', 'grasp_cola', 'close_fridge', 'pick', 'place')})


def settings():
    # Synthetic offline point only, never a deployment coordinate.
    return {'reception_real': {'native_vla': {
        'points': {'fridge': dict(target_id='test_fridge', route_phase='', leg_index=1,
                                 goal_xyt=[1, 2, 0], motion_mode='forward_path')},
        'operations': {op: dict(object_id=obj, route=['fridge'], timeout_sec=60)
                       for op, obj in [('open_fridge', 'fridge_1'), ('grasp_cola', 'cola_can_1')]}}}}


def planned(*operations):
    return compile_plan(dict(supported=True, unsupported_requirements=[],
                             actions=[{'skill': op} for op in operations]), catalog(settings(), capabilities()))


def evidence(operation='open_fridge'):
    result = dict(source='operator_observation', reference='offline-observation-1', observed_at=now())
    if operation == 'open_fridge':
        result.update(door_open=True, door_stable_open=True, hands_clear_of_door=True)
    else:
        result.update(object_grasped=True, holding='cola_can_1', clear_of_fridge=True)
    return result


class FakeDream:
    def __init__(self):
        self.commands = {}

    def status(self):
        return dict(active_command_id='', active_command_state=None, navigation_transport_ready=True)

    def submit_navigation(self, body):
        self.commands[body['command_id']] = dict(body, state='succeeded', completed_at=now(),
            result=dict(success=True, reached=True, navigation_stopped=True))
        return {'accepted': True}

    def command(self, command_id):
        return deepcopy(self.commands[command_id])

    def wait_command(self, command_id, **kwargs):
        return self.command(command_id)


class FakeVla:
    def __init__(self):
        self.commands = {}
        self.posts = []
        self.pause_calls = 0
        self.finish_calls = 0
        self.cancels = []
        self.lost_finish = False
        self.status_override = {}
        self.holding = None

    def capabilities(self):
        return capabilities()

    def control_status(self):
        active = next((k for k, v in self.commands.items() if v['state'] not in {'succeeded', 'failed', 'cancelled'}), '')
        value = dict(active_command_id=active, policy_running=bool(active),
            action_port={'navigation_port_ready': not active}, navigation_handoff_ready=True,
            recovery_required=False, policy_state_uncertain=False, holding=self.holding)
        value.update(self.status_override)
        return value

    def submit_task(self, body):
        self.posts.append(deepcopy(body))
        self.commands[body['command_id']] = dict(body, state='running', started_at=now(), result={})
        return {'accepted': True}

    def task(self, command_id):
        return deepcopy(self.commands[command_id])

    def wait_task(self, command_id, **kwargs):
        return self.task(command_id)

    def pause(self, task_id, command_id):
        self.pause_calls += 1
        self.commands[command_id].update(state='waiting_result_confirmation', pause_requested=True,
                                         verification_ready_at=now())
        return self.task(command_id)

    def finish(self, task_id, command_id, observed, authorization):
        self.finish_calls += 1
        if authorization != 'Bearer test-only':
            raise HttpContractError('unauthorized', status_code=403)
        task = self.commands[command_id]
        result = dict(success=True, object_id=task['object_id'], policy_stopped=True, navigation_port_ready=True,
            evidence=deepcopy(observed), effect_verified_by=observed['source'], evidence_level=observed['source'],
            autonomous_effect_verification=False)
        if task['operation'] == 'open_fridge':
            result.update(door_open=True, door_closed=False)
        else:
            result.update(object_grasped=True, holding=task['object_id'])
            self.holding = task['object_id']
        task.update(state='succeeded', completed_at=now(), finish_requested=True, result=result)
        if self.lost_finish:
            raise HttpContractError('lost reply')
        return self.task(command_id)

    def cancel(self, task_id, command_id):
        self.cancels.append(command_id)
        self.commands[command_id].update(state='cancelled', completed_at=now(),
            result=dict(success=False, object_id=self.commands[command_id]['object_id'],
                        policy_stopped=True, navigation_port_ready=True))
        return {'accepted': True}


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vla, self.nav = FakeVla(), FakeDream()
        self.port = BodyAdapter(self.nav, self.vla)
        self.runtime = TaskRuntime(KernelStore(self.temp.name), self.port, config={}, package='generic')
        self.runtime.open_task('native-test', phases=planned('open_fridge', 'grasp_cola').steps)
        self.runtime.drive()

    @property
    def command(self):
        return self.runtime.record['open_command_id']

    def control(self, action, **kwargs):
        return self.runtime.vla_effect('native-test', self.command, action, **kwargs)

    def finish(self):
        self.control('pause')
        operation = self.vla.task(self.command)['operation']
        self.control('finish', evidence=evidence(operation), authorization='Bearer test-only')

    def test_full_two_action_workflow_preserves_identity_and_requires_continue(self):
        self.assertEqual(self.runtime.state, 'waiting_human')
        self.assertEqual(len(self.vla.posts), 1)
        original = self.command
        self.control('query')
        self.assertEqual(self.command, original)
        with self.assertRaises(Rejected):
            self.runtime.continue_current()
        self.finish()
        self.assertEqual(self.runtime.state, 'waiting_human')
        self.assertEqual(len(self.vla.posts), 1)
        self.runtime.continue_current()
        self.runtime.drive()
        self.assertEqual(self.runtime.state, 'waiting_human')
        self.assertEqual(len(self.vla.posts), 2)
        self.assertNotEqual(self.command, original)
        self.finish()
        self.runtime.continue_current()
        self.runtime.drive()
        self.assertEqual(self.runtime.state, 'succeeded')
        self.assertEqual(self.runtime.record['holding'], 'cola_can_1')
        self.assertNotIn('test-only', json.dumps(self.runtime.record))

    def test_duplicate_pause_and_lost_finish_reply_do_not_repeat_posts(self):
        self.control('pause'); self.control('pause')
        self.assertEqual(self.vla.pause_calls, 1)
        observed = evidence()
        self.vla.lost_finish = True
        with self.assertRaises(Rejected):
            self.control('finish', evidence=observed, authorization='Bearer test-only')
        # Restore from the same ledger; no automatic POST on recovery.
        self.runtime = TaskRuntime(KernelStore(self.temp.name), self.port, config={}, package='generic')
        self.control('finish', evidence=observed, authorization='Bearer test-only')
        self.assertEqual(self.vla.finish_calls, 1)
        self.assertEqual(len(self.vla.posts), 1)
        with self.assertRaises(Rejected):
            self.control('finish', evidence=dict(observed, reference='other'), authorization='Bearer test-only')

    def test_stale_evidence_and_unauthorized_finish_can_be_corrected(self):
        old = evidence()
        self.control('pause')
        for observed, credential in [(old, 'Bearer test-only'), (evidence(), ''), (evidence(), 'Bearer wrong')]:
            with self.assertRaises(Rejected):
                self.control('finish', evidence=observed, authorization=credential)
        self.control('finish', evidence=evidence(), authorization='Bearer test-only')
        self.assertEqual(self.vla.task(self.command)['state'], 'succeeded')

    def test_task_pause_cancels_command_but_verification_pause_does_not(self):
        self.control('pause')
        self.assertEqual(self.vla.cancels, [])
        command = self.command
        self.runtime.request_pause()
        self.assertEqual(self.vla.cancels, [command])
        self.assertEqual(self.runtime.state, 'paused')
        with self.assertRaises(Rejected):
            self.control('finish', evidence=evidence(), authorization='Bearer test-only')

    def test_shutdown_does_not_leave_waiting_native_policy_unchecked(self):
        from types import SimpleNamespace
        from brain.application import BrainApplication
        owner = SimpleNamespace(config={'reception_real': {'kernel_runtime_dir': self.temp.name}},
                                service=SimpleNamespace(attach=lambda: self.runtime))
        command = self.command
        BrainApplication._checkpoint(owner)
        self.assertEqual(self.runtime.state, 'paused')
        self.assertEqual(self.vla.cancels, [command])

    def test_cancelled_task_and_wrong_identity_cannot_finish(self):
        with self.assertRaises(Rejected):
            self.runtime.vla_effect('other', self.command, 'pause')
        with self.assertRaises(Rejected):
            self.runtime.vla_effect('native-test', 'other', 'pause')
        command = self.command
        self.runtime.request_cancel()
        with self.assertRaises(Rejected):
            self.runtime.vla_effect('native-test', command, 'finish', evidence=evidence(), authorization='Bearer test-only')
        self.assertEqual(self.vla.finish_calls, 0)
        self.assertEqual(self.vla.cancels, [])

    def test_query_late_after_cancel_does_not_overwrite_task(self):
        entered, release = threading.Event(), threading.Event()
        original = self.port.effect_control
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set(); release.wait(3)
            return result
        errors = []
        def query():
            try: self.control('query')
            except Rejected as exc: errors.append(str(exc))
        with patch.object(self.port, 'effect_control', side_effect=delayed):
            worker = threading.Thread(target=query)
            worker.start()
            self.assertTrue(entered.wait(2))
            self.runtime.request_cancel()
            release.set(); worker.join(3)
        self.assertEqual(self.runtime.state, 'cancelled')
        self.assertTrue(errors)

    def test_bad_effect_or_unreleased_resource_never_passes(self):
        self.finish()
        command = self.command
        self.vla.commands[command]['result']['navigation_port_ready'] = False
        self.runtime.continue_current(); self.runtime.drive()
        self.assertEqual(self.runtime.state, 'recovery_required')
        self.assertEqual(len(self.vla.posts), 1)

    def test_success_receipt_does_not_override_current_recovery_requirement(self):
        self.finish()
        self.vla.status_override['recovery_required'] = True
        self.runtime.continue_current(); self.runtime.drive()
        self.assertEqual(self.runtime.state, 'recovery_required')
        self.assertEqual(len(self.nav.commands), 1)
        self.assertEqual(len(self.vla.posts), 1)

    def test_quarantine_blocks_navigation_even_after_explicit_skip(self):
        command = self.command
        self.runtime.request_pause()
        self.vla.status_override['recovery_required'] = True
        self.runtime.skip_current(step_id='VLA_OPEN_FRIDGE')
        self.runtime.drive()
        self.assertEqual(len(self.nav.commands), 1)
        self.assertEqual(len(self.vla.posts), 1)
        self.assertEqual(self.runtime.state, 'recovery_required')
        self.assertEqual(self.vla.cancels, [command])

    def test_skip_does_not_fabricate_door_open_precondition(self):
        self.runtime.request_pause()
        self.runtime.skip_current(step_id='VLA_OPEN_FRIDGE')
        self.runtime.drive()
        self.assertEqual(len(self.vla.posts), 1)
        self.assertEqual(self.runtime.state, 'recovery_required')
        self.assertIn('开门通过证据', self.runtime.record['blocked_reason'])

    def test_wrong_target_and_observation_after_completion_are_not_accepted(self):
        self.finish()
        request = self.runtime._request_of(self.command)
        raw = self.vla.task(self.command)
        bad = dict(raw, target_area='different')
        self.assertFalse(interpret_observation(self.command, bad, request)['evidence']['identity_ok'])
        raw['completed_at'] = raw['verification_ready_at']
        self.assertFalse(interpret_observation(self.command, raw, request)['evidence']['time_ok'])


class NativePlanningTests(unittest.TestCase):
    def test_new_plan_and_contract_are_explicit(self):
        steps = planned('open_fridge', 'grasp_cola').steps
        self.assertEqual([s.kind for s in steps], ['navigate', 'open_fridge', 'verify', 'navigate', 'grasp_cola', 'verify'])
        from brain.packages.registry import PackagePolicy
        from contracts.vla import validate_request
        body = PackagePolicy().request(steps[1], 'task', 'command', lambda _: 'original-nav')['body']
        validate_request(body)
        self.assertEqual(body['navigation_proof']['dream_command_id'], 'original-nav')

    def test_missing_scene_unknown_actions_and_incomplete_plan_rejected(self):
        for actions in [('grasp_cola',), ('close_fridge',), ('place',), ('open_fridge', 'open_fridge')]:
            with self.subTest(actions=actions), self.assertRaises(Rejected): planned(*actions)
        value = dict(supported=True, unsupported_requirements=[], actions=[{'skill': 'open_fridge'}])
        with self.assertRaises(Rejected): compile_plan(value, catalog({}, capabilities()))
        with self.assertRaises(Rejected): compile_plan(dict(value, unsupported_requirements=['place']), catalog(settings(), capabilities()))
        with self.assertRaises(Rejected): compile_plan(dict(value, actions=[{'skill':'open_fridge', 'goal_xyt':[0,0,0]}]), catalog(settings(), capabilities()))

    def test_legacy_sop_rejected_before_navigation_on_native_service(self):
        from brain.packages.reception_skill import load_skill
        with self.assertRaises(Rejected): require_operations(load_skill().steps, capabilities())
        require_operations(load_skill().steps, None)
        caps = capabilities(); caps['real_action_enabled'] = False
        with self.assertRaises(Rejected): require_operations(planned('open_fridge').steps, caps)

    def test_effect_evidence_requires_fresh_matching_observation(self):
        ready = now()
        validate_evidence('open_fridge', 'fridge_1', evidence(), ready)
        for changed in [dict(evidence(), source='model'), dict(evidence(), hands_clear_of_door=False),
                        dict(evidence(), observed_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())]:
            with self.assertRaises(ValueError): validate_evidence('open_fridge', 'fridge_1', changed, ready)


class NativeClientTests(unittest.TestCase):
    def test_unavailable_executor_needs_explicit_nonexecution_proof(self):
        from contracts.submission import submission_refusal
        payload = {'success':False, 'error':{'code':'CAPABILITY_UNAVAILABLE', 'details':{
            'action_started':False, 'action_state_uncertain':False}}}
        self.assertIsNotNone(submission_refusal(501, payload, command_id='cmd', task_id='task'))
        payload['error']['details'].pop('action_state_uncertain')
        self.assertIsNone(submission_refusal(501, payload, command_id='cmd', task_id='task'))

    def test_missing_handoff_or_quarantine_refuses_before_task_post(self):
        from brain.packages.registry import PackagePolicy
        step = planned('open_fridge').steps[1]
        request = PackagePolicy().request(step, 'task', 'cmd', lambda _: 'nav')
        for field, value in [('navigation_handoff_ready',False), ('policy_state_uncertain',True),
                             ('recovery_required',True)]:
            vla = FakeVla(); vla.status_override[field] = value
            result = BodyAdapter(FakeDream(), vla).submit('cmd', request)
            self.assertFalse(result['accepted'])
            self.assertFalse(result['unclear'])
            self.assertTrue(result['submission_rejected'])
            self.assertEqual(vla.posts, [])

    def test_capabilities_only_404_falls_back_and_headers_are_ephemeral(self):
        client = VlaClient('http://127.0.0.1:1')
        with patch.object(client.http, 'request_json', side_effect=HttpContractError('old', status_code=404)):
            self.assertIsNone(client.capabilities())
        with patch.object(client.http, 'request_json', side_effect=HttpContractError('unreachable')):
            with self.assertRaises(HttpContractError): client.capabilities()
        with patch.object(client.http, 'request_json', return_value=({}, 202, {})) as post:
            client.finish('task', 'cmd', evidence(), 'Bearer test-only')
            self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer test-only'})
            self.assertNotIn('Authorization', post.call_args.args[2])
            self.assertNotIn('test-only', repr(client.__dict__))

    def test_native_wait_returns_intermediate_state_without_ending_legacy_wait(self):
        client = VlaClient('http://127.0.0.1:1')
        with patch.object(client, 'task', return_value={'state': 'waiting_result_confirmation'}):
            self.assertEqual(client.wait_task('cmd', timeout_sec=1,
                stop_states={'waiting_result_confirmation'})['state'], 'waiting_result_confirmation')
        with patch.object(client, 'task', side_effect=[{'state':'waiting_result_confirmation'}, {'state':'succeeded'}]):
            self.assertEqual(client.wait_task('cmd', timeout_sec=1, poll_interval_sec=0)['state'], 'succeeded')


class NativeEntryTests(unittest.TestCase):
    def test_http_requires_original_identity_and_forwards_credential_only_for_call(self):
        from brain.api.app import create_app
        application = Mock()
        application.vla_effect.return_value = {'accepted': True}
        client = create_app(application, facade=Mock()).test_client()
        identity = {'task_id':'task', 'command_id':'command'}
        for body in ({'task_id':'task'}, dict(identity, operation='open_fridge'), dict(identity, command_id='bad/id')):
            self.assertEqual(client.post('/api/task_vla/pause', json=body).status_code, 400)
        application.vla_effect.assert_not_called()
        observed = evidence()
        response = client.post('/api/task_vla/finish', json=dict(identity, evidence=observed),
                               headers={'Authorization':'Bearer test-only'})
        self.assertEqual(response.status_code, 200)
        application.vla_effect.assert_called_once_with('task', 'command', 'finish',
                                                      evidence=observed, authorization='Bearer test-only')
        self.assertNotIn('test-only', response.get_data(as_text=True))

    def test_service_discovers_pins_and_runs_new_planning_path(self):
        from brain.app import create_service
        with tempfile.TemporaryDirectory() as root:
            config = settings()
            config['reception_real'].update(kernel_runtime_dir=root, kernel_enabled=True)
            vla = FakeVla()
            port = BodyAdapter(FakeDream(), vla)
            output = json.dumps(dict(supported=True, unsupported_requirements=[], actions=[{'skill':'open_fridge'}]))
            service = create_service(config, model=lambda _: output, port_factory=lambda _: port)
            service.reasoner = None
            with patch('brain.service.select_backend', return_value='body_real'), patch(
                    'brain.service.target_identity', return_value={'backend':'body_real'}):
                service.publish('打开冰箱', 'native-service', options={'reflect':False})
                self.assertEqual(service.status()['state'], 'waiting_human', service.status())
                self.assertEqual(service.runtime.record['selection']['vla_capabilities'], capabilities())
                command = service.runtime.record['open_command_id']
                service.vla_effect('native-service', command, 'pause')
                service.vla_effect('native-service', command, 'finish', evidence=evidence(), authorization='Bearer test-only')
                service.control('continue', task_id='native-service')
                self.assertEqual(service.status()['state'], 'succeeded', service.status())
                self.assertEqual(len(vla.posts), 1)


if __name__ == '__main__':
    unittest.main()
