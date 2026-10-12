"""Human approval is scoped to an immutable suggestion, never a retry policy."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from brain.api.app import create_app
from brain.application import BrainApplication
from contracts.tasks import Rejected
from tests.brain import test_reasoning


class AdviceConfirmationTests(unittest.TestCase):
    setUp = test_reasoning.ReasoningIntegrationTests.setUp
    service = test_reasoning.ReasoningIntegrationTests.service

    def start(self, action='query_original', app=False):
        self.config['brain']['reasoning'] = {'confirm_recovery_advice': True}
        self.body.outcomes['NAVIGATING_TO_TABLE2'] = {'terminal': 'failed', 'stopped': True,
            'resources_released': True, 'evidence': {'identity_ok': True, 'time_ok': True}}
        def model(messages):
            if action == 'adaptive':
                context = json.loads(messages[0]['content'].split('\n')[-1])
                choice = 'continue' if context.get('original_query') and 'continue' in context['allowed_actions'] else 'query_original'
            else:
                choice = action
            return json.dumps({'action': choice, 'summary': '处理当前异常', 'reason': '依据原命令证据'})
        self.svc = self.service(model)
        if app:
            self.app = BrainApplication(self.svc).start()
            self.addCleanup(self.app.close)
            self.app.publish('开始接待（仅导航）', 'advice-task')
            self.wait(lambda: self.app.status().get('recovery_advice'))
        else:
            self.svc.publish('开始接待（仅导航）', 'advice-task')
        return self.svc.status()['recovery_advice']

    def wait(self, predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if predicate(): return
            time.sleep(.01)
        self.fail('Timed out')

    def confirm(self, advice):
        return self.svc.confirm_advice('advice-task', advice['advice_id'], advice['record_revision'])

    def test_query_does_not_retry_or_advance_and_second_approval_continues(self):
        advice = self.start('adaptive')
        original = self.svc.runtime.record['open_command_id']
        self.assertEqual(len(self.body.submits), 1)
        result = self.confirm(advice)
        self.assertTrue(result['query_only'])
        self.assertTrue(result['result_known'])
        self.assertEqual(self.body.submits, [original])
        self.assertEqual(self.svc.status()['state'], 'recovery_required')
        next_advice = self.svc.status()['recovery_advice']
        self.assertEqual(next_advice['action'], 'continue')
        self.body.outcomes.clear()
        result = self.confirm(next_advice)
        self.assertTrue(result['retry_prepared'])
        self.assertEqual(self.svc.status()['state'], 'succeeded')
        self.assertEqual(len(self.body.submits), 5)
        self.assertNotEqual(self.body.submits[0], self.body.submits[1])
        again = self.confirm(next_advice)
        self.assertTrue(again['no_op'])
        self.assertEqual(len(self.body.submits), 5)

    def test_stale_revision_and_replaced_task_cannot_confirm(self):
        advice = self.start()
        with self.assertRaises(Rejected):
            self.svc.confirm_advice('advice-task', advice['advice_id'], advice['record_revision'] - 1)
        with self.assertRaises(Rejected):
            self.svc.confirm_advice('different-task', advice['advice_id'], advice['record_revision'])
        self.svc.control('cancel')
        with self.assertRaises(Rejected): self.confirm(advice)
        self.assertEqual(len(self.body.submits), 1)

    def test_duplicate_and_restart_do_not_replay_authorization(self):
        advice = self.start()
        receipt, fresh = self.svc.runtime.claim_advice(advice['advice_id'], advice['record_revision'])
        self.assertTrue(fresh)
        self.svc = self.service(lambda _: '{}')
        result = self.confirm(advice)
        self.assertTrue(result['no_op'])
        self.assertEqual(result['confirmation']['status'], 'processing')
        self.assertEqual(len(self.body.submits), 1)

    def test_model_unavailable_falls_back_and_default_disables_confirmation(self):
        advice = self.start('illegal-tool')
        self.assertEqual(advice['source'], 'rules')
        self.svc.config['brain']['reasoning'].pop('confirm_recovery_advice')
        with self.assertRaises(Rejected): self.confirm(advice)
        self.assertEqual(len(self.body.submits), 1)

    def test_unknown_result_does_not_offer_continue(self):
        advice = self.start('adaptive')
        original = self.svc.runtime.record['open_command_id']
        self.body.commands[original] = {'command_id': original, 'timed_out': True}
        self.assertFalse(self.confirm(advice)['result_known'])
        next_advice = self.svc.status()['recovery_advice']
        self.assertNotIn('continue', next_advice['allowed_actions'])
        self.assertEqual(len(self.body.submits), 1)

    def test_query_success_waits_for_confirmation_and_does_not_repeat_action(self):
        advice = self.start('adaptive')
        original = self.svc.runtime.record['open_command_id']
        self.body.commands[original].update(terminal='succeeded', evidence={
            'identity_ok': True, 'time_ok': True, 'supports': True, 'grade': 'object'})
        self.confirm(advice)
        self.assertEqual(len(self.body.submits), 1)
        self.body.outcomes.clear()
        result = self.confirm(self.svc.status()['recovery_advice'])
        self.assertTrue(result['verifying_original'])
        self.assertEqual(len(self.body.submits), 4)
        self.assertEqual(self.svc.status()['state'], 'succeeded')

    def test_cancel_advice_only_cancels_task(self):
        advice = self.start('cancel')
        self.confirm(advice)
        self.assertEqual(self.svc.status()['state'], 'cancelled')
        self.assertEqual(self.body.cancels, [])

    def test_cancel_during_query_wins_over_late_result(self):
        advice = self.start(app=True)
        entered, release = threading.Event(), threading.Event()
        original_query = self.body.query
        def query(*args):
            entered.set(); release.wait(3)
            return original_query(*args)
        outcome = []
        with patch.object(self.body, 'query', side_effect=query):
            thread = threading.Thread(target=lambda: outcome.append(self.app.confirm_advice(
                'advice-task', advice['advice_id'], advice['record_revision'])))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.app.control('cancel', task_id='advice-task')
            finally:
                release.set(); thread.join(4)
        self.assertEqual(self.app.status()['state'], 'cancelled')
        self.assertEqual(len(self.body.submits), 1)
        self.assertFalse(outcome[0]['accepted'])

    def test_http_rejects_extra_action_and_double_click_does_not_repeat_query(self):
        advice = self.start(app=True)
        client = create_app(self.app).test_client()
        body = {'task_id': 'advice-task', 'advice_id': advice['advice_id'], 'expected_revision': advice['record_revision']}
        self.assertEqual(client.post('/api/task_advice/confirm', json={**body, 'action': 'continue'}).status_code, 400)
        first = client.post('/api/task_advice/confirm', json=body)
        self.assertEqual(first.status_code, 200, first.json)
        queries = list(self.body.queries)
        second = client.post('/api/task_advice/confirm', json=body)
        self.assertTrue(second.json['no_op'])
        self.assertEqual(self.body.queries, queries)

    def test_two_simultaneous_confirmations_authorize_only_one_retry(self):
        advice = self.start('continue', app=True)
        self.body.outcomes.clear()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.app.confirm_advice(
            'advice-task', advice['advice_id'], advice['record_revision']))) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(4)
        self.wait(lambda: self.app.status()['state'] == 'succeeded')
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(bool(r.get('no_op')) for r in results), 1)
        self.assertEqual(len(self.body.submits), 5)
        self.assertEqual(len(self.app.status()['advice_confirmations']), 1)

    def test_pause_while_already_in_recovery_keeps_existing_control_semantics(self):
        advice = self.start(app=True)
        entered, release = threading.Event(), threading.Event()
        original_query = self.body.query
        def query(*args):
            if threading.current_thread().name.startswith('brain-control') and not entered.is_set():
                entered.set(); release.wait(3)
            return original_query(*args)
        results = []
        with patch.object(self.body, 'query', side_effect=query):
            thread = threading.Thread(target=lambda: results.append(self.app.confirm_advice(
                'advice-task', advice['advice_id'], advice['record_revision'])))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                paused = self.app.control('pause', task_id='advice-task')
            finally:
                release.set(); thread.join(4)
        self.assertEqual(self.app.status()['state'], paused['state'])
        self.assertFalse(paused['accepted'])
        self.assertTrue(results[0]['query_only'])
        self.assertEqual(len(self.body.submits), 1)

    def test_changed_target_rejects_confirmation(self):
        advice = self.start()
        with patch('brain.service.target_identity', return_value={'backend': 'different'}):
            with self.assertRaises(Rejected): self.confirm(advice)
        self.assertEqual(len(self.body.submits), 1)
