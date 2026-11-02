"""Saved Runtime events -> gateway contract -> small read-only report, without robot I/O."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from flask import Flask

from brain.api.diagnostic_reports import register_diagnostic_reports
from brain.observability.diagnostic_runs import DiagnosticRuns, run_id_for_task
from brain.storage.tasks import KernelStore
from contracts.diagnostic_reports import validate_report, validate_request
from contracts.planning import Plan
from tests.factories import TaskRuntime
from tests.brain import test_diagnostic_runs as run_fixture
from tests.brain import test_kernel_reception as runtime_fixture


class ContractGateway:
    """Implements frozen wire shapes using the Gateway-provided no-recording fixture."""
    def __init__(self):
        self.reports = {}
        self.calls = []
        self.fixture = json.loads(Path('tests/contracts/robot_diagnostic_report.example.json').read_text())

    def _reply(self, run_id, event_id=None):
        body = deepcopy(self.reports[run_id])
        body['generated_at'] = datetime.now().astimezone().isoformat()
        if event_id:
            body['event_id'] = event_id
        return validate_report(body, run_id=run_id, task_id=body['task_id'], event_id=event_id)

    def begin_run(self, payload):
        payload = validate_request('begin', payload)
        self.calls.append(('begin', deepcopy(payload)))
        body = deepcopy(self.fixture)
        body.update(run_id=payload['run_id'], task_id=payload['task_id'])
        body['window']['requested_start'] = payload['started_at']
        self.reports.setdefault(payload['run_id'], body)
        return self._reply(payload['run_id'])

    def add_event(self, run_id, payload):
        payload = validate_request('event', payload, run_id=run_id)
        self.calls.append(('event', deepcopy(payload)))
        return self._reply(run_id, payload['event_id'])

    def finish_run(self, run_id, payload):
        payload = validate_request('finish', payload, run_id=run_id)
        self.calls.append(('finish', deepcopy(payload)))
        body = self.reports[run_id]
        body['window']['requested_end'] = payload['ended_at']
        body['status'] = 'partial'
        body['quality']['analysis']['state'] = 'finished'
        body['quality']['recorder_released'] = True
        return self._reply(run_id)

    def get_report(self, run_id, *, task_id):
        self.calls.append(('get', {'run_id': run_id, 'task_id': task_id}))
        assert self.reports[run_id]['task_id'] == task_id
        return self._reply(run_id)


class RobotReportFlowTests(unittest.TestCase):
    def test_control_index_read_budget_never_changes_original_ledger(self):
        from brain.application import BrainApplication
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'body_control.json'
            path.write_bytes(b' ' * (8 * 1024 * 1024 + 1))
            app = object.__new__(BrainApplication)
            app.body_control = SimpleNamespace(store=SimpleNamespace(path=path))
            with self.assertRaisesRegex(ValueError, '预算'):
                app._diagnostic_control_records()
            self.assertEqual(path.stat().st_size, 8 * 1024 * 1024 + 1)
            path.write_text(json.dumps({'source_gate': {'operations': {}}}))
            self.assertEqual(app._diagnostic_control_records(), {'source_gate': {'operations': {}}})

    def test_full_fake_runtime_keeps_business_success_separate_from_partial_capture(self):
        gateway, pool = ContractGateway(), run_fixture.ManualPool()
        runs = DiagnosticRuns({'brain': {'diagnostics': {'enabled': True}}}, client=gateway, pool=pool)
        self.addCleanup(runs.close)
        with tempfile.TemporaryDirectory() as directory:
            port = runtime_fixture.FakeBody()
            runtime = TaskRuntime(KernelStore(directory), port,
                                  config={'reception_real': {'dream_inspection_enabled': False}})
            phases = tuple(runtime.phases)
            runtime.event_sink = runs.event
            runtime.open_task('flow-test', task_desc='offline reception', planning=True,
                              execution_backend='unselected', reflection_enabled=False)
            runtime.bind_execution({'package': 'reception'}, 'reception_real', {'backend': 'reception_real'}, port)
            pool.drain(limit=100)  # A normal capture intent reaches the gateway before task completion.
            runtime.plan(lambda: Plan(phases))
            runtime.drive()
            self.assertEqual(runtime.state, 'succeeded')
            before = Path(directory, 'current_task.json').read_bytes()
            pool.drain(limit=400)
            runs.tick()
            pool.drain(limit=400)
            self.assertEqual(Path(directory, 'current_task.json').read_bytes(), before)
            self.assertEqual([action for action, _ in gateway.calls].count('begin'), 1)
            self.assertEqual([action for action, _ in gateway.calls].count('finish'), 1)
            finish = next(body for action, body in gateway.calls if action == 'finish')
            original_commands = {attempt['command_id'] for bucket in runtime.record['steps'].values()
                                 for attempt in bucket.get('attempts', []) if attempt.get('command_id')}
            self.assertTrue(set(finish['command_ids']) <= original_commands)
            self.assertTrue(finish['command_ids'])
            app = Flask(__name__)
            register_diagnostic_reports(app, SimpleNamespace(diagnostic_runs=runs))
            reader = app.test_client()
            report = reader.get('/api/task_diagnostics?task_id=flow-test').get_json()
            self.assertEqual(report['run_id'], run_id_for_task('flow-test'))
            self.assertEqual(report['report']['status'], 'partial')
            self.assertIn('recording_disabled', report['report']['quality']['missing_evidence'])
            self.assertFalse(report['task_authority'])
            before_calls = len(gateway.calls)
            reader.get('/api/task_diagnostics?task_id=flow-test&refresh=1')
            pool.drain()
            self.assertTrue(all(action == 'get' for action, _ in gateway.calls[before_calls:]))
            self.assertEqual(Path(directory, 'current_task.json').read_bytes(), before)

    def test_gateway_complete_fixture_does_not_imply_whole_robot_completeness(self):
        body = json.loads(Path('tests/contracts/robot_diagnostic_report_complete.example.json').read_text())
        report = validate_report(body, run_id=body['run_id'], task_id=body['task_id'])
        self.assertEqual(report['status'], 'complete')
        self.assertFalse(report['quality']['whole_robot_complete'])
        self.assertEqual(report['quality']['loss_visibility'], 'unknown')
        self.assertEqual(report['quality']['external_summaries'][0]['verification'], 'source_declared')
        self.assertIsNone(report['quality']['topics']['/g1pilot/navigation_state']['max_header_record_age_ms'])
        self.assertFalse(report['state_authority'])
