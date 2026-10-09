"""Pause/requery uses the original command and preserves verification boundaries."""
import tempfile
import threading
import unittest
from copy import deepcopy
from unittest.mock import Mock

from tests.brain.test_kernel_reception import FakeBody, _runtime


class OfflineCancelTests(unittest.TestCase):
    def test_service_cancel_never_constructs_executor_and_allows_new_task(self):
        from brain.app import create_service
        with tempfile.TemporaryDirectory() as root:
            runtime, port = self.failed_runtime(root)
            with runtime._exclusive():
                runtime._reload_locked()
                runtime.record.update(command_unknown=True, stopped_confirmed=False, resources_cleared=False)
                runtime._save('test_transport_unknown')
            factory = Mock(side_effect=AssertionError('executor must not be constructed'))
            service = create_service({'reception_real': {'kernel_runtime_dir': root}}, model=Mock(), port_factory=factory)
            task_id = runtime.record['task_id']
            result = service.control('cancel', task_id=task_id)
            self.assertTrue(result['completed'])
            factory.assert_not_called()
            current = service.runtime.snapshot()
            self.assertFalse(current['stopped_confirmed'])
            self.assertTrue(current['task_cancellation']['remote_command_unknown'])
            self.assertFalse(service.status()['blocks_new_motion'])
            self.assertTrue(service.control('cancel', task_id=task_id)['no_op'])
            runtime.open_task('new-round', task_desc='new round')
            self.assertEqual(runtime.state, 'running')
            port.cancel.assert_not_called()
            port.query.assert_not_called()

    def failed_runtime(self, root):
        port = FakeBody()
        port.outcomes['NAVIGATING_TO_TABLE2'] = {
            'terminal': 'failed', 'stopped': True, 'resources_released': True,
            'evidence': {'identity': 'nav-table2-abcdef123456', 'identity_ok': True, 'time_ok': True}}
        runtime = _runtime(root, port)
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        port.cancel = Mock(side_effect=ConnectionError('navigation offline'))
        port.query = Mock(side_effect=ConnectionError('navigation offline'))
        return runtime, port

    def test_cancel_after_restart_uses_persisted_stop_without_recontacting_executor(self):
        from brain.kernel.runtime import TaskRuntime
        with tempfile.TemporaryDirectory() as root:
            runtime, port = self.failed_runtime(root)
            attempt = deepcopy(runtime.record['steps']['NAVIGATING_TO_TABLE2']['attempts'][-1])
            runtime = TaskRuntime(runtime.store, port, config=runtime.config,
                                  policy=runtime.policy, release_reader=runtime.release_reader)
            result = runtime.request_cancel()
            self.assertTrue(result['accepted'])
            self.assertTrue(result['completed'])
            self.assertEqual(runtime.state, 'cancelled')
            self.assertFalse(runtime.public_status()['blocks_new_motion'])
            self.assertIsNone(runtime.record['open_command_id'])
            self.assertEqual(runtime.record['steps']['NAVIGATING_TO_TABLE2']['attempts'][-1], attempt)
            self.assertEqual(attempt['verdict'], 'FAIL')
            self.assertEqual(len(port.submits), 1)
            port.cancel.assert_not_called()
            port.query.assert_not_called()

    def test_task_cancel_does_not_require_remote_receipts(self):
        mutations = [
            (('finished',), False), (('submitted',), False), (('action_ended',), False),
            (('publisher_stopped',), False), (('outcome',), 'pending'),
            (('progress', 'terminal'), ''), (('progress', 'timed_out'), True),
            (('progress', 'publisher_stopped'), False), (('progress', 'action_ended'), False),
            (('progress', 'task_id'), 'another-task'), (('progress', 'step_id'), 'VLA_PICKING'),
            (('progress', 'attempt_id'), 'another-attempt'), (('progress', 'command_id'), 'another-command'),
            (('progress', 'evidence', 'identity'), 'another-command'),
            (('progress', 'evidence', 'identity_ok'), False),
            (('progress', 'evidence', 'time_ok'), False),
            (('progress', 'evidence', 'resources_released'), False),
        ]
        for path, value in mutations:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as root:
                runtime, port = self.failed_runtime(root)
                with runtime._exclusive():
                    runtime._reload_locked()
                    target = runtime.record['steps']['NAVIGATING_TO_TABLE2']['attempts'][-1]
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    runtime._save('test_incomplete_receipt')
                result = runtime.request_cancel()
                self.assertTrue(result['accepted'])
                self.assertEqual(runtime.state, 'cancelled')
                self.assertFalse(runtime.public_status()['blocks_new_motion'])
                port.cancel.assert_not_called()
                port.query.assert_not_called()

    def test_task_cancel_archives_inflight_command_without_claiming_it_stopped(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, port = self.failed_runtime(root)
            with runtime._exclusive():
                runtime._reload_locked()
                original = runtime.record['open_command_id']
                runtime.record['open_command_id'] = original + '-r1'
                runtime._save('test_new_command')
            self.assertTrue(runtime.request_cancel()['accepted'])
            self.assertEqual(runtime.state, 'cancelled')
            self.assertEqual(runtime.record['task_cancellation']['abandoned_command_id'], original + '-r1')
            port.cancel.assert_not_called()
            port.query.assert_not_called()



class PausingBody(FakeBody):
    def __init__(self, completed=False, at_verification=False):
        super().__init__()
        self.completed, self.at_verification = completed, at_verification
        self.entered, self.release = threading.Event(), threading.Event()
        self.first = None

    def submit(self, command_id, request):
        result = super().submit(command_id, request)
        if self.first is None:
            self.first = command_id
        self.commands[command_id].update(stopped=True, resources_released=True)
        return result

    def wait(self, command_id, request):
        if command_id == self.first and not self.at_verification:
            self.entered.set()
            self.release.wait(5)
        return dict(self.commands[command_id])

    def cancel(self, command_id, request):
        super().cancel(command_id, request)
        if not self.completed:
            self.commands[command_id].update(terminal='cancelled', evidence={})
        return {'accepted': True}

    def handoff(self, step_id, kind):
        if self.at_verification and kind == 'safe_idle' and not self.release.is_set():
            self.entered.set()
            self.release.wait(5)
        return super().handoff(step_id, kind)


class TaskControlTests(unittest.TestCase):
    def test_resume_rechecks_failed_handoff_without_repeating_completed_navigation(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.handoff_confirmed['to_vla'] = False
            runtime = _runtime(root, port)
            runtime.drive()
            self.assertEqual(runtime.state, 'recovery_required')
            self.assertEqual(runtime.record['blocked_reason'], 'handoff_unconfirmed')
            self.assertIsNone(runtime.record['open_command_id'])
            original = list(port.submits)
            self.assertEqual(len(original), 1)
            runtime.resume()
            runtime.drive()
            self.assertEqual(runtime.state, 'recovery_required')
            self.assertEqual(port.submits, original)
            port.handoff_confirmed['to_vla'] = True
            runtime.resume()
            runtime.drive()
            self.assertEqual(runtime.state, 'succeeded')
            self.assertEqual(len(port.submits), 6)
            self.assertEqual(len(set(port.submits)), 6)

    def exercise(self, *, completed=False, at_verification=False):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        port = PausingBody(completed=completed, at_verification=at_verification)
        runtime = _runtime(root.name, port)
        errors = []
        def drive():
            try:
                runtime.drive()
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=drive)
        thread.start()
        try:
            self.assertTrue(port.entered.wait(10))
            if at_verification:
                self.assertEqual(runtime.state, 'verifying')
            self.assertEqual(runtime.request_pause(), 'paused')
        finally:
            port.release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(runtime.state, 'paused')
        from brain.service_support import control_task
        answer, _ = control_task(runtime.config, "continue", runtime=runtime)
        self.assertTrue(answer["accepted"], answer)
        runtime.drive()
        return runtime, port

    def test_cancelled_action_continue_dispatches_new_attempt_of_same_step(self):
        runtime, port = self.exercise()
        self.assertEqual(runtime.state, 'succeeded')
        self.assertEqual(port.submits.count(port.first), 1)
        self.assertEqual(port.submits.count(port.first + '-r1'), 1)
        self.assertEqual(len(port.submits), 7)
        attempts = runtime.record['steps']['NAVIGATING_TO_TABLE2']['attempts']
        self.assertEqual(attempts[0]['verdict'], 'FAIL')
        self.assertEqual(attempts[0]['outcome'], 'cancelled')
        self.assertEqual(attempts[1]['verdict'], 'PASS')
        self.assertEqual(attempts[1]['authorized_by'], 'human_continue')
        self.assertEqual(runtime.public_status()['completed'], 11)

    def test_completed_action_resume_advances_without_resubmission(self):
        runtime, port = self.exercise(completed=True)
        self.assertEqual(runtime.state, 'succeeded')
        self.assertEqual(len(port.submits), 6)
        self.assertEqual(len(set(port.submits)), 6)
        self.assertEqual(runtime.public_status()['completed'], 11)

    def test_pause_verification_resumes_verification_without_repeating_action(self):
        runtime, port = self.exercise(completed=True, at_verification=True)
        self.assertEqual(runtime.state, 'succeeded')
        self.assertEqual(len(port.submits), 6)
        self.assertEqual(len(set(port.submits)), 6)
        self.assertEqual(runtime.public_status()['completed'], 11)

class SkipControlTests(unittest.TestCase):
    def test_skip_running_stops_current_once_and_advances_once(self):
        with tempfile.TemporaryDirectory() as root:
            port = PausingBody()
            runtime = _runtime(root, port)
            errors = []
            def drive():
                try:
                    runtime.drive()
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=drive)
            thread.start()
            try:
                self.assertTrue(port.entered.wait(2))
                old = runtime.record['open_command_id']
                result = runtime.skip_current(step_id='NAVIGATING_TO_TABLE2')
                self.assertEqual(result['next_step_id'], 'VLA_PICKING')
                self.assertEqual(port.cancels, [old])
                self.assertEqual(runtime.record['steps']['NAVIGATING_TO_TABLE2']['status'], 'manual_skipped')
            finally:
                port.release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(port.submits, [old])

    def failed_runtime(self, step='LATERAL_TO_RELAY3', **outcome):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        port = FakeBody()
        port.outcomes[step] = dict(terminal='failed', stopped=True, resources_released=True, **outcome)
        runtime = _runtime(root.name, port)
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        return runtime, port

    def test_skip_advances_once_preserves_failure_and_finishes_without_false_success(self):
        from contracts.tasks import Rejected
        from brain.learning.worker import episode_from_record
        runtime, port = self.failed_runtime()
        old_command = runtime.record['open_command_id']
        status = runtime.public_status()
        self.assertTrue(status['can_skip'])
        self.assertEqual(status['skip_step_id'], 'LATERAL_TO_RELAY3')
        result = runtime.skip_current(step_id=status['skip_step_id'])
        self.assertEqual(result['next_step_id'], 'NAVIGATING_TO_TABLE1')
        self.assertEqual(runtime._latest('LATERAL_TO_RELAY3')['verdict'], 'FAIL')
        self.assertEqual(port.submits.count(old_command), 1)
        with self.assertRaises(Rejected):
            runtime.skip_current(step_id='LATERAL_TO_RELAY3')
        runtime.drive()
        self.assertEqual(runtime.state, 'failed')
        status = runtime.public_status()
        self.assertTrue(status['flow_finished'])
        self.assertFalse(status['all_done'])
        skipped = next(t for t in status['subtask_list'] if t['step_id'] == 'LATERAL_TO_RELAY3')
        self.assertEqual(skipped['status'], 'skipped')
        self.assertFalse(skipped['effect_verified'])
        self.assertEqual(len(port.submits), 6)
        self.assertTrue(all(n == 1 for n in runtime.record['dispatch_counts'].values()))
        episode = episode_from_record(runtime.snapshot())
        self.assertEqual(episode['final'], 'failure')
        self.assertEqual(episode['manual_skips'][0]['command_id'], old_command)

    def test_skipped_navigation_still_dispatches_pick_with_the_real_command_as_proof(self):
        runtime, port = self.failed_runtime('NAVIGATING_TO_TABLE2')
        skipped = runtime.record['open_command_id']
        port.handoff_confirmed['to_vla'] = False  # bridged by the skip, never asked
        runtime.skip_current(step_id='NAVIGATING_TO_TABLE2')
        self.assertEqual(runtime.record['phase'], 'VLA_PICKING')
        runtime.drive()
        pick = runtime.record['steps']['VLA_PICKING']['attempts'][0]['request']['body']
        # The proof names the real DREAM command; VLA checks it against DREAM itself.
        self.assertEqual(pick['navigation_proof']['dream_command_id'], skipped)
        self.assertEqual(len(port.submits), 5)
        # Only the handoff across the skip is filled in; the later place handoff is still checked.
        self.assertEqual(runtime.state, 'recovery_required')
        self.assertEqual(runtime.record['phase'], 'VLA_PLACING')
        self.assertEqual(runtime.record['blocked_reason'], 'handoff_unconfirmed')
        self.assertTrue(runtime.public_status()['has_failures'])

    def test_skipped_failed_pick_bridges_next_handoff_and_dispatches_navigation(self):
        runtime, port = self.failed_runtime('VLA_PICKING')
        port.handoff_confirmed['to_nav'] = False  # bridged by the skip, never asked
        runtime.skip_current(step_id='VLA_PICKING')
        runtime.drive()
        self.assertIn('NAVIGATING_TO_RELAY2', runtime.record['steps'])
        self.assertTrue(runtime.record['steps']['NAVIGATING_TO_RELAY2']['attempts'][0]['submitted'])
        self.assertIsNone(runtime.record['holding'])
        self.assertEqual(runtime.state, 'failed')
        self.assertEqual([item['step_id'] for item in runtime.record['manual_skips']], ['VLA_PICKING'])

    def test_unskipped_handoff_is_still_checked(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        from tests.brain.test_kernel_reception import FakeBody, _runtime
        port = FakeBody()
        port.handoff_confirmed['to_vla'] = False
        runtime = _runtime(root.name, port)
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        self.assertEqual(runtime.record['blocked_reason'], 'handoff_unconfirmed')
        self.assertEqual(len(port.submits), 1)

    def test_unreachable_gate_waits_for_human_and_continue_rechecks_it(self):
        class FlakyGate(FakeBody):
            reachable = False
            @property
            def gate_open(self):
                if not self.reachable:
                    raise RuntimeError('服务不可达 http://dream:8001/v1/status: [Errno 111] Connection refused')
                return True
            @gate_open.setter
            def gate_open(self, value):
                pass
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        port = FlakyGate()
        runtime = _runtime(root.name, port)
        runtime.drive()
        self.assertEqual(runtime.state, 'waiting_human')
        self.assertIn('Connection refused', runtime.record['blocked_reason'])
        self.assertEqual(port.submits, [])
        status = runtime.public_status()
        self.assertTrue(status['can_resume'])
        self.assertTrue(status['can_skip'])  # operator may abandon this unstarted leg
        runtime.continue_current()
        runtime.drive()
        self.assertEqual(runtime.state, 'waiting_human')  # still unreachable: re-check, no submit
        self.assertEqual(port.submits, [])
        port.reachable = True
        runtime.continue_current()
        runtime.drive()
        self.assertEqual(port.submits[0].rsplit('-', 1)[0], 'nav-table2')
        self.assertEqual(runtime.record['dispatch_counts']['NAVIGATING_TO_TABLE2'], 1)

    def test_skip_requires_stop_release_and_same_command(self):
        from contracts.tasks import Rejected
        for change in ({'stopped': False}, {'resources_released': False}, {'command_id': 'other'},
                       {'terminal': '', 'started': True}):
            with self.subTest(change=change):
                runtime, port = self.failed_runtime()
                original = runtime.snapshot()
                command = runtime.record['open_command_id']
                port.commands[command].update(change)
                with self.assertRaises(Rejected):
                    runtime.skip_current(step_id='LATERAL_TO_RELAY3')
                self.assertEqual(original, runtime.snapshot())

    def test_skip_rejects_stale_step_and_state_change_during_query(self):
        from contracts.tasks import Rejected
        runtime, port = self.failed_runtime()
        with self.assertRaises(Rejected):
            runtime.skip_current(step_id='NAVIGATING_TO_TABLE2')
        original_query = port.query
        def changed(command, request):
            answer = original_query(command, request)
            runtime.request_cancel()
            return answer
        port.query = changed
        with self.assertRaises(Rejected):
            runtime.skip_current(step_id='LATERAL_TO_RELAY3')
        self.assertFalse(runtime.record.get('manual_skips'))
        self.assertEqual(runtime.record['control_request'], 'cancel')

    def test_skip_paused_incomplete_step_and_reject_already_completed_action(self):
        from contracts.tasks import Rejected
        runtime, port = self.failed_runtime('NAVIGATING_TO_TABLE2')
        runtime.record['state'] = 'paused'
        runtime._save('test_paused')
        command = runtime.record['open_command_id']
        self.assertTrue(runtime.public_status()['can_skip'])
        port.commands[command]['terminal'] = 'succeeded'
        with self.assertRaises(Rejected):
            runtime.skip_current(step_id='NAVIGATING_TO_TABLE2')
        port.commands[command]['terminal'] = 'cancelled'
        result = runtime.skip_current(step_id='NAVIGATING_TO_TABLE2')
        self.assertEqual(result['next_step_id'], 'VLA_PICKING')
        self.assertEqual(runtime.state, 'running')

    def test_skip_last_action_does_not_mark_its_verification_passed(self):
        runtime, port = self.failed_runtime('VLA_PLACING')
        self.assertEqual(runtime.skip_current(step_id='VLA_PLACING')['next_step_id'], None)
        self.assertEqual(runtime.state, 'failed')
        status = runtime.public_status()
        self.assertTrue(status['flow_finished'])
        self.assertFalse(status['all_done'])
        for t in status['subtask_list'][-2:]:
            self.assertEqual(t['status'], 'skipped')
            self.assertFalse(t['effect_verified'])
        self.assertEqual(runtime.record['holding'], 'cola_can_1')

    def test_skip_rejects_terminal_and_passed_step(self):
        from contracts.tasks import Rejected
        runtime, port = self.failed_runtime()
        for state in ('succeeded', 'cancelled', 'failed'):
            runtime.record['state'] = state
            runtime._save('test_state')
            self.assertFalse(runtime.public_status()['can_skip'])
            with self.assertRaises(Rejected):
                runtime.skip_current(step_id='LATERAL_TO_RELAY3')
        runtime.record['state'] = 'recovery_required'
        runtime._latest('LATERAL_TO_RELAY3')['verdict'] = 'PASS'
        runtime._save('test_pass')
        self.assertFalse(runtime.public_status()['can_skip'])
        with self.assertRaises(Rejected):
            runtime.skip_current(step_id='LATERAL_TO_RELAY3')


class ContinueAttemptTests(unittest.TestCase):
    failed_runtime = SkipControlTests.failed_runtime
    # Keep these tests independent of the Web and exercise real Runtime admission.
    def test_continue_retry_rechecks_handoff_and_reuses_prepared_attempt(self):
        runtime, port = self.failed_runtime('VLA_PICKING')
        original = runtime.record['open_command_id']
        port.handoff_confirmed['to_vla'] = False
        reply = runtime.continue_current()
        self.assertTrue(reply['retry_prepared'])
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        self.assertEqual(runtime.record['blocked_reason'], 'handoff_unconfirmed')
        self.assertEqual(len(port.submits), 2)
        runtime.continue_current()
        runtime.drive()
        self.assertEqual(len(runtime.record['steps']['VLA_PICKING']['attempts']), 2)
        port.handoff_confirmed['to_vla'] = True
        port.outcomes.pop('VLA_PICKING')
        runtime.continue_current()
        runtime.drive()
        self.assertEqual(runtime.state, 'succeeded')
        self.assertEqual(port.submits.count(original + '-r1'), 1)

    def test_continue_requires_original_stop_resource_and_terminal_evidence(self):
        from contracts.tasks import Rejected
        for change in ({'stopped': False}, {'resources_released': False}, {'command_id': 'other'},
                       {'terminal': '', 'started': True}, {'timed_out': True}):
            with self.subTest(change=change):
                runtime, port = self.failed_runtime('VLA_PICKING')
                original = runtime.snapshot()
                port.commands[runtime.record['open_command_id']].update(change)
                with self.assertRaises(Rejected):
                    runtime.continue_current()
                self.assertEqual(runtime.snapshot(), original)

    def test_continue_never_repeats_reported_success_with_weak_evidence(self):
        runtime, port = self.failed_runtime('VLA_PICKING')
        command = runtime.record['open_command_id']
        port.commands[command].update(terminal='succeeded', evidence={})
        reply = runtime.continue_current()
        self.assertTrue(reply['verifying_original'])
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        self.assertEqual(len(port.submits), 2)
        self.assertEqual(len(runtime.record['steps']['VLA_PICKING']['attempts']), 1)

    def test_continue_ignores_late_query_after_cancel(self):
        from contracts.tasks import Rejected
        runtime, port = self.failed_runtime('VLA_PICKING')
        query = port.query
        def cancelled(command, request):
            result = query(command, request)
            runtime.request_cancel()
            return result
        port.query = cancelled
        with self.assertRaises(Rejected):
            runtime.continue_current()
        self.assertEqual(runtime.state, 'cancelled')
        self.assertEqual(len(runtime.record['steps']['VLA_PICKING']['attempts']), 1)
