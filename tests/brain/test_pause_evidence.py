"""Pause records original evidence without advancing or replaying the task."""
import tempfile
import threading
import unittest
from copy import deepcopy
from unittest.mock import Mock

from brain.reasoning import recovery_context
from tests.brain.test_kernel_reception import _runtime
from tests.brain.test_task_controls import PausingBody
from tests.factories import TaskRuntime


class PauseEvidenceTests(unittest.TestCase):
    def running(self, *, completed=False):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        port = PausingBody(completed=completed)
        runtime = _runtime(root.name, port)
        errors = []
        def drive():
            try:
                runtime.drive()
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=drive)
        thread.start()
        def finish():
            port.release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
        self.addCleanup(finish)
        self.assertTrue(port.entered.wait(5))
        return runtime, port, thread

    def test_cancel_receipt_and_flags_survive_restart_and_feed_model(self):
        runtime, port, thread = self.running()
        command = runtime.record['open_command_id']
        query = port.query
        def observed(*args):
            result = query(*args)
            result['raw'] = {'command_id': command, 'state': 'cancelled',
                             'result': {'navigation_stopped': True}}
            return result
        port.query = observed
        self.assertEqual(runtime.request_pause(), 'paused')
        port.release.set(); thread.join(5)
        restored = TaskRuntime(runtime.store, port, config=runtime.config)
        record = restored.snapshot()
        self.assertFalse(record['command_unknown'])
        self.assertTrue(record['stopped_confirmed'])
        self.assertTrue(record['resources_cleared'])
        self.assertTrue(record['dispatch_closed'])
        self.assertEqual(record['open_command_id'], command)
        progress = record['steps']['NAVIGATING_TO_TABLE2']['attempts'][0]['progress']
        self.assertEqual(progress['terminal'], 'cancelled')
        self.assertEqual(progress['evidence']['downstream']['command_id'], command)
        self.assertEqual(record['pending_progress']['command_id'], command)
        self.assertEqual(restored.public_status()['current_command']['stage'], 'cancelled')
        self.assertEqual(restored.public_status()['completed'], 2)
        context = recovery_context(record)
        self.assertFalse(context['command_unknown'])
        self.assertTrue(context['stopped_confirmed'])
        self.assertTrue(context['resources_cleared'])
        self.assertEqual(port.submits, [command])

    def test_success_during_pause_waits_for_verification(self):
        runtime, port, thread = self.running(completed=True)
        runtime.request_pause()
        port.release.set(); thread.join(5)
        self.assertFalse(runtime.record['command_unknown'])
        attempt = runtime.record['steps']['NAVIGATING_TO_TABLE2']['attempts'][0]
        self.assertEqual(attempt['progress']['terminal'], 'succeeded')
        self.assertNotEqual(attempt['verdict'], 'PASS')
        self.assertEqual(runtime.public_status()['completed'], 2)
        self.assertEqual(len(port.submits), 1)

    def test_foreign_stale_missing_or_unconfirmed_observation_cannot_confirm_stop(self):
        changes = [({'command_id': 'foreign'}, False), ({'timed_out': True}, False),
                   ({'evidence': {}}, False), ({'evidence': {'identity_ok': False, 'time_ok': True}}, False),
                   ({'evidence': {'identity_ok': True, 'time_ok': False}}, False),
                   ({'stopped': False}, True), ({'terminal': '', 'started': True}, False)]
        for change, known in changes:
            with self.subTest(change=change):
                runtime, port, thread = self.running()
                query = port.query
                port.query = lambda *args: dict(query(*args), **change)
                self.assertEqual(runtime.request_pause(), 'recovery_required')
                self.assertEqual(runtime.record['command_unknown'], not known)
                self.assertFalse(runtime.record['stopped_confirmed'])
                self.assertFalse(runtime.record['resources_cleared'])
                port.release.set(); thread.join(5)

    def test_stop_does_not_imply_resource_release(self):
        runtime, port, _ = self.running()
        query = port.query
        port.query = lambda *args: dict(query(*args), resources_released=False)
        self.assertEqual(runtime.request_pause(), 'paused')
        self.assertFalse(runtime.record['command_unknown'])
        self.assertTrue(runtime.record['stopped_confirmed'])
        self.assertFalse(runtime.record['resources_cleared'])

    def test_cancel_timeout_can_be_resolved_by_original_query(self):
        runtime, port, _ = self.running()
        cancel = port.cancel
        def lost(*args):
            cancel(*args)
            raise TimeoutError('cancel response lost')
        port.cancel = lost
        self.assertEqual(runtime.request_pause(), 'paused')
        self.assertTrue(runtime.record['stopped_confirmed'])
        self.assertEqual(runtime._latest('NAVIGATING_TO_TABLE2')['pause_cancel_error'], 'cancel response lost')

    def test_query_failure_cannot_use_cancel_ack_as_stop_proof(self):
        runtime, port, _ = self.running()
        port.query = Mock(side_effect=TimeoutError('query unavailable'))
        self.assertEqual(runtime.request_pause(), 'recovery_required')
        self.assertTrue(runtime.record['command_unknown'])
        self.assertFalse(runtime.record['stopped_confirmed'])
        self.assertIsNone(runtime.record['pending_progress'])

    def test_late_pause_query_cannot_overwrite_task_cancel(self):
        runtime, port, _ = self.running()
        query = port.query
        cancelled = {}
        def late(*args):
            answer = query(*args)
            runtime.request_cancel()
            cancelled.update(deepcopy(runtime.record))
            return answer
        port.query = late
        self.assertEqual(runtime.request_pause(), 'cancelled')
        self.assertEqual(runtime.snapshot(), cancelled)

    def test_late_pause_query_cannot_update_a_new_command_in_the_same_state(self):
        runtime, port, _ = self.running()
        query = port.query
        changed = {}
        def late(*args):
            answer = query(*args)
            with runtime._exclusive():
                runtime._reload_locked()
                runtime.record['open_command_id'] = 'another-command'
                runtime._save('concurrent_command_change')
                changed.update(deepcopy(runtime.record))
            return answer
        port.query = late
        runtime.request_pause()
        self.assertEqual(runtime.snapshot(), changed)

    def test_pause_replaces_stale_advice_query_with_current_attempt_evidence(self):
        from brain.recovery import recovery_progress
        runtime, _, _ = self.running()
        with runtime._exclusive():
            runtime._reload_locked()
            attempt = runtime._latest('NAVIGATING_TO_TABLE2')
            runtime.record['advice_query'] = dict(command_id=attempt['command_id'],
                attempt_id=attempt['attempt_id'], terminal='succeeded')
            runtime._save('stale_advice_query')
        runtime.request_pause()
        self.assertIsNone(recovery_context(runtime.record)['original_query'])
        self.assertEqual(recovery_progress(runtime.record)['terminal'], 'cancelled')

    def test_legacy_paused_unknown_can_query_without_recancel_or_resume(self):
        runtime, port, thread = self.running()
        runtime.request_pause()
        port.release.set(); thread.join(5)
        with runtime._exclusive():
            runtime._reload_locked()
            runtime.record.update(command_unknown=True, stopped_confirmed=False, resources_cleared=False)
            runtime._save('legacy_pause_flags')
        self.assertIn('query_original', recovery_context(runtime.record)['allowed_actions'])
        cancels, submits = list(port.cancels), list(port.submits)
        self.assertTrue(runtime.query_advice_original()['result_known'])
        self.assertEqual(runtime.state, 'paused')
        self.assertTrue(runtime.record['dispatch_closed'])
        self.assertFalse(runtime.record['command_unknown'])
        self.assertTrue(runtime.record['stopped_confirmed'])
        self.assertTrue(runtime.record['resources_cleared'])
        self.assertEqual(port.cancels, cancels)
        self.assertEqual(port.submits, submits)
