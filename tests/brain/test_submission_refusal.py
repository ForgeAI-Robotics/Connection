"""Submission refusal must not create a phantom running command or imply success."""
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import Mock

from brain.adapters.execution import BodyAdapter
from brain.adapters.http_client import HttpContractError
from brain.kernel.runtime import status_from_record
from tests.brain.test_kernel_reception import _runtime
from tests.factories import TaskRuntime


REFUSAL = {"success": False, "error": {"code": "INVALID_LEG_ORDER",
    "message": "relay2/door_approach must succeed before relay3 lateral crossing",
    "retryable": False, "details": {}}}


def adapter(error):
    dream = Mock()
    dream.status.return_value = {"navigation_transport_ready": True}
    dream.submit_navigation.side_effect = error
    return BodyAdapter(dream, Mock())


class SubmissionRefusalTests(unittest.TestCase):
    def test_admission_refusals_cancel_locally_and_allow_a_new_task(self):
        cases = [(code, {}) for code in (
            'MOTOR_HEALTH_NOT_READY', 'SAFETY_NOT_READY', 'VLA_ACTION_PORT_OWNED',
            'NAVIGATION_STILL_ACTIVE', 'NAVIGATION_STATUS_UNVERIFIABLE')]
        cases += [(code, {'active_command_id': 'another-command'})
                  for code in ('NAVIGATION_BUSY', 'VLA_BUSY')]
        for code, details in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                raw = {'success': False, 'error': {'code': code, 'message': '未受理', 'details': details}}
                port = adapter(HttpContractError('HTTP 409', status_code=409, payload=raw))
                runtime = _runtime(root, port)
                runtime.drive()
                self.assertEqual(runtime.public_status()['failure_detail']['kind'], 'rejected')
                runtime.request_cancel()
                runtime.settle_cancel()
                self.assertFalse(runtime.public_status()['blocks_new_motion'])
                self.assertTrue(runtime.request_cancel()['completed'])
                runtime.open_task('task-new-after-cancel', task_desc='开始接待')
                self.assertEqual(runtime.state, 'running')
                port.dream.command.assert_not_called()
                port.dream.cancel.assert_not_called()
                self.assertEqual(port.dream.submit_navigation.call_count, 1)

    def test_cancel_repairs_existing_motor_refusal_without_restarting_runtime(self):
        raw = {'success': False, 'error': {'code': 'MOTOR_HEALTH_NOT_READY',
               'message': '降温确认中，禁止新任务', 'retryable': True,
               'details': {'motor_health': {'new_task_allowed': False, 'state': 'cooldown'}}}}
        with tempfile.TemporaryDirectory() as root:
            runtime, port = self.legacy(root, payload=raw)
            with runtime._exclusive():
                runtime._reload_locked()
                runtime.record.update(control_request='cancel', dispatch_closed=True,
                                      blocked_reason='cancel_not_accepted')
                runtime._save('test_failed_cancel')
            self.assertTrue(runtime.request_cancel()['completed'])
            self.assertEqual(runtime.state, 'cancelled')
            self.assertFalse(runtime.public_status()['blocks_new_motion'])
            port.dream.command.assert_not_called()
            port.dream.cancel.assert_not_called()

    def test_explicit_refusal_is_finished_without_wait_query_or_cancel_even_after_restart(self):
        port = adapter(HttpContractError("HTTP 409", status_code=409, payload=REFUSAL))
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root, port)
            runtime.drive()
            self.assertEqual(runtime.state, "recovery_required")
            self.assertIsNone(runtime.record["open_command_id"])
            self.assertFalse(runtime.record["command_unknown"])
            self.assertTrue(runtime.record["resources_cleared"])
            self.assertEqual(runtime.record["blocked_reason"], "submission_rejected:INVALID_LEG_ORDER")
            attempt = deepcopy(runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][-1])
            self.assertEqual(attempt["outcome"], "rejected")
            self.assertEqual(attempt["verdict"], "FAIL")
            self.assertFalse(attempt["progress"]["started"])
            self.assertFalse(attempt["progress"]["timed_out"])
            self.assertEqual(attempt["progress"]["evidence"]["downstream"], REFUSAL)
            public = runtime.public_status()
            self.assertEqual(public["failure_detail"]["code"], "INVALID_LEG_ORDER")
            self.assertTrue(public["failure_detail"]["not_started"])
            self.assertEqual(public["completed"], 2)
            runtime = TaskRuntime(runtime.store, port, config=runtime.config)
            runtime.request_cancel()
            runtime.settle_cancel()
            self.assertEqual(runtime.state, "cancelled")
            self.assertFalse(runtime.public_status()["blocks_new_motion"])
            self.assertEqual(runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][-1], attempt)
            port.dream.wait_command.assert_not_called()
            port.dream.command.assert_not_called()
            port.dream.cancel.assert_not_called()
            self.assertEqual(port.dream.submit_navigation.call_count, 1)

    def test_only_explicit_continue_creates_one_new_attempt(self):
        port = adapter(HttpContractError("HTTP 409", status_code=409, payload=REFUSAL))
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root, port)
            runtime.drive()
            runtime.drive()
            self.assertEqual(port.dream.submit_navigation.call_count, 1)
            result = runtime.continue_current()
            self.assertTrue(result["retry_prepared"])
            self.assertNotEqual(result["command_id"], result["previous_command_id"])
            self.assertEqual(port.dream.submit_navigation.call_count, 1)
            runtime.drive()
            self.assertEqual(port.dream.submit_navigation.call_count, 2)
            port.dream.command.assert_not_called()

    def test_network_errors_404_conflicts_busy_and_5xx_remain_unknown(self):
        cases = [(None, None), (404, REFUSAL), (500, REFUSAL), (409, "bad response"),
                 (409, {"success": False, "error": {"code": "COMMAND_ID_CONFLICT"}}),
                 (409, {"success": False, "error": {"code": "NAVIGATION_BUSY"}}),
                 (409, {"success": False, "error": {"code": "NAVIGATION_BUSY", "details": {
                     "active_command_id": "nav-table2-abcdef123456"}}}),
                 (409, dict(REFUSAL, command_id="another-command")),
                 (409, dict(REFUSAL, accepted=True)), (409, dict(REFUSAL, success=True))]
        for status, payload in cases:
            with self.subTest(status=status, payload=payload), tempfile.TemporaryDirectory() as root:
                port = adapter(HttpContractError("ambiguous", status_code=status, payload=payload))
                runtime = _runtime(root, port)
                runtime.drive()
                self.assertEqual(runtime.state, "recovery_required")
                self.assertTrue(runtime.record["open_command_id"])
                self.assertTrue(runtime.record["command_unknown"])
                self.assertFalse(runtime.record["resources_cleared"])
                self.assertTrue(runtime.public_status()["blocks_new_motion"])
                self.assertNotEqual(runtime.record["blocked_reason"], "timeout")

    def legacy(self, root, payload=REFUSAL, error=None):
        # Reproduce the old runner's exact UNKNOWN receipt, with the HTTP response
        # already saved; the runtime must not query 404 and infer a stop from that.
        port = adapter(HttpContractError(error or "HTTP 409 http://nav:8001/v1/navigation/goals: refused",
                                         payload=payload))
        runtime = _runtime(root, port)
        runtime.drive()
        with runtime._exclusive():
            runtime._reload_locked()
            attempt = runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][-1]
            attempt["progress"]["timed_out"] = True
            attempt["outcome"] = "timeout"
            runtime.record["pending_progress"] = deepcopy(attempt["progress"])
            runtime.record["blocked_reason"] = "timeout"
            runtime._save("test_old_runner_timeout")
        return runtime, port

    def test_legacy_refusal_reconciliation_is_audited_idempotent_and_offline(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, port = self.legacy(root)
            old = deepcopy(runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][-1]["progress"])
            self.assertTrue(runtime.reconcile_submission_rejection())
            self.assertFalse(runtime.reconcile_submission_rejection())
            self.assertIsNone(runtime.record["open_command_id"])
            self.assertEqual(runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][-1]["original_unknown_progress"], old)
            runtime.request_cancel()
            runtime.settle_cancel()
            self.assertEqual(runtime.state, "cancelled")
            port.dream.command.assert_not_called()
            port.dream.cancel.assert_not_called()

    def test_legacy_repair_rejects_foreign_identity_and_missing_refusal_proof(self):
        for mutation in ("identity", "network", "conflict", "started", "body"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                runtime, _ = self.legacy(root)
                with runtime._exclusive():
                    runtime._reload_locked()
                    attempt = runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][-1]
                    if mutation == "identity":
                        attempt["progress"]["command_id"] = "old-command"
                    elif mutation == "network":
                        attempt["progress"]["error"] = "Connection lost"
                    elif mutation == "started":
                        attempt["progress"]["started"] = True
                    elif mutation == "body":
                        attempt["progress"]["evidence"]["downstream"]["task_id"] = "another-task"
                    else:
                        attempt["progress"]["evidence"]["downstream"]["error"]["code"] = "COMMAND_ID_CONFLICT"
                    runtime._save("test_legacy_proof")
                self.assertFalse(runtime.reconcile_submission_rejection())
                self.assertTrue(runtime.record["command_unknown"])

    def test_legacy_repair_completes_a_previously_requested_cancel(self):
        with tempfile.TemporaryDirectory() as root:
            runtime, port = self.legacy(root)
            with runtime._exclusive():
                runtime._reload_locked()
                runtime.record.update(control_request="cancel", dispatch_closed=True)
                runtime._save("test_pending_cancel")
            self.assertTrue(runtime.reconcile_submission_rejection())
            self.assertEqual(runtime.state, "cancelled")
            self.assertFalse(runtime.public_status()["blocks_new_motion"])
            port.dream.cancel.assert_not_called()
            port.dream.command.assert_not_called()

    def test_failure_status_exposes_navigation_cause(self):
        record = {"state": "recovery_required", "cursor": 0,
                  "phase_specs": [{"step_id": "NAVIGATING_TO_RELAY2"}],
                  "steps": {"NAVIGATING_TO_RELAY2": {"attempts": [{"verdict": "FAIL",
                    "progress": {"terminal": "failed", "evidence": {"downstream": {"error": {
                        "code": "NAVIGATION_TERMINAL_UNREACHABLE", "message": "final_yaw_reversal_limit"}}}}}]}}}
        detail = status_from_record(record)["failure_detail"]
        self.assertEqual(detail["message"], "final_yaw_reversal_limit")
        self.assertFalse(detail["not_started"])


if __name__ == "__main__":
    unittest.main()
