"""Diagnostic evidence cannot change task, command or receiver outcomes."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brain.adapters.dream_client import DreamClient
from brain.adapters.vla_client import VlaClient
from brain.observability.events import emit
from contracts.body_control import ControlError
from shared.brain_journal import BrainJournal, reset_journal_for_tests
from shared.diagnostic_context import diagnostic_context
from shared.task_text import format_process_event
from tests.brain.test_body_control import nav_request
from tests.brain import test_source_gate as gate_fixture
from tests.brain import test_vla_terminal_sync as sync_fixture


class EvidenceProjectionTests(unittest.TestCase):
    def test_supplied_vla_fixture_preserves_both_failure_layers(self):
        sample = json.loads(Path('tests/contracts/vla_diagnostic_failure.example.json').read_text())
        self.assertTrue(sample['synthetic'])
        raw = sample['body']
        record = {'task_id': raw['task_id'], 'phase': 'VLA_OPEN_FRIDGE',
                  'open_command_id': raw['command_id'], 'state': 'recovery_required',
                  'blocked_reason': raw['error']['message'], 'resources_cleared': False,
                  'steps': {'VLA_OPEN_FRIDGE': {'attempts': [{'attempt_id': 'sample-attempt',
                      'command_id': raw['command_id'], 'progress': {'evidence': {'downstream': raw}}}]}}}
        with patch('brain.observability.events.journal_emit') as sink:
            emit('vla_terminal_observed', record)
        event = dict(kind='TASK', **sink.call_args.kwargs)
        self.assertEqual(event['diagnostic']['downstream']['handoff']['original_execution_failure'],
                         raw['error']['details']['original_execution_failure'])
        text = format_process_event(event)
        for value in ('EXECUTION_STOP_UNCONFIRMED', 'SONIC_STREAM_CONTROL_LOST',
                      'exit_state.body_stopped=false', 'result.business_outcome="unverified"'):
            self.assertIn(value, text)

    def test_full_paths_preserve_conflicting_stop_and_instance_facts(self):
        raw = {'task_id': 'task', 'command_id': 'command', 'state': 'failed',
               'observed_at': '2026-11-02T05:00:01Z', 'completed_at': '2026-11-02T05:00:00Z',
               'error': {'code': 'EXECUTION_STOP_UNCONFIRMED', 'message': 'stop uncertain',
                         'details': {'original_execution_failure': {'code': 'SONIC_STREAM_CONTROL_LOST'}}},
               'result': {'body_stopped': True, 'business_outcome': 'verified'},
               'exit_state': {'body_stopped': False, 'publisher_stopped': True, 'hands_stopped': None},
               'handoff': {'execution': {'receiver_instance_id': 'o6', 'source_gate_receiver_instance_id': 'sonic'}},
               'completion': {'status': 'completed', 'evidence': {'observed_at': '2026-11-02T04:59:00Z'}}}
        record = {'task_id': 'task', 'phase': 'VLA_OPEN_FRIDGE', 'open_command_id': 'command',
                  'state': 'recovery_required', 'blocked_reason': 'stop uncertain',
                  'resources_cleared': False, 'stopped_confirmed': False,
                  'steps': {'VLA_OPEN_FRIDGE': {'attempts': [{'attempt_id': 'a1', 'command_id': 'command',
                      'progress': {'evidence': {'downstream': raw, 'identity_ok': True, 'time_ok': True}}}]}}}
        original = deepcopy(record)
        with patch('brain.observability.events.journal_emit') as sink:
            emit('vla_terminal_observed', record)
        data = sink.call_args.kwargs
        self.assertEqual(data['diagnostic']['downstream'], raw)
        self.assertEqual(record, original)
        line = format_process_event({'kind': 'TASK', **data})
        for text in ('EXECUTION_STOP_UNCONFIRMED', 'SONIC_STREAM_CONTROL_LOST',
                     'result.body_stopped=true', 'exit_state.body_stopped=false',
                     'exit_state.hands_stopped=null', 'result.business_outcome="verified"',
                     'resources_cleared=false'):
            self.assertIn(text, line)
        self.assertIn('最近保存', line)

    def test_query_error_is_visible_without_relabelling_last_saved_receipt(self):
        record = {'task_id': 'task', 'phase': 'VLA_OPEN_FRIDGE', 'state': 'waiting_human',
                  'blocked_reason': 'vla_status_unconfirmed',
                  'vla_confirmation': {'observation_error': 'original query timed out'},
                  'steps': {'VLA_OPEN_FRIDGE': {'attempts': [{'command_id': 'c',
                      'vla_query_error': {'error': 'original query timed out'},
                      'progress': {'evidence': {'downstream': {'state': 'running'}}}}]}}}
        with patch('brain.observability.events.journal_emit') as sink:
            emit('vla_status_unconfirmed', record)
        data = sink.call_args.kwargs
        self.assertEqual(data['diagnostic']['downstream']['state'], 'running')
        self.assertEqual(data['diagnostic']['query_error_receipt']['error'], 'original query timed out')
        self.assertIn('original query timed out', format_process_event({'kind': 'TASK', **data}))


class QueryLifecycleTests(unittest.TestCase):
    def test_supplied_nav_events_export_both_stop_observations(self):
        from brain.observability.diagnostics import query_diagnostics
        fixture = json.loads(Path('tests/contracts/nav_diagnostic_events.example.json').read_text())
        self.assertTrue(fixture['synthetic'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nav.jsonl'
            path.write_text(''.join(json.dumps(event) + '\n' for event in fixture['events']))
            exported = query_diagnostics(root=Path(directory) / 'local',
                command_id='nav-synthetic-command', peer_events=[('nav', path)])
        events = [row['event'] for row in exported['records']]
        self.assertEqual(len(events), 2)
        self.assertEqual([event['result']['navigation_stopped'] for event in events], [False, True])
        self.assertEqual(events[0]['completed_at'], events[1]['completed_at'])
        self.assertFalse(exported['state_authority'])

    def test_native_queries_log_terminal_and_changed_stop_evidence_without_new_posts(self):
        for cls, method in ((VlaClient, 'task'), (DreamClient, 'command')):
            with self.subTest(peer=cls.__name__), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {'FQPLANNER_LOG_ROOT': directory}):
                journal = BrainJournal()
                reset_journal_for_tests(journal)
                try:
                    client = cls('http://offline.invalid')
                    first = {'task_id': 'task', 'command_id': 'command', 'state': 'failed',
                             'error': {'code': 'FAULT'}, 'result': {'navigation_stopped': False},
                             'exit_state': {'body_stopped': False}}
                    second = deepcopy(first)
                    second['result']['navigation_stopped'] = True
                    second['exit_state']['body_stopped'] = True
                    with diagnostic_context(task_id='task'), patch.object(client.http, 'request_json',
                            side_effect=[(first, 200, {}), (first, 200, {}), (second, 200, {})]) as http:
                        for _ in range(3):
                            getattr(client, method)('command')
                    rows = [json.loads(s) for s in journal.paths()['jsonl'].read_text().splitlines()]
                    calls = [r for r in rows if r['kind'] == 'CALL']
                    self.assertEqual(len(calls), 2)
                    self.assertTrue(all(r['task_id'] == 'task' for r in calls))
                    self.assertTrue(all(c.args[0] == 'GET' for c in http.call_args_list))
                finally:
                    reset_journal_for_tests()


class WaitingVlaLogTests(unittest.TestCase):
    setUp = sync_fixture.VlaTerminalSyncTests.setUp
    tearDown = sync_fixture.VlaTerminalSyncTests.tearDown
    service = sync_fixture.VlaTerminalSyncTests.service
    terminal = sync_fixture.VlaTerminalSyncTests.terminal

    def test_background_terminal_produces_readable_evidence_without_finish(self):
        service = self.service()
        cid = self.terminal(service)
        row = self.vla.commands[cid]
        row['exit_state']['body_stopped'] = False
        row['result']['body_stopped'] = False
        with patch.dict(os.environ, {'FQPLANNER_LOG_ROOT': str(self.root / 'logs')}):
            journal = BrainJournal(echo_process=True)
            reset_journal_for_tests(journal)
            try:
                service.runtime.event_sink = lambda event, record: emit(event, record)
                output = StringIO()
                with redirect_stdout(output):
                    self.assertTrue(service.runtime.observe_waiting_vla())
                self.assertIn('TASK_EXECUTOR_FAILED', output.getvalue())
                self.assertIn('ENCODER_TIMEOUT', output.getvalue())
                self.assertIn('exit_state.body_stopped=false', output.getvalue())
                rows = [json.loads(line) for line in journal.paths()['jsonl'].read_text().splitlines()]
                observed = next(r for r in rows if r['event'] == 'vla_terminal_observed')
                self.assertEqual(observed['command_id'], cid)
                self.assertEqual(observed['diagnostic']['downstream']['error'], row['error'])
                self.assertEqual(service.runtime.state, 'recovery_required')
                self.assertEqual(self.vla.finish_posts, 0)
                self.assertEqual(len(self.vla.posts), 1)
            finally:
                reset_journal_for_tests()

    def test_late_result_logs_original_identity_after_new_task(self):
        service = self.service()
        cid = self.terminal(service)
        runtime = service.runtime
        query = self.port.query
        def late(*args):
            result = query(*args)
            runtime.request_cancel()
            runtime.open_task('new-task', task_desc='new task')
            return result
        with patch.object(self.port, 'query', side_effect=late), patch('shared.brain_journal.emit') as log:
            self.assertFalse(runtime.observe_waiting_vla())
        records = [c.kwargs for c in log.call_args_list if c.kwargs.get('event') == 'progress_ignored']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['task_id'], 'test-task')
        self.assertEqual(records[0]['command_id'], cid)
        self.assertEqual(records[0]['current_task_id'], 'new-task')
        self.assertEqual(runtime.record['task_id'], 'new-task')


class GateLogTests(unittest.TestCase):
    setUp = gate_fixture.SourceGateTests.setUp
    tearDown = gate_fixture.SourceGateTests.tearDown
    schedule = gate_fixture.SourceGateTests.schedule

    def test_lost_selection_response_logs_original_request_without_replay(self):
        self.gate.source, self.gate.lost = 'none', True
        request = nav_request()
        self.schedule(request)
        with patch.dict(os.environ, {'FQPLANNER_LOG_ROOT': str(self.root / 'logs')}):
            journal = BrainJournal()
            reset_journal_for_tests(journal)
            try:
                result = self.port.submit(request['body']['command_id'], request)
                rows = [json.loads(s) for s in journal.paths()['jsonl'].read_text().splitlines()]
            finally:
                reset_journal_for_tests()
        self.assertTrue(result['accepted'])
        gateway = [r for r in rows if r['kind'] == 'GATEWAY']
        self.assertIn('selection_unconfirmed', [r['event'] for r in gateway])
        self.assertIn('selection_verified', [r['event'] for r in gateway])
        self.assertEqual({r['request_id'] for r in gateway if 'request_id' in r}, {self.gate.puts[0]['request_id']})
        self.assertEqual({r['task_id'] for r in gateway}, {request['body']['task_id']})
        self.assertEqual(len(self.gate.puts), 1)
        self.assertEqual(len(self.nav.posts), 1)

    def test_same_source_and_broken_diagnostic_sink_do_not_send_extra_put(self):
        request = nav_request()
        self.schedule(request)
        with patch('shared.brain_journal.emit', side_effect=OSError('disk full')):
            self.assertTrue(self.port.submit(request['body']['command_id'], request)['accepted'])
        self.assertEqual(self.gate.puts, [])
        self.assertEqual(len(self.nav.posts), 1)

    def test_reconciling_old_selection_does_not_attach_it_to_new_task(self):
        self.control.timeout, self.control.poll = .002, .001
        original = self.gate.set
        def pending(body):
            result = original(body)
            result['state'] = 'pending'
            self.gate.requests[body['request_id']] = deepcopy(result)
            return result
        with patch.dict(os.environ, {'FQPLANNER_LOG_ROOT': str(self.root / 'logs')}):
            journal = BrainJournal()
            reset_journal_for_tests(journal)
            try:
                with diagnostic_context(task_id='old-task', command_id='old-command'), \
                     patch.object(self.gate, 'set', side_effect=pending):
                    with self.assertRaises(ControlError):
                        self.control._select('vla', 'vla:old-key')
                rid = self.gate.puts[0]['request_id']
                self.gate.requests[rid]['state'] = 'applied'
                with diagnostic_context(task_id='new-task', command_id='new-command'):
                    self.control._select('vla', 'vla:new-key')
                rows = [json.loads(s) for s in journal.paths()['jsonl'].read_text().splitlines()]
                observed = [r for r in rows if r.get('event') == 'receipt_observed' and r.get('request_id') == rid]
                self.assertTrue(observed)
                self.assertEqual({r['task_id'] for r in observed}, {'old-task'})
                self.assertTrue(any(r.get('event') == 'source_reused' and r['task_id'] == 'new-task' for r in rows))
                self.assertEqual(len(self.gate.puts), 1)
            finally:
                reset_journal_for_tests()
