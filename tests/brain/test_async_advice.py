"""Slow advisory models must never delay authorized execution or own Runtime writes."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from brain.application import BrainApplication
from tests.brain import test_reasoning


class AsyncAdviceTests(unittest.TestCase):
    setUp = test_reasoning.ReasoningIntegrationTests.setUp
    service = test_reasoning.ReasoningIntegrationTests.service

    def wait(self, predicate):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for application progress')

    def start(self):
        self.body.outcomes['NAVIGATING_TO_TABLE2'] = {
            'terminal': 'failed', 'stopped': True, 'resources_released': True,
            'evidence': {'identity_ok': True, 'time_ok': True}}
        self.entered, self.release = threading.Event(), threading.Event()
        self.contexts = []

        def model(messages):
            prompt = messages[0]['content']
            if '任务异常分析器' not in prompt:
                return test_reasoning.route()
            self.contexts.append(json.loads(prompt.split('\n')[-1]))
            if len(self.contexts) == 1:
                self.entered.set()
                self.release.wait(5)
            return json.dumps({'action': 'request_help', 'summary': '离线建议', 'reason': '原命令证据'})

        self.app = BrainApplication(self.service(model)).start()
        def cleanup():
            self.release.set()
            self.app.close(3)
        self.addCleanup(cleanup)
        self.app.publish('开始接待（仅导航）', 'async-advice')
        self.assertTrue(self.entered.wait(2))

    def test_skip_dispatches_next_leg_before_model_returns_and_logs_control(self):
        self.start()
        with patch('shared.brain_journal.emit') as journal:
            result = self.app.control('skip', task_id='async-advice', step_id='NAVIGATING_TO_TABLE2')
            self.assertTrue(result['accepted'])
            self.wait(lambda: len(self.body.submits) > 1)
            logs = [c.kwargs for c in journal.call_args_list if c.args and c.args[0] == 'CONTROL']
            self.assertEqual([r['event'] for r in logs], ['received', 'processing', 'accepted'])
            self.assertEqual(len({r['control_id'] for r in logs}), 1)
        self.assertFalse(self.release.is_set())
        self.wait(lambda: self.app.status().get('terminal'))
        self.release.set()
        self.wait(lambda: self.app._advice_inflight is None)
        self.assertIsNone(self.app.status().get('recovery_advice'))

    def test_continue_runs_and_latest_failure_supersedes_stale_analysis(self):
        self.start()
        for count in (2, 3):
            self.assertTrue(self.app.control('continue', task_id='async-advice')['accepted'])
            self.wait(lambda: len(self.body.submits) == count and self.app.status()['state'] == 'recovery_required')
        self.assertEqual(len(self.contexts), 1)
        self.assertFalse(self.release.is_set())
        self.release.set()
        self.wait(lambda: self.app.status().get('recovery_advice'))
        advice = self.app.status()['recovery_advice']
        self.assertEqual(advice['command_id'], self.body.submits[-1])
        self.assertEqual(len(self.contexts), 2)  # Intermediate failures are coalesced.
        self.assertEqual(len(set(self.body.submits)), 3)

    def test_cancel_and_new_semantic_planning_do_not_wait_for_old_advice(self):
        self.start()
        self.assertTrue(self.app.control('cancel', task_id='async-advice')['accepted'])
        self.body.outcomes.clear()
        self.app.publish('搬一罐可乐到目标桌', 'new-after-cancel')
        self.wait(lambda: self.app.status()['state'] == 'succeeded')
        self.assertFalse(self.release.is_set())
        self.release.set()
        self.wait(lambda: self.app._advice_inflight is None)
        self.assertEqual(self.app.status()['task_id'], 'new-after-cancel')
        self.assertIsNone(self.app.status().get('recovery_advice'))

    def test_shutdown_does_not_apply_late_model_result(self):
        self.start()
        revision = self.app.service.runtime.snapshot()['revision']
        self.app.close(.05)
        self.release.set()
        self.wait(lambda: not self.app.adviser.health()['alive'])
        self.assertEqual(self.app.service.runtime.snapshot()['revision'], revision)
        self.assertIsNone(self.app.status().get('recovery_advice'))
