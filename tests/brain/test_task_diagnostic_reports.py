"""Read-only task-bound report views must never start recording or task actions."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from flask import Flask
from brain.api.diagnostic_reports import register_diagnostic_reports


def report_view(task_id='task-001', **changes):
    report = json.loads(Path('tests/contracts/robot_diagnostic_report.example.json').read_text())
    report['task_id'], report['run_id'] = task_id, 'diag-' + task_id
    result = {'enabled': True, 'task_id': task_id, 'run_id': report['run_id'], 'tracking': 'finished',
              'refreshing': False, 'report': report, 'error': None, 'incomplete_reasons': [],
              'dropped_events': 0, 'updated_at': '2026-11-02T08:00:01+00:00'}
    result.update(changes)
    return result


class TaskDiagnosticReportTests(unittest.TestCase):
    def setUp(self):
        self.runs = Mock(spec=['summary', 'refresh', 'begin', 'finish'])
        self.runs.summary.side_effect = lambda task_id: report_view(task_id)
        self.runs.refresh.side_effect = lambda task_id: report_view(task_id, refreshing=True)
        self.application = SimpleNamespace(diagnostic_runs=self.runs, status=Mock(side_effect=AssertionError('no latest task fallback')))
        app = Flask(__name__)
        register_diagnostic_reports(app, self.application)
        self.client = app.test_client()

    def test_query_and_refresh_only_read_original_task(self):
        response = self.client.get('/api/task_diagnostics?task_id=old-task')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['report']['task_id'], 'old-task')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        refresh = self.client.get('/api/task_diagnostics?task_id=old-task&refresh=1')
        self.assertEqual(refresh.status_code, 202)
        self.runs.refresh.assert_called_once_with('old-task')
        current = self.client.get('/api/task_diagnostics?task_id=new-task')
        self.assertEqual(current.get_json()['report']['task_id'], 'new-task')
        self.runs.begin.assert_not_called(); self.runs.finish.assert_not_called()
        self.application.status.assert_not_called()

    def test_missing_identity_and_mutating_parameters_never_call_coordinator(self):
        for query in ('', '?task_id=', '?task_id=../x', '?task_id=a&task_id=b',
                      '?task_id=a&refresh=2', '?task_id=a&action=begin', '?task_id=a&run_id=other'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/task_diagnostics'+query).status_code, 400)
        self.assertEqual(self.client.post('/api/task_diagnostics?task_id=a').status_code, 405)
        self.runs.summary.assert_not_called(); self.runs.refresh.assert_not_called()

    def test_head_never_schedules_a_gateway_refresh(self):
        self.assertEqual(self.client.head('/api/task_diagnostics?task_id=task-001&refresh=1').status_code, 200)
        self.runs.summary.assert_called_once_with('task-001')
        self.runs.refresh.assert_not_called()

    def test_unavailable_untracked_disabled_and_cached_error_are_distinct(self):
        for changes, code in [({'report': None, 'tracking': 'not_tracked'}, 404),
                              ({'report': None, 'tracking': 'disabled', 'enabled': False}, 200),
                              ({'report': None, 'error': {'code': 'RUN_NOT_FOUND', 'message': 'missing'}}, 404),
                              ({'report': None, 'error': {'code': 'OFFLINE', 'message': 'unavailable'}}, 503),
                              ({'error': {'code': 'OFFLINE', 'message': 'cached only'}}, 200)]:
            with self.subTest(changes=changes):
                self.runs.summary.side_effect = None
                self.runs.summary.return_value = report_view(**changes)
                response = self.client.get('/api/task_diagnostics?task_id=task-001')
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.get_json()['tracking'], changes.get('tracking', 'finished'))

    def test_foreign_task_or_run_report_never_leaks(self):
        for where, key, value in [('wrapper','task_id','foreign'), ('report','task_id','foreign'),
                                  ('report','run_id','foreign'), ('report','schema','unknown')]:
            with self.subTest(where=where,key=key):
                body = report_view()
                (body if where == 'wrapper' else body['report'])[key] = value
                self.runs.summary.side_effect = None; self.runs.summary.return_value = body
                response = self.client.get('/api/task_diagnostics?task_id=task-001')
                self.assertEqual(response.status_code, 502)
                self.assertIsNone(response.get_json()['report'])

    def test_redacted_compact_projection_and_size_limit(self):
        body = report_view()
        body['Authorization'] = 'Bearer wrapper-secret'
        body['report']['raw_topic_payload'] = {'secret':'raw-not-exported'}
        body['report']['findings'][0]['message'] = 'Authorization: Bearer finding-secret'
        self.runs.summary.side_effect = None; self.runs.summary.return_value = body
        response = self.client.get('/api/task_diagnostics?task_id=task-001')
        text = response.get_data(as_text=True)
        for secret in ('wrapper-secret','raw-not-exported','finding-secret'):
            self.assertNotIn(secret,text)
        self.assertIn('quality',response.get_json()['report'])
        body['report']['quality']['oversize'] = 'x' * 100000
        response = self.client.get('/api/task_diagnostics?task_id=task-001')
        self.assertLess(len(response.data), 65536)
        self.assertEqual(response.get_json()['error']['code'], 'REPORT_VIEW_TOO_LARGE')

    def test_missing_coordinator_and_read_failure_are_visible(self):
        self.application.diagnostic_runs = None
        self.assertEqual(self.client.get('/api/task_diagnostics?task_id=task-001').status_code, 503)
        self.application.diagnostic_runs = self.runs
        self.runs.summary.side_effect = RuntimeError('Authorization: Bearer never-export-this')
        response = self.client.get('/api/task_diagnostics?task_id=task-001')
        self.assertEqual(response.status_code,503)
        self.assertNotIn('never-export-this', response.get_data(as_text=True))

    def test_web_entry_forwards_only_get_and_preserves_original_identity(self):
        from entries.web.app import create_app
        brain=Mock()
        brain.request.return_value=SimpleNamespace(content=json.dumps(report_view('old-task',refreshing=True)).encode(),
            status_code=202,headers={'content-type':'application/json'})
        client=create_app(client=brain,ops_client=Mock()).test_client()
        response=client.get('/api/task_diagnostics?task_id=old-task&refresh=1')
        self.assertEqual(response.status_code,202)
        self.assertEqual(response.get_json()['task_id'],'old-task')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        args,kwargs=brain.request.call_args
        self.assertEqual(args,('GET','/api/task_diagnostics'))
        self.assertEqual(dict(kwargs['params']),{'task_id':'old-task','refresh':'1'})
        self.assertEqual(kwargs['timeout'],5)
        self.assertEqual(client.post('/api/task_diagnostics?task_id=old-task').status_code,405)
        self.assertEqual(brain.request.call_count,1)


if __name__ == '__main__':
    unittest.main()
