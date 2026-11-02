"""Diagnostic failures must not change execution receipts or erase evidence changes."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from io import StringIO
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from brain.adapters.vla_client import VlaClient
from shared.brain_journal import BrainJournal, CallBook, reset_journal_for_tests
from shared.diagnostic_context import diagnostic_context
from shared.diagnostic_io import DiagnosticWriter, bound_record, redact
from shared.log_setup import _Tee


class DiagnosticIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, FQPLANNER_LOG_ROOT=self.temp.name)
        self.env.start()
        self.journal = BrainJournal(echo_process=False)
        reset_journal_for_tests(self.journal)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)
        self.addCleanup(reset_journal_for_tests)

    def records(self):
        self.journal.flush(1)
        return [json.loads(line) for p in sorted(Path(self.temp.name).glob('*/master/brain*.jsonl'))
                for line in p.read_text().splitlines()]

    def test_log_disk_failure_after_accept_does_not_replace_peer_receipt(self):
        client = VlaClient('http://offline.invalid')
        receipt = {'state': 'running', 'task_id': 't1', 'command_id': 'c1'}
        original = self.journal.paths
        count = 0
        def disk_full(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic ENOSPC')
            return original(*args, **kwargs)
        with patch.object(client.http, 'request_json', return_value=(receipt, 202, {})) as http, \
                patch.object(self.journal, 'paths', side_effect=disk_full):
            actual = client.submit_task({'task_id': 't1', 'command_id': 'c1', 'operation': 'open_fridge'})
        self.assertEqual(actual, receipt)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.journal.health()['write_errors'], 1)
        self.assertTrue(self.journal.health()['degraded'])

    def test_broken_stdout_keeps_structured_event_and_does_not_escape(self):
        self.journal.echo_process = True
        with patch('builtins.print', side_effect=BrokenPipeError('synthetic closed pipe')):
            result = self.journal.emit('TASK', event='task_opened', task_id='t1')
        self.assertEqual(self.records()[0]['event_id'], result['event_id'])
        self.assertEqual(self.journal.health()['write_errors'], 1)

    def test_ignored_background_future_exception_reaches_process_output(self):
        from brain.workers import RuntimeOwner
        self.journal.echo_process = True
        owner = RuntimeOwner()
        output = StringIO()
        def fail():
            raise RuntimeError('background failure token=must-not-leak')
        try:
            with redirect_stdout(output), diagnostic_context(task_id='task-background', command_id='original-command'):
                future = owner.submit(fail)
                deadline = time.monotonic() + 1
                while not future.done() and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertTrue(future.done())
                # Deliberately never inspect the Future result/exception.
                self.assertTrue(owner.close(1))
            text = output.getvalue()
            self.assertIn('RuntimeError', text)
            self.assertIn('background failure', text)
            self.assertIn('task-bac', text)
            self.assertIn('original-command', text)
            self.assertNotIn('must-not-leak', text)
            self.assertNotIn('Traceback (most recent call last)', text)
            records = [r for r in self.records() if r['kind'] == 'DIAGNOSTIC']
            self.assertEqual(len(records), 1)
            self.assertIn('Traceback (most recent call last)', records[0]['exception'])
            self.assertNotIn('must-not-leak', records[0]['exception'])
        finally:
            owner.close(1)

    def test_standard_logging_handler_archives_without_second_process_echo(self):
        from shared.brain_journal import _JournalDiagnosticHandler
        self.journal.echo_process = True
        logger = logging.getLogger('test.brain.standard.once')
        standard, duplicate = StringIO(), StringIO()
        console = logging.StreamHandler(standard)
        archival = _JournalDiagnosticHandler(self.journal)
        with patch.object(logger, 'handlers', [console, archival]), patch.object(logger, 'propagate', False):
            with redirect_stdout(duplicate):
                try:
                    raise ValueError('standard logger fault')
                except ValueError:
                    logger.exception('standard exception')
        self.assertEqual(duplicate.getvalue(), '')
        self.assertEqual(standard.getvalue().count('ValueError: standard logger fault'), 1)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['_process_already_logged'])

    def test_slow_writer_queue_is_bounded_shutdown_is_bounded_and_loss_visible(self):
        writer = DiagnosticWriter(asynchronous=True, capacity=2)
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
        try:
            self.assertTrue(writer.submit(slow))
            self.assertTrue(entered.wait(1))
            self.assertTrue(writer.submit(lambda: None, event_id='retained', critical=True))
            self.assertTrue(writer.submit(lambda: None, event_id='retained-2', critical=True))
            started = time.monotonic()
            self.assertFalse(writer.submit(lambda: None, event_id='lost', critical=True))
            self.assertFalse(writer.close(.02))
            self.assertLess(time.monotonic() - started, .5)
            health = writer.health()
            self.assertLessEqual(health['queued'], 2)
            self.assertEqual(health['dropped'], 1)
            self.assertIn('lost', health['lost_event_ids'])
        finally:
            release.set()
            self.assertTrue(writer.close(1))

    def test_redaction_preserves_identity_false_and_null(self):
        raw = {'task_id': 'task', 'command_id': 'command', 'request_id': 'request',
               'body_stopped': False, 'holding': None,
               'headers': {'Authorization': 'Bearer secret-one', 'Cookie': 'session=secret-two'},
               'context': {'worker_token': 'secret-three', 'api_key': 'secret-four'},
               'error': 'https://user:password@host/path?access_token=secret-five&task_id=task',
               'exception': "token='secret-six'"}
        clean = redact(raw)
        for word in ('secret-one', 'secret-two', 'secret-three', 'secret-four', 'secret-five', 'secret-six', 'password@'):
            self.assertNotIn(word, json.dumps(clean))
        self.assertEqual(clean['task_id'], raw['task_id'])
        self.assertEqual(clean['command_id'], raw['command_id'])
        self.assertEqual(clean['request_id'], raw['request_id'])
        self.assertIs(clean['body_stopped'], False)
        self.assertIsNone(clean['holding'])
        self.assertEqual(raw['headers']['Authorization'], 'Bearer secret-one')

    def test_secret_only_dictionary_consumes_sanitizer_budget(self):
        raw = {f'component_{n}_token': 'secret' for n in range(10000)}
        clean = redact(raw, max_items=20)
        self.assertLessEqual(len(clean), 20)
        self.assertIn('_diagnostic_truncated', clean)

    def test_async_queue_overflow_never_writes_producer_stderr(self):
        writer = DiagnosticWriter(asynchronous=True, capacity=1)
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(2)
        try:
            writer.submit(slow)
            self.assertTrue(entered.wait(1))
            writer.submit(lambda: None, critical=True)
            with patch('sys.__stderr__') as stderr:
                self.assertFalse(writer.submit(lambda: None, critical=True))
                stderr.write.assert_not_called()
                stderr.flush.assert_not_called()
        finally:
            release.set()
            writer.close(1)

    def test_close_cannot_overtake_an_accepted_enqueue(self):
        writer = DiagnosticWriter(asynchronous=True, capacity=2)
        entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
        original = writer.queue.put_nowait
        results = []
        def paused_put(item):
            entered.set()
            release.wait(1)
            original(item)
        try:
            with patch.object(writer.queue, 'put_nowait', side_effect=paused_put):
                submitter = threading.Thread(target=lambda: results.append(writer.submit(delivered.set)))
                submitter.start()
                self.assertTrue(entered.wait(1))
                closer = threading.Thread(target=lambda: writer.close(1))
                closer.start()
                release.set()
                submitter.join(1)
                closer.join(1)
            self.assertEqual(results, [True])
            self.assertTrue(delivered.is_set())
            self.assertEqual(writer.health()['pending'], 0)
        finally:
            release.set()
            writer.close(1)

    def test_partial_stdout_fragments_are_sanitized_together_and_failures_isolated(self):
        original, disk = StringIO(), StringIO()
        tee = _Tee(original, disk)
        tee.write('Authorization: ')
        tee.write('Bearer secret-fragment')
        tee.flush()
        tee.write('\n')
        self.assertNotIn('secret-fragment', disk.getvalue())
        self.assertNotIn('secret-fragment', original.getvalue())
        with patch.object(disk, 'write', side_effect=OSError('synthetic disk full')):
            tee.write('plain diagnostic\n')
        self.assertIn('plain diagnostic', original.getvalue())
        self.assertEqual(tee._writer.health()['write_errors'], 1)

    def test_event_metadata_and_nonfinite_or_recursive_values_are_explicit(self):
        recursive = {}
        recursive['loop'] = recursive
        a = self.journal.emit('TASK', event='one', task_id='t', detail=recursive, metric=float('nan'))
        b = self.journal.emit('TASK', event='two', task_id='t')
        self.assertEqual(a['log_schema_version'], 1)
        self.assertEqual(a['service_instance_id'], b['service_instance_id'])
        self.assertNotEqual(a['event_id'], b['event_id'])
        self.assertIsNotNone(datetime.fromisoformat(a['time']).tzinfo)
        self.assertIn('nonfinite', a['metric'])
        self.assertIn('recursive', a['detail']['loop'])
        self.assertEqual(len(self.records()), 2)

    def test_size_and_day_rotation_preserves_parseable_records(self):
        self.journal.max_file_bytes = 1024
        today = datetime(2026, 11, 2, 23, 59, tzinfo=timezone.utc)
        for n in range(5):
            with patch('shared.brain_journal._stamp', return_value=today if n < 3 else today + timedelta(days=1)):
                self.journal.emit('TASK', event='rotation', task_id='t', ordinal=n, detail='x' * 900)
        self.assertEqual(sorted(r['ordinal'] for r in self.records()), list(range(5)))
        self.assertGreater(len(list(Path(self.temp.name).glob('*/master/brain*.jsonl'))), 2)

    def test_oversize_record_has_explicit_truncation_and_bounded_size(self):
        value = bound_record({'task_id':'t', 'event':'large', 'detail':['x' * 10000 for _ in range(100)]})
        self.assertTrue(value['diagnostic_truncated'])
        self.assertEqual(value['task_id'], 't')
        self.assertLessEqual(len(json.dumps(value).encode()), 524288)

    def test_terminal_query_dedup_preserves_same_state_stop_changes(self):
        book = CallBook('vla')
        base = {'task_id':'t', 'command_id':'c', 'state':'failed',
                'error':{'code':'FAULT','message':'failure'}, 'exit_state':{'body_stopped':False}}
        book.start('open_fridge', task_id='t', command_id='c')
        self.assertTrue(book.observe('c', base, task_id='t'))
        book.complete('c', False, payload=base)
        self.assertFalse(book.observe('c', base, task_id='t'))
        changed = deepcopy(base)
        changed['exit_state']['body_stopped'] = True
        self.assertTrue(book.observe('c', changed, task_id='t'))
        self.assertFalse(book.observe('c', changed, task_id='t'))
        states = [r for r in self.records() if r['event'] == 'state']
        self.assertEqual([r['evidence_changes']['exit_state.body_stopped'] for r in states], [False, True])

    def test_foreign_receipt_does_not_rename_requested_command_or_task(self):
        book = CallBook('vla')
        book.observe('requested-c', {'task_id':'foreign-t', 'command_id':'foreign-c','state':'failed'}, task_id='requested-t')
        record = self.records()[-1]
        self.assertEqual(record['task_id'], 'requested-t')
        self.assertEqual(record['cmd'], 'requested-c')
        self.assertEqual(record['observed_task_id'], 'foreign-t')
        self.assertEqual(record['observed_command_id'], 'foreign-c')
        self.assertIs(record['identity_match'], False)

    def test_explicit_other_task_does_not_carry_previous_command(self):
        with diagnostic_context(task_id='old', command_id='old-command', step_id='old-step'):
            record = self.journal.emit('TASK', event='new', task_id='new')
        self.assertEqual(record['task_id'], 'new')
        self.assertNotIn('command_id', record)
        self.assertNotIn('step_id', record)

    def test_late_call_keeps_its_original_context_not_current_task_context(self):
        from shared.brain_journal import OutboundCall
        with diagnostic_context(task_id='original', command_id='original-command', trace_id='original-trace'):
            call = OutboundCall('vla', 'open_fridge', command_id='original-command')
        with diagnostic_context(task_id='new', command_id='new-command', control_id='new-control', trace_id='new-trace'):
            call.update({'state':'failed','command_id':'original-command','task_id':'original'})
        record = self.records()[-1]
        self.assertEqual(record['task_id'], 'original')
        self.assertEqual(record['trace_id'], 'original-trace')
        self.assertNotIn('control_id', record)

    def test_legacy_current_task_is_local_to_its_context(self):
        self.journal.set_current_task('parent-task')
        result = []
        thread = threading.Thread(target=lambda: result.append(self.journal.emit('TASK', event='unbound')))
        thread.start()
        thread.join(1)
        self.assertIsNone(result[0]['task_id'])

    def test_same_running_query_has_low_frequency_wait_summary(self):
        book = CallBook('vla')
        call = book.start('open_fridge', task_id='t', command_id='c', wait_summary_sec=30)
        payload = {'state':'running', 'command_id':'c'}
        call.update(payload)
        for _ in range(5):
            self.assertFalse(call.update(payload))
        call._last_summary -= 31
        self.assertTrue(call.update(payload))
        last = self.records()[-1]
        self.assertEqual(last['event'], 'waiting')
        self.assertEqual(last['unchanged_polls'], 6)


if __name__ == '__main__':
    unittest.main()
