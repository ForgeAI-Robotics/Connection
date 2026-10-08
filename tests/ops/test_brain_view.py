import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.app import create_app
from ops.brain_view import current_alert, overview
from shared.brain_journal import BrainJournal


class BrainViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'FQPLANNER_LOG_ROOT': self.temp.name})
        env.start(); self.addCleanup(env.stop)
        self.app = create_app()
        self.client = self.app.test_client()
        for worker in self.app.extensions['workers']:
            self.addCleanup(worker.shutdown)

    def test_cross_day_failure_is_history_not_current_alert(self):
        old = Path(self.temp.name) / '2026-09-28' / 'master'
        old.mkdir(parents=True)
        (old / 'brain.pin').write_text('CALL cancelled ok=false')
        journal = BrainJournal()
        journal.emit('PREFLIGHT', event='boot')
        result = self.client.get('/api/services/master/logs?kind=brain').get_json()
        self.assertIsNone(result['pin'])
        self.assertIn('boot', result['text'])
        self.assertIsNone(current_alert({'ready': True, 'state': 'healthy'},
                         {'state': 'cancelled', 'active': False}, {}))
        self.assertTrue((old / 'brain.pin').exists())

    def test_unresolved_motion_stays_visible_even_if_task_says_cancelled(self):
        health = {'ready': True, 'state': 'healthy'}
        task = {'state': 'cancelled', 'task_id': 'original', 'resources_cleared': False}
        alert = current_alert(health, task, {})
        self.assertEqual(alert['level'], 'ERROR')
        self.assertEqual(alert['task_id'], 'original')
        self.assertIsNotNone(current_alert(health, {'state': 'recovery_required'}, {}))
        self.assertIsNotNone(current_alert(health, {}, {'task': 'timeout'}))

    def test_overview_reads_only_current_brain_and_reports_partial_failure(self):
        values = {'/health': {'ready': True, 'state': 'healthy'},
                  '/api/task_status': {'task_id': 't', 'state': 'waiting_human', 'blocked_reason': '确认交接'},
                  '/api/execution_profile': {'config': {'mode': 'real'}}}
        with patch('ops.brain_view._get', side_effect=lambda path: values[path]) as get:
            result = overview()
        self.assertEqual(get.call_count, 3)
        self.assertEqual(result['alert']['message'], '确认交接')
        self.assertEqual(result['profile']['config']['mode'], 'real')
        with patch('ops.brain_view._get', side_effect=OSError('offline')):
            result = self.client.get('/api/brain/overview').get_json()
        self.assertEqual(result['alert']['level'], 'ERROR')
        self.assertEqual(set(result['errors']), {'health', 'task', 'profile'})
