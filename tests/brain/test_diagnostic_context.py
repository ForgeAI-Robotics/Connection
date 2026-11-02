"""Correlation follows submitted work, including failures nobody waits for."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brain.adapters.http_client import HttpClient, HttpContractError
from brain.adapters.model import ModelClient
from brain.workers import RuntimeOwner, WorkerPool
from contracts.tasks import Rejected
from shared.brain_journal import BrainJournal, reset_journal_for_tests
from shared.diagnostic_context import (context_fields, diagnostic_context,
    diagnostic_event, report_exception)


class DiagnosticContextTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        env = patch.dict(os.environ, {'FQPLANNER_LOG_ROOT': scratch.name})
        env.start(); self.addCleanup(env.stop)
        self.journal = BrainJournal()
        reset_journal_for_tests(self.journal)
        self.addCleanup(reset_journal_for_tests)

    def rows(self, kind=None):
        path = self.journal.paths()['jsonl']
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if kind is None or row.get('kind') == kind]

    def test_context_snapshot_and_explicit_clear_never_mutate_parent(self):
        with diagnostic_context(task_id='old', command_id='old-command', trace_id='trace'):
            snapshot = context_fields(); snapshot['task_id'] = 'changed'
            with diagnostic_context(task_id='new', command_id=None, password='not-allowed'):
                self.assertEqual(context_fields(), {'task_id':'new', 'trace_id':'trace'})
            self.assertEqual(context_fields()['task_id'], 'old')
        self.assertEqual(context_fields(), {})

    def test_replace_context_drops_unrelated_late_control_identity(self):
        with diagnostic_context(task_id='new',control_id='new-control',job_id='new-job'):
            with diagnostic_context(replace=True,task_id='old',trace_id='old-trace'):
                self.assertEqual(context_fields(),{'task_id':'old','trace_id':'old-trace'})
            self.assertEqual(context_fields()['control_id'],'new-control')

    def test_queued_work_keeps_original_identity_after_new_task_and_parent_exit(self):
        pool = WorkerPool('context-test', 1)
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(pool.close)
        def old():
            entered.set(); release.wait(2)
            diagnostic_event('AUDIT', 'old_finished')
        with diagnostic_context(task_id='old', command_id='old-command', trace_id='old-trace'):
            first = pool.submit(old)
        self.assertTrue(entered.wait(1))
        with diagnostic_context(task_id='new', command_id='new-command', trace_id='new-trace'):
            second = pool.submit(lambda: diagnostic_event('AUDIT', 'new_finished'))
        release.set(); first.result(2); second.result(2)
        rows = self.rows('AUDIT')
        self.assertEqual([(r['task_id'],r['command_id'],r['trace_id']) for r in rows],
                         [('old','old-command','old-trace'),('new','new-command','new-trace')])
        starts = [r for r in self.rows('WORKER') if r['event']=='started']
        self.assertEqual(len({r['job_id'] for r in starts}), 2)
        self.assertGreater(starts[1]['queue_wait_s'], 0)
        self.assertEqual(context_fields(), {})

    def test_ignored_future_is_reported_once_and_owner_stays_alive(self):
        owner = RuntimeOwner(); self.addCleanup(owner.close)
        done = threading.Event()
        def failed():
            try:
                raise RuntimeError('synthetic background fault')
            except RuntimeError as exc:
                report_exception(exc, component='inner')
                raise
        with diagnostic_context(task_id='old', command_id='original'):
            future = owner.submit(failed)
            future.add_done_callback(lambda _:done.set())
        self.assertTrue(done.wait(2))
        rows = self.rows('DIAGNOSTIC')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['task_id'], 'old')
        self.assertEqual(rows[0]['command_id'], 'original')
        self.assertIn('Traceback', rows[0]['exception'])
        self.assertIn('failed', rows[0]['exception'])
        self.assertTrue(owner.thread.is_alive())
        self.assertIsInstance(future.exception(), RuntimeError)

    def test_expected_rejection_does_not_create_error_diagnostic(self):
        owner = RuntimeOwner(); self.addCleanup(owner.close)
        def reject():raise Rejected('cancelled original wait')
        with self.assertRaises(Rejected):owner.call(reject)
        self.assertEqual(self.rows('DIAGNOSTIC'), [])

    def test_exception_formatting_failure_does_not_replace_background_error(self):
        class BadDetail(RuntimeError):
            def __str__(self):raise ValueError('detail formatting failed')
        owner=RuntimeOwner();self.addCleanup(owner.close)
        original=BadDetail()
        def fail():raise original
        with self.assertRaises(BadDetail) as caught:owner.call(fail)
        self.assertIs(caught.exception,original)
        self.assertEqual(self.rows('DIAGNOSTIC')[0]['message'],'Exception detail unavailable')

    def test_nested_control_context_is_restored_while_original_waits(self):
        owner = RuntimeOwner(); pool = WorkerPool('waiting',1)
        self.addCleanup(owner.close); self.addCleanup(pool.close)
        entered, release = threading.Event(), threading.Event()
        def blocked():entered.set(); release.wait(2)
        def original():
            owner.wait(pool.submit(blocked))
            diagnostic_event('AUDIT', 'original_resumed')
        with diagnostic_context(task_id='original',trace_id='old'):
            original_future = owner.submit(original)
        self.assertTrue(entered.wait(1))
        with diagnostic_context(task_id='control-target',trace_id='control'):
            owner.call(lambda:diagnostic_event('AUDIT','control'),control=True)
        release.set(); original_future.result(2)
        rows=self.rows('AUDIT')
        self.assertEqual([(r['event'],r['task_id']) for r in rows],
                         [('control','control-target'),('original_resumed','original')])

    def test_wait_summary_uses_original_job_identity(self):
        owner = RuntimeOwner(); pool = WorkerPool('slow',1)
        self.addCleanup(owner.close); self.addCleanup(pool.close)
        release = threading.Event()
        def wait_job():owner.wait(pool.submit(lambda:release.wait(.08)))
        with patch('brain.workers.WAIT_NOTICE_SEC',.01), diagnostic_context(task_id='waiting-task'):
            owner.call(wait_job)
        rows=[r for r in self.rows('WORKER') if r['event']=='waiting']
        self.assertTrue(rows)
        self.assertEqual(rows[0]['task_id'],'waiting-task')
        self.assertIn('waiting_job_id',rows[0])

    def model(self):
        model=ModelClient({'model': {'timeout_sec':7}})
        model.global_model=Mock(base_url='http://model.invalid/v1')
        model.model_name='offline-model'
        model.global_model.chat.completions.create.return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='private raw result'))])
        return model

    def test_model_start_and_completion_have_identity_without_prompt_dump(self):
        model=self.model()
        with diagnostic_context(task_id='task',trace_id='trace',call_id='parent'):
            self.assertEqual(model([{'role':'user','content':'private prompt'}]),'private raw result')
            self.assertEqual(context_fields()['call_id'],'parent')
        rows=self.rows('MODEL')
        self.assertEqual([r['event'] for r in rows],['started','completed'])
        self.assertEqual(rows[0]['call_id'],rows[1]['call_id'])
        self.assertNotEqual(rows[0]['call_id'],'parent')
        self.assertEqual(rows[0]['parent_call_id'],'parent')
        self.assertEqual(rows[0]['task_id'],'task')
        self.assertEqual(rows[0]['timeout_sec'],7)
        self.assertGreaterEqual(rows[1]['duration_s'],0)
        self.assertNotIn('private prompt',json.dumps(rows))
        self.assertNotIn('private raw result',json.dumps(rows))

    def test_model_failure_retains_traceback_and_error_identity(self):
        model=self.model()
        model.global_model.chat.completions.create.side_effect=ValueError('synthetic model failure')
        with diagnostic_context(task_id='task',trace_id='trace'), self.assertRaises(ValueError):
            model([{'role':'user','content':'private prompt'}])
        self.assertEqual([r['event'] for r in self.rows('MODEL')],['started','failed'])
        self.assertEqual(self.rows('DIAGNOSTIC')[0]['task_id'],'task')
        self.assertIn('Traceback',self.rows('DIAGNOSTIC')[0]['exception'])

    def test_reflection_snapshots_have_fresh_context_and_error_is_not_silent(self):
        from brain.learning.worker import ReflectionWorker
        scratch=tempfile.TemporaryDirectory();self.addCleanup(scratch.cleanup)
        seen=[];done=threading.Event();worker=None
        def process(episode):
            seen.append(context_fields())
            if len(seen)==2:
                worker.stop.set();done.set()
            if context_fields()['task_id']=='old':
                raise ValueError('synthetic reflection fault')
            return {'summary':'ok'}
        worker=ReflectionWorker(Path(scratch.name),{},processor=process)
        self.addCleanup(worker.close)
        for task in ('old','new'):
            worker.enqueue({'task_id':task,'revision':1,'state':'failed','package':'generic',
                            'phase':'step','open_command_id':task+'-command','steps':{}})
        with diagnostic_context(task_id='unrelated',trace_id='caller',control_id='caller-control'):
            worker.start()
        self.assertTrue(done.wait(2))
        self.assertTrue(worker.close())
        self.assertEqual({r['task_id'] for r in seen},{'old','new'})
        self.assertEqual(len({r['trace_id'] for r in seen}),2)
        self.assertTrue(all('control_id' not in r for r in seen))
        errors=self.rows('DIAGNOSTIC')
        self.assertEqual(len(errors),1)
        self.assertEqual(errors[0]['task_id'],'old')
        self.assertEqual(errors[0]['command_id'],'old-command')
        self.assertIn('Traceback',errors[0]['exception'])

    def test_ordinary_query_work_is_quiet_but_visible_in_health(self):
        pool=WorkerPool('poll-test',1);self.addCleanup(pool.close)
        entered,release=threading.Event(),threading.Event()
        def query():entered.set();release.wait(1)
        with diagnostic_context(task_id='poll-task',command_id='original'):
            future=pool.submit(query)
        self.assertTrue(entered.wait(1))
        active=pool.health()['active']
        self.assertEqual(active[0]['task_id'],'poll-task')
        self.assertEqual(active[0]['command_id'],'original')
        release.set();future.result(1)
        self.assertEqual(self.rows('WORKER'),[])

    def test_http_poll_errors_coalesce_then_recovery_is_visible_without_headers(self):
        http=HttpClient('http://offline.invalid')
        failure=HttpContractError('synthetic timeout')
        with diagnostic_context(task_id='task',command_id='cmd',trace_id='trace'):
            with patch.object(http,'_request_bytes',side_effect=failure):
                for _ in range(3):
                    with self.assertRaises(HttpContractError):http.request_bytes('GET','/original')
            with patch.object(http,'_request_bytes',return_value=(b'{}',200,{})) as wire:
                http.request_bytes('GET','/original')
                http.request_bytes('GET','/original')
                self.assertIsNone(wire.call_args.kwargs['headers'])
        rows=self.rows('HTTP')
        self.assertEqual([r['event'] for r in rows],['failed','completed'])
        self.assertEqual(rows[0]['command_id'],'cmd')
        self.assertNotEqual(rows[0]['call_id'],rows[1]['call_id'])

    def test_api_trace_is_request_local_and_does_not_change_control_body(self):
        from brain.api.app import create_app
        facade=Mock()
        facade.control.side_effect=lambda action,**kwargs: {'accepted':True,'task_id':kwargs['task_id']}
        client=create_app(Mock(),facade=facade).test_client()
        for task in ('first','second'):
            self.assertEqual(client.post('/api/task_pause',json={'task_id':task}).status_code,200)
            self.assertEqual(context_fields(),{})
        rows=[r for r in self.rows('REQUEST') if r['event']=='completed']
        self.assertEqual([r['task_id'] for r in rows],['first','second'])
        self.assertEqual(len({r['trace_id'] for r in rows}),2)
        self.assertEqual(facade.control.call_args.kwargs,{'task_id':'second'})

    def test_text_control_request_logs_target_not_new_submission_identity(self):
        from brain.api.app import create_app
        facade=Mock()
        facade.control.side_effect=lambda action,**kwargs:{'accepted':True,'task_id':kwargs['task_id']}
        client=create_app(Mock(),facade=facade).test_client()
        result=client.post('/publish_task',json={'task':'取消任务','task_id':'unused-request-id','target_task_id':'original-task'})
        self.assertEqual(result.status_code,200)
        rows=self.rows('REQUEST')
        self.assertEqual([r['task_id'] for r in rows],['original-task','original-task'])
        self.assertNotIn('unused-request-id',json.dumps(rows))

    def test_new_http_request_does_not_inherit_outer_request_or_parent_call(self):
        from brain.api.app import create_app
        facade=Mock()
        facade.control.side_effect=lambda action,**kwargs:{'accepted':True,'task_id':kwargs['task_id']}
        client=create_app(Mock(),facade=facade).test_client()
        with diagnostic_context(task_id='outer-task',request_id='outer-gate',parent_call_id='outer-call'):
            self.assertEqual(client.post('/api/task_pause',json={'task_id':'new-task'}).status_code,200)
            self.assertEqual(context_fields()['request_id'],'outer-gate')
        rows=self.rows('REQUEST')
        self.assertEqual({r['task_id'] for r in rows},{'new-task'})
        self.assertFalse(any('request_id' in r or 'parent_call_id' in r for r in rows))


if __name__=='__main__':unittest.main()
