"""Pause/requery uses the original command and preserves verification boundaries."""
import tempfile
import threading
import unittest

from tests.brain.test_kernel_reception import FakeBody, _runtime


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

    def test_skip_does_not_forge_navigation_proof_even_if_handoff_port_says_ready(self):
        runtime, port = self.failed_runtime('NAVIGATING_TO_TABLE2')
        runtime.skip_current(step_id='NAVIGATING_TO_TABLE2')
        self.assertEqual(runtime.record['phase'], 'VLA_PICKING')
        runtime.drive()
        self.assertEqual(runtime.state, 'recovery_required')
        self.assertIn('缺少导航成功凭证', runtime.record['blocked_reason'])
        self.assertEqual(len(port.submits), 1)
        self.assertIsNone(runtime.record['holding'])

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

    def test_skip_rejects_running_waiting_and_passed_step(self):
        from contracts.tasks import Rejected
        runtime, port = self.failed_runtime()
        for state in ('running', 'succeeded', 'waiting_human'):
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
        self.assertEqual(runtime.state, 'cancelling')
        self.assertEqual(len(runtime.record['steps']['VLA_PICKING']['attempts']), 1)
