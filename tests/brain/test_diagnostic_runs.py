"""Remote recording transport is bounded, idempotent and never task authority."""
from concurrent.futures import Future
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from brain.observability.diagnostic_runs import DiagnosticRuns, run_id_for_task
from brain.workers import RuntimeOwner
from contracts.tasks import Rejected
from contracts.diagnostic_reports import DiagnosticReportError, validate_report, validate_request


STAMP = '2026-11-02T16:00:00+08:00'
END = '2026-11-02T16:01:00+08:00'


class ManualPool:
    def __init__(self):self.jobs=[];self.closed=False;self.reject=False
    def submit(self, fn, *args):
        if self.closed or self.reject:raise RuntimeError('synthetic queue unavailable')
        future=Future();self.jobs.append((future,fn,args));return future
    def next(self):
        future,fn,args=self.jobs.pop(0)
        if future.set_running_or_notify_cancel():
            try:future.set_result(fn(*args))
            except BaseException as exc:future.set_exception(exc)
    def drain(self,limit=100):
        for _ in range(limit):
            if not self.jobs:return
            self.next()
        raise AssertionError('unexpected unbounded diagnostics chain')
    def health(self):return {'queued':len(self.jobs),'closing':self.closed}
    def close(self,timeout=1.):
        self.closed=True
        for future,_,_ in self.jobs:future.cancel()
        self.jobs.clear();return True


class FakeClient:
    def __init__(self):
        self.calls=[];self.reports={};self.failures={};self.poll_statuses=[]
        self.example=json.loads(Path('tests/contracts/robot_diagnostic_report.example.json').read_text())
    def reply(self,run_id,task_id):
        report=deepcopy(self.example)
        report.update(run_id=run_id,task_id=task_id,status='recording')
        report['quality']['analysis']['state']='pending'
        return report
    def call(self,action,run_id,body):
        self.calls.append((action,run_id,deepcopy(body)))
        errors=self.failures.get(action,[])
        if errors:
            error=errors.pop(0)
            if error:raise error
    def begin_run(self,payload):
        validate_request('begin',payload)
        run_id=payload['run_id'];task=payload['task_id']
        self.reports.setdefault(run_id,self.reply(run_id,task))
        self.call('begin',run_id,payload)
        return deepcopy(self.reports[run_id])
    def add_event(self,run_id,payload):
        validate_request('event',payload,run_id=run_id)
        self.call('event',run_id,payload)
        return dict(deepcopy(self.reports[run_id]),event_id=payload['event_id'])
    def finish_run(self,run_id,payload):
        validate_request('finish',payload,run_id=run_id)
        self.call('finish',run_id,payload)
        if run_id not in self.reports:
            raise DiagnosticReportError('NOT_FOUND','original run absent',status_code=404)
        report=self.reports[run_id]
        report['status']='finishing';report['window']['requested_end']=payload['ended_at']
        return deepcopy(report)
    def get_report(self,run_id,*,task_id):
        self.call('report',run_id,{'task_id':task_id})
        if run_id not in self.reports:
            raise DiagnosticReportError('NOT_FOUND','original run absent',status_code=404)
        result=deepcopy(self.reports[run_id])
        if self.poll_statuses:
            status,analysis=self.poll_statuses.pop(0)
            result['status']=status;result['quality']['analysis']['state']=analysis
            if status in {'complete','partial','interrupted'} and analysis=='finished':
                result['quality']['recorder_released']=True
        return result


def record(task='task',backend='reception_real',state='running',revision=1,command=None):
    return {'task_id':task,'execution_backend':backend,'state':state,'revision':revision,
            'updated_at':STAMP if revision<10 else END,'phase':'NAV',
            'open_command_id':command,'steps':{}}


class DiagnosticRunsTests(unittest.TestCase):
    def make(self,**settings):
        self.clock=0.;self.pool=ManualPool();self.client=FakeClient()
        self.runs=DiagnosticRuns({'brain':{'diagnostics':{'enabled':True,**settings}}},
            client=self.client,pool=self.pool,clock=lambda:self.clock,now=lambda:END)
        self.addCleanup(self.runs.close)
        return self.runs
    def start(self,task='task',backend='reception_real'):
        self.runs.event('task_opened',record(task,backend='unselected'))
        self.runs.event('execution_selected',record(task,backend=backend,revision=2))
        self.pool.drain()
    def advance(self,seconds=3):
        self.clock+=seconds;self.runs.tick();self.pool.drain()

    def test_physical_task_begin_event_finish_and_report_ready(self):
        self.make();self.start()
        source=record(state='succeeded',revision=10,command='original-command')
        before=json.dumps(source,sort_keys=True)
        self.client.poll_statuses=[('finishing','pending'),('complete','finished')]
        self.runs.event('task_succeeded',source);self.pool.drain();self.advance()
        self.assertEqual(json.dumps(source,sort_keys=True),before)
        calls=self.client.calls
        self.assertEqual(len([c for c in calls if c[0]=='begin']),1)
        finish=[c for c in calls if c[0]=='finish'][0]
        self.assertEqual(finish[2]['reason'],'completed')
        self.assertEqual(finish[2]['ended_at'],END)
        self.assertEqual(finish[2]['command_ids'],['original-command'])
        summary=self.runs.summary('task')
        self.assertEqual(summary['report']['status'],'complete')
        self.assertFalse(summary['refreshing'])

    def test_disabled_simulation_and_unselected_never_begin(self):
        self.make()
        for backend in ('unselected','desk','reception_simple','reception_mock'):
            task='task-'+backend
            self.runs.event('task_opened',record(task,backend=backend))
            self.runs.event('execution_selected',record(task,backend=backend,revision=2))
            self.runs.event('task_succeeded',record(task,backend=backend,state='succeeded',revision=10))
        self.pool.drain();self.assertEqual(self.client.calls,[])
        disabled=DiagnosticRuns({},client=self.client,pool=ManualPool())
        disabled.event('execution_selected',record());disabled.refresh('task');disabled.close()
        self.assertEqual(self.client.calls,[])

    def test_simulation_history_cannot_exhaust_physical_run_cache(self):
        self.make(max_cached_runs=2)
        for index in range(6):
            self.runs.event('task_opened',record('simulation-'+str(index),backend='unselected'))
            self.runs.event('execution_selected',record('simulation-'+str(index),backend='reception_simple',revision=2))
        self.start('physical')
        self.assertEqual(len([c for c in self.client.calls if c[0]=='begin']),1)
        self.assertLessEqual(self.runs.health()['cached_runs'],2)

    def test_hybrid_scope_is_metadata_not_motion_evidence(self):
        self.make();self.start(backend='reception_sim_navigation_real')
        event=next(c[2] for c in self.client.calls if c[0]=='event')
        self.assertFalse(event['summary']['navigation_physical'])
        self.assertTrue(event['summary']['manipulation_physical'])
        self.assertIn('not_motion_or_success',event['summary']['scope'])

    def test_begin_timeout_retry_has_same_id_body_and_start_time(self):
        self.make()
        self.client.failures['begin']=[DiagnosticReportError('UNAVAILABLE','synthetic timeout')]
        self.start();self.advance()
        starts=[c for c in self.client.calls if c[0]=='begin']
        self.assertEqual(len(starts),2)
        self.assertEqual(starts[0],starts[1])
        self.assertEqual(starts[0][2]['started_at'],STAMP)

    def test_finish_timeout_retry_preserves_body_and_end_time(self):
        self.make();self.start()
        self.client.failures['finish']=[DiagnosticReportError('UNAVAILABLE','synthetic timeout')]
        self.runs.event('task_cancelled',record(state='cancelled',revision=10))
        self.pool.drain();self.advance()
        ends=[c for c in self.client.calls if c[0]=='finish']
        self.assertEqual(len(ends),2);self.assertEqual(ends[0],ends[1])

    def test_queue_rejected_begin_is_never_started_after_task_end_or_capture_timeout(self):
        for state in ('succeeded','cancelled','failed','capture_timeout'):
            with self.subTest(state=state):
                self.make(max_capture_duration_sec=10)
                self.pool.reject=True
                self.runs.event('execution_selected',record(revision=2))
                if state=='capture_timeout':self.advance(11)
                else:self.runs.event('task_ended',record(state=state,revision=10))
                self.pool.reject=False;self.advance(5)
                self.assertEqual(self.client.calls,[])
                self.assertEqual(self.runs.summary('task')['tracking'],'not_recorded')
                self.assertIn('capture_ended_before_begin_delivery',self.runs.summary('task')['incomplete_reasons'])

    def test_queued_begin_is_cancelled_before_worker_sends_after_terminal(self):
        self.make();self.runs.event('execution_selected',record(revision=2))
        self.assertEqual(len(self.pool.jobs),1)
        queued=self.pool.jobs[0][0]
        self.runs.event('task_cancelled',record(state='cancelled',revision=10))
        self.assertTrue(queued.cancelled())
        self.pool.drain();self.advance(5)
        self.assertEqual(self.client.calls,[])

    def test_running_future_without_send_is_rechecked_at_client_boundary(self):
        self.make();self.runs.event('execution_selected',record(revision=2))
        future,fn,args=self.pool.jobs.pop(0)
        self.assertTrue(future.set_running_or_notify_cancel())
        self.runs.event('task_succeeded',record(state='succeeded',revision=10))
        self.assertFalse(future.cancelled())
        future.set_result(fn(*args));self.advance(5)
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.runs.summary('task')['tracking'],'not_recorded')

    def test_actually_attempted_unknown_begin_keeps_original_finish_without_rebegin(self):
        for code in ('UNAVAILABLE','INVALID_REPORT'):
            with self.subTest(code=code):
                self.make()
                self.client.failures['begin']=[DiagnosticReportError(code,'response not confirmed')]
                self.runs.event('execution_selected',record(revision=2));self.pool.next()
                self.assertEqual(self.client.calls[0][0],'begin')
                self.runs.event('task_cancelled',record(state='cancelled',revision=10))
                self.advance(3)
                self.assertEqual(len([c for c in self.client.calls if c[0]=='begin']),1)
                self.assertEqual(len([c for c in self.client.calls if c[0]=='finish']),1)
                self.assertTrue(all(c[1]==run_id_for_task('task') for c in self.client.calls))

    def test_recovery_then_resume_does_not_close_or_create_second_run(self):
        self.make();self.start()
        self.runs.event('verdict',record(state='recovery_required',revision=3));self.pool.drain()
        self.runs.event('human_continue',record(state='running',revision=4));self.pool.drain()
        self.assertEqual(len([c for c in self.client.calls if c[0]=='begin']),1)
        self.assertEqual([c for c in self.client.calls if c[0]=='finish'],[])

    def test_restart_and_refresh_only_query_deterministic_original_run(self):
        self.make()
        self.runs.event('task_restored',record(state='recovery_required'))
        self.pool.drain()
        self.runs.event('human_continue',record(revision=3));self.pool.drain()
        self.runs.refresh('task');self.pool.drain()
        self.assertTrue(self.client.calls)
        self.assertTrue(all(c[0]=='report' for c in self.client.calls))
        self.assertTrue(all(c[1]==run_id_for_task('task') for c in self.client.calls))
        self.assertEqual(self.runs.summary('task')['error']['code'],'NOT_FOUND')
        self.advance(100)
        self.assertEqual(len(self.client.calls),2)

    def test_summary_and_health_do_not_send_requests(self):
        self.make();self.start()
        before=len(self.client.calls)
        for _ in range(20):self.runs.summary('task');self.runs.health()
        self.assertEqual(len(self.client.calls),before)

    def test_superseded_finish_targets_old_run_and_late_selection_ignored(self):
        self.make();self.start('old')
        self.client.poll_statuses=[('complete','finished')]
        self.runs.event('task_opened',record('new',backend='unselected'))
        self.runs.event('execution_selected',record('new',revision=2))
        self.runs.event('execution_selected',record('old',revision=3))
        self.pool.drain();self.advance()
        old_finish=next(c for c in self.client.calls if c[0]=='finish')
        new_begin=next(c for c in self.client.calls if c[0]=='begin' and c[2]['task_id']=='new')
        self.assertEqual(old_finish[1],run_id_for_task('old'))
        self.assertEqual(old_finish[2]['reason'],'superseded')
        self.assertLess(self.client.calls.index(old_finish),self.client.calls.index(new_begin))
        self.assertEqual(self.runs.summary('new')['report']['task_id'],'new')
        self.assertIn('superseded',self.runs.summary('old')['incomplete_reasons'])

    def test_capture_deadline_closes_diagnostics_without_task_terminal(self):
        self.make(max_capture_duration_sec=10);self.start();self.advance(11)
        finish=next(c for c in self.client.calls if c[0]=='finish')
        self.assertEqual(finish[2]['reason'],'capture_timeout')
        self.runs.event('human_continue',record(revision=3));self.pool.drain()
        self.assertEqual(len([c for c in self.client.calls if c[0]=='begin']),1)
        self.assertIn('capture_timeout',self.runs.summary('task')['incomplete_reasons'])

    def test_nullable_report_summary_does_not_block_successor_capture(self):
        self.make()
        self.client.example['quality']['external_summaries'] = [{
            'source': 'brain', 'event_id': 'task-opened', 'at': STAMP,
            'verification': 'source_declared', 'summary': {'event': 'task_opened'},
            'command_id': None, 'source_instance_id': None}]
        for name in ('begin_run', 'add_event', 'finish_run', 'get_report'):
            original = getattr(self.client, name)
            def checked(*args, _original=original, **kwargs):
                reply = _original(*args, **kwargs)
                return validate_report(reply, run_id=reply['run_id'], task_id=reply['task_id'],
                                       event_id=reply.get('event_id'))
            setattr(self.client, name, checked)
        self.start('old')
        self.client.poll_statuses = [('partial', 'finished')]
        self.runs.event('task_opened', record('new', backend='unselected'))
        self.runs.event('execution_selected', record('new', revision=2))
        self.pool.drain()
        self.advance()
        old = self.runs.summary('old')
        new = self.runs.summary('new')
        self.assertTrue(old['report']['quality']['recorder_released'])
        self.assertEqual(new['report']['task_id'], 'new')
        self.assertIsNone(new['error'])
        self.assertEqual(len([c for c in self.client.calls if c[0] == 'begin'
                              and c[2]['task_id'] == 'new']), 1)

    def test_queue_overflow_and_transport_limit_are_explicit(self):
        self.make(event_queue_limit=2,max_attempts=2)
        self.runs.event('task_opened',record(backend='unselected'))
        self.runs.event('execution_selected',record(revision=2))
        for rev in range(3,8):self.runs.event('action_finished',record(revision=rev))
        self.pool.drain()
        self.assertGreater(self.runs.summary('task')['dropped_events'],0)
        self.assertIn('event_queue_overflow',self.runs.summary('task')['incomplete_reasons'])
        self.client.failures['finish']=[DiagnosticReportError('UNAVAILABLE','timeout')]*5
        self.runs.event('task_cancelled',record(state='cancelled',revision=10));self.pool.drain()
        for _ in range(5):self.advance()
        self.assertEqual(len([c for c in self.client.calls if c[0]=='finish']),2)
        self.assertIn('finish_delivery_unconfirmed',self.runs.summary('task')['incomplete_reasons'])

    def test_conflict_is_not_retried_or_replaced_by_new_run(self):
        self.make();self.client.failures['begin']=[DiagnosticReportError('CONFLICT','busy',status_code=409)]
        self.start()
        for _ in range(5):self.advance()
        self.assertEqual(len([c for c in self.client.calls if c[0]=='begin']),1)
        self.assertEqual(self.runs.summary('task')['error']['code'],'CONFLICT')

    def test_explicit_busy_can_retry_same_begin_within_bounded_start_budget(self):
        self.make(start_wait_timeout_sec=8,max_attempts=1)
        self.client.failures['begin']=[DiagnosticReportError('BUSY','previous recorder finishing',status_code=409),None]
        self.start();self.advance(3)
        starts=[c for c in self.client.calls if c[0]=='begin']
        self.assertEqual(len(starts),2);self.assertEqual(starts[0],starts[1])
        self.assertIn('delayed_start',self.runs.summary('task')['incomplete_reasons'])
        self.assertEqual(self.runs.summary('task')['tracking'],'active')

    def test_busy_timeout_does_not_retry_forever_or_stop_other_capture(self):
        self.make(start_wait_timeout_sec=5)
        self.client.failures['begin']=[DiagnosticReportError('BUSY','foreign recorder busy',status_code=409)]*10
        self.start()
        for _ in range(8):self.advance(3)
        self.assertEqual(len([c for c in self.client.calls if c[0]=='begin']),3)
        self.assertEqual([c for c in self.client.calls if c[0]=='finish'],[])
        self.assertIn('recorder_busy_timeout',self.runs.summary('task')['incomplete_reasons'])

    def test_readonly_probe_before_task_creation_does_not_disable_new_task_capture(self):
        self.make();self.runs.refresh('task');self.pool.drain()
        self.assertEqual(self.runs.summary('task')['tracking'],'query_only')
        self.start()
        self.assertEqual(len([c for c in self.client.calls if c[0]=='begin']),1)

    def test_recording_end_report_is_not_ready_until_analysis_finished(self):
        self.make();self.start()
        self.client.poll_statuses=[('partial','running'),('partial','finished')]
        self.runs.event('task_succeeded',record(state='succeeded',revision=10));self.pool.drain()
        self.assertTrue(self.runs.summary('task')['refreshing'])
        self.advance()
        self.assertFalse(self.runs.summary('task')['refreshing'])
        self.assertEqual(self.runs.summary('task')['report']['status'],'partial')

    def test_report_pending_polling_is_bounded_and_keeps_partial_evidence(self):
        self.make(max_report_polls=2);self.start()
        self.runs.event('task_succeeded',record(state='succeeded',revision=10));self.pool.drain()
        for _ in range(5):self.advance()
        self.assertIn('report_poll_limit',self.runs.summary('task')['incomplete_reasons'])
        self.assertIsNotNone(self.runs.summary('task')['report'])
        self.assertFalse(self.runs.summary('task')['refreshing'])

    def test_saved_request_ids_are_projected_without_changing_ledger(self):
        self.make()
        saved={'source_gate':{'operations':{'old':{'diagnostic_identity':{'task_id':'other'},'request':{'request_id':'foreign'}},
            'ours':{'diagnostic_identity':{'task_id':'task'},'request':{'request_id':'original-request'}}}}}
        before=json.dumps(saved,sort_keys=True);self.runs.control_records=lambda:saved
        self.start();self.runs.event('task_cancelled',record(state='cancelled',revision=10));self.pool.drain()
        events=[c[2] for c in self.client.calls if c[0]=='event']
        self.assertTrue(any(e.get('request_id')=='original-request' for e in events))
        self.assertNotIn('foreign',json.dumps(self.client.calls))
        self.assertEqual(json.dumps(saved,sort_keys=True),before)
        self.assertIn('original-request',next(c[2] for c in self.client.calls if c[0]=='finish')['request_ids'])

    def test_rejected_peer_identities_never_enter_authoritative_index(self):
        self.make();self.start()
        row=record(state='failed',revision=10,command='original')
        row['steps']={'NAV':{'attempts':[{'task_id':'task','command_id':'original','progress':{'evidence':{'downstream':{
            'task_id':'foreign','command_id':'foreign-command','request_id':'foreign-request',
            'diagnostic_summary':{'schema':'fq/peer-diagnostic-summary/v1','task_id':'foreign',
            'command_id':'foreign-command','state_authority':False,'findings':[]}}}}}]}}
        self.runs.event('verdict',row);self.pool.drain()
        self.assertNotIn('foreign-command',json.dumps(self.client.calls))
        self.assertNotIn('foreign-request',json.dumps(self.client.calls))
        finish=next(c[2] for c in self.client.calls if c[0]=='finish')
        self.assertEqual(finish['command_ids'],['original']);self.assertEqual(finish['request_ids'],[])

    def test_gateway_request_leading_punctuation_is_preserved(self):
        self.make();self.runs.control_records=lambda:{'operations':{'ours':{
            'diagnostic_identity':{'task_id':'task'},'request':{'request_id':'.original-request'}}}}
        self.start()
        self.assertTrue(any(c[0]=='event' and c[2].get('request_id')=='.original-request' for c in self.client.calls))

    def test_peer_summary_projects_bounded_metadata_and_reference_only(self):
        self.make();self.start()
        row=record(revision=3,command='nav-original')
        row['steps']={'NAV':{'attempts':[{'command_id':'nav-original','progress':{'evidence':{'downstream':{
            'task_id':'task','command_id':'nav-original','private_internal_body':'must-not-upload','diagnostic_summary':{'schema':'fq/peer-diagnostic-summary/v1',
            'task_id':'task','command_id':'nav-original','state_authority':False,'peer':'nav','generated_at':STAMP,'status':'partial',
            'readiness':False,'quality':{'missing_evidence':['body_stop']},
            'evidence_refs':[{'reference':'peer://nav/original/log'}]}}}}}]}}
        self.runs.event('verdict',row);self.pool.drain()
        bodies=[c[2] for c in self.client.calls if c[0]=='event']
        evidence=[b for b in bodies if b['kind']=='evidence']
        self.assertTrue(any(e.get('evidence_ref')=='peer://nav/original/log' for e in evidence))
        self.assertTrue(any(e.get('summary',{}).get('peer_readiness') is False for e in evidence))
        self.assertNotIn('must-not-upload',json.dumps(bodies))

    def test_untrusted_or_wrong_peer_summary_is_not_forwarded(self):
        self.make();self.start()
        for rev,identity in enumerate(({'peer':'vla','state_authority':False},
                                       {'peer':'nav','state_authority':True},
                                       {'peer':'nav','state_authority':False,'task_id':'other'}),3):
            row=record(revision=rev,command='original')
            summary={'schema':'fq/peer-diagnostic-summary/v1','task_id':'task','command_id':'original',
                     'quality':{},'findings':[{'code':'UNTRUSTED','classification':'confirmed'}],**identity}
            row['steps']={'NAV':{'attempts':[{'command_id':'original','request':{'skill':'navigate'},
                'progress':{'evidence':{'downstream':{'task_id':'task','command_id':'original','diagnostic_summary':summary}}}}]}}
            self.runs.event('verdict',row);self.pool.drain()
        self.assertNotIn('UNTRUSTED',json.dumps(self.client.calls))

    def test_native_classification_alias_and_missing_fields_are_preserved(self):
        self.make();self.start()
        row=record(revision=3,command='vla-original')
        row['steps']={'VLA':{'attempts':[{'command_id':'vla-original','request':{'skill':'open_fridge'},
            'progress':{'evidence':{'downstream':{'task_id':'task','command_id':'vla-original','operation':'open_fridge',
                'diagnostic_summary':{'schema':'fq/peer-diagnostic-summary/v1','task_id':'task','command_id':'vla-original',
                    'peer':'vla','state_authority':False,'generated_at':STAMP,
                    'quality':{'status':'partial','missing':['remote_trace']},
                    'findings':[{'code':'EXECUTION_STOP_UNCONFIRMED','classification':'unknown','message':'source declares uncertain stop'}]}}}}}]}}
        self.runs.event('verdict',row);self.pool.drain()
        events=[c[2] for c in self.client.calls if c[0]=='event']
        self.assertTrue(any(e.get('summary',{}).get('missing_count')==1 for e in events))
        self.assertTrue(any(e.get('summary',{}).get('finding_code')=='EXECUTION_STOP_UNCONFIRMED'
                            and e['summary']['assessment']=='unknown' for e in events))

    def test_actual_vla_fixture_refs_and_hash_updates_survive_projection(self):
        self.make();self.start('synthetic-task')
        native=json.loads(Path('tests/contracts/vla_peer_diagnostic_summary.example.json').read_text())['diagnostic_summary']
        before=json.dumps(native,sort_keys=True)
        row=record('synthetic-task',revision=3,command='synthetic-command')
        row['steps']={'VLA':{'attempts':[{'command_id':'synthetic-command','request':{'skill':'open_fridge'},
            'progress':{'evidence':{'downstream':{'task_id':'synthetic-task','command_id':'synthetic-command',
                'operation':'open_fridge','diagnostic_summary':native,'raw_joint_array':[1,2,3]}}}}]}}
        self.runs.event('verdict',row);self.pool.drain()
        events=[c[2] for c in self.client.calls if c[0]=='event']
        refs=[e for e in events if 'evidence_ref' in e]
        self.assertEqual(len(refs),2)
        self.assertEqual(refs[0]['evidence_ref'],native['evidence_refs'][0]['root']+':'+native['evidence_refs'][0]['relative_path'])
        self.assertEqual(refs[0]['summary']['sha256'],native['evidence_refs'][0]['sha256'])
        self.assertEqual(refs[0]['summary']['hash_scope'],'written_prefix')
        self.assertEqual(refs[0]['summary']['scope'],'source_declared_not_fetched')
        self.assertTrue(any(e.get('summary',{}).get('peer_status')=='ready'
                            and e['summary']['quality_status']=='partial' for e in events))
        self.assertTrue(any(e.get('summary',{}).get('assessment')=='unknown' for e in events))
        self.assertNotIn('raw_joint_array',json.dumps(events))
        self.assertEqual(json.dumps(native,sort_keys=True),before)
        row['revision']=4
        native['evidence_refs'][0]['sha256']='a'*64
        native['evidence_refs'][0]['bytes']+=1
        self.runs.event('verdict',row);self.pool.drain()
        refs=[c[2] for c in self.client.calls if c[0]=='event' and 'evidence_ref' in c[2]]
        self.assertEqual(len(refs),3)
        self.assertEqual(refs[-1]['summary']['sha256'],'a'*64)

    def test_actual_nav_fixture_pending_to_sealed_maps_confidence_and_inert_refs(self):
        self.make()
        native=json.loads(Path('tests/contracts/nav_peer_diagnostic_summary.example.json').read_text())['diagnostic_summary']
        pending=deepcopy(native);pending.update(archive_state='pending',generated_at=None,evidence_refs=[],findings=[])
        reads=[]
        self.runs.peer_summary_reader=lambda *args:reads.append(args) or deepcopy(native)
        opened=record('synthetic-task',backend='unselected')
        selected=record('synthetic-task',revision=2)
        selected['execution_target']={'dream':'http://nav.original.invalid'}
        self.runs.event('task_opened',opened);self.runs.event('execution_selected',selected);self.pool.drain()
        terminal=record('synthetic-task',state='failed',revision=10,command='synthetic-command')
        terminal['steps']={'NAV':{'attempts':[{'command_id':'synthetic-command','request':{'skill':'navigate'},
            'progress':{'evidence':{'downstream':{'task_id':'synthetic-task','command_id':'synthetic-command',
                'diagnostic_summary':pending}}}}]}}
        before=json.dumps(terminal,sort_keys=True)
        self.runs.event('verdict',terminal);self.pool.drain()
        self.assertEqual(reads,[('nav','synthetic-task','synthetic-command','http://nav.original.invalid')])
        self.runs.tick();self.pool.drain()
        events=[c[2] for c in self.client.calls if c[0]=='event']
        refs=[e for e in events if 'evidence_ref' in e]
        self.assertEqual(len(refs),1)
        ref=native['evidence_refs'][0]
        self.assertEqual(refs[0]['evidence_ref'],ref['ref_id'])
        self.assertEqual(refs[0]['summary']['relative_path'],ref['relative_path'])
        self.assertEqual(refs[0]['summary']['sha256'],ref['sha256'])
        self.assertEqual(refs[0]['summary']['scope'],'source_declared_not_fetched')
        self.assertEqual(refs[0]['summary']['peer_ref_scope'],ref['scope'])
        self.assertNotIn('root',refs[0]['summary'])
        self.assertTrue(any(e.get('summary',{}).get('peer_archive_state')=='pending' for e in events))
        self.assertTrue(any(e.get('summary',{}).get('peer_archive_state')=='snapshot_sealed' for e in events))
        self.assertTrue(any(e.get('summary',{}).get('finding_code')=='RETAINED_NAV_EVIDENCE'
                            and e['summary']['assessment']=='confirmed' for e in events))
        self.assertEqual(self.runs.summary('synthetic-task')['pending_peer_indices'],0)
        self.assertEqual(json.dumps(terminal,sort_keys=True),before)

    def test_nav_relative_reference_traversal_and_conflicting_confidence_are_rejected(self):
        self.make();self.start()
        native=json.loads(Path('tests/contracts/nav_peer_diagnostic_summary.example.json').read_text())['diagnostic_summary']
        native.update(task_id='task',command_id='original')
        native['evidence_refs'][0]['relative_path']='../private/config'
        native['findings'][0]['assessment']='unknown'
        row=record(revision=3,command='original')
        row['steps']={'NAV':{'attempts':[{'command_id':'original','request':{'skill':'navigate'},
            'progress':{'evidence':{'downstream':{'task_id':'task','command_id':'original','diagnostic_summary':native}}}}]}}
        self.runs.event('verdict',row);self.pool.drain()
        events=[c[2] for c in self.client.calls if c[0]=='event']
        self.assertFalse(any('evidence_ref' in e for e in events))
        self.assertFalse(any(e.get('summary',{}).get('finding_code')=='RETAINED_NAV_EVIDENCE' for e in events))
        reasons=self.runs.summary('task')['incomplete_reasons']
        self.assertIn('peer_evidence_reference_unavailable',reasons)
        self.assertIn('peer_finding_classification_conflict',reasons)

    def test_finish_precedes_queued_metadata_and_late_events_keep_original_run(self):
        self.make();self.start()
        before=len(self.client.calls)
        for rev in range(3,8):self.runs.event('action_finished',record(revision=rev,command='original'))
        self.runs.event('task_succeeded',record(state='succeeded',revision=10,command='original'))
        self.pool.drain()
        actions=[c[0] for c in self.client.calls[before:]]
        self.assertLessEqual(actions.index('finish'),1)  # One already-admitted metadata request may finish first.
        self.assertTrue(all(c[1]==run_id_for_task('task') for c in self.client.calls))
        self.assertGreater(actions.count('event'),0)

    def test_pending_native_index_settles_after_terminal_without_business_mutation(self):
        self.make()
        native=json.loads(Path('tests/contracts/vla_peer_diagnostic_summary.example.json').read_text())['diagnostic_summary']
        pending=deepcopy(native);pending.update(status='pending',collection_state='open',evidence_refs=[],findings=[])
        reads=[]
        def reader(peer,task_id,command_id,target):
            reads.append((peer,task_id,command_id,target))
            return deepcopy(pending if len(reads)==1 else native)
        self.runs.peer_summary_reader=reader
        opened=record('synthetic-task',backend='unselected')
        selected=record('synthetic-task',revision=2)
        selected['execution_target']={'vla':'http://vla.original.invalid'}
        self.runs.event('task_opened',opened);self.runs.event('execution_selected',selected);self.pool.drain()
        terminal=record('synthetic-task',state='failed',revision=10,command='synthetic-command')
        terminal['steps']={'VLA':{'attempts':[{'command_id':'synthetic-command','request':{'skill':'open_fridge'},
            'progress':{'evidence':{'downstream':{'task_id':'synthetic-task','command_id':'synthetic-command',
                'operation':'open_fridge','diagnostic_summary':pending}}}}]}}
        before=json.dumps(terminal,sort_keys=True)
        self.runs.event('verdict',terminal);self.pool.drain()
        self.assertEqual(len(reads),1)
        self.assertIn('peer_final_index_pending',self.runs.summary('synthetic-task')['incomplete_reasons'])
        calls_before=len(self.client.calls)
        self.runs.refresh('synthetic-task');self.pool.drain()
        self.assertTrue(all(c[0]=='report' for c in self.client.calls[calls_before:]))
        self.assertEqual(len(reads),1)
        self.advance(11);self.runs.tick();self.pool.drain()
        self.assertEqual(len(reads),2)
        self.assertEqual(reads[1],('vla','synthetic-task','synthetic-command','http://vla.original.invalid'))
        refs=[(index,c) for index,c in enumerate(self.client.calls) if c[0]=='event' and 'evidence_ref' in c[2]]
        self.assertEqual(len(refs),2)
        finish_index=next(i for i,c in enumerate(self.client.calls) if c[0]=='finish')
        self.assertTrue(all(i>finish_index for i,_ in refs))
        self.assertEqual(len([c for c in self.client.calls if c[0]=='begin']),1)
        self.assertEqual(len([c for c in self.client.calls if c[0]=='finish']),1)
        self.assertEqual(self.runs.summary('synthetic-task')['pending_peer_indices'],0)
        self.assertEqual(json.dumps(terminal,sort_keys=True),before)

    def test_native_pending_budget_does_not_poll_forever(self):
        self.make()
        reads=[]
        pending={'schema':'fq/peer-diagnostic-summary/v1','task_id':'task','command_id':'original',
                 'peer':'nav','state_authority':False,'status':'pending','collection_state':'open','quality':{}}
        self.runs.peer_summary_reader=lambda *args:reads.append(args) or deepcopy(pending)
        row=record(revision=2);row['execution_target']={'dream':'http://nav.original.invalid'}
        self.runs.event('execution_selected',row);self.pool.drain()
        terminal=record(state='failed',revision=10,command='original')
        terminal['steps']={'NAV':{'attempts':[{'command_id':'original','request':{'skill':'navigate'},
            'progress':{'evidence':{'downstream':{'task_id':'task','command_id':'original','diagnostic_summary':pending}}}}]}}
        self.runs.event('verdict',terminal);self.pool.drain()
        for _ in range(12):self.advance(11)
        self.assertEqual(len(reads),4)
        self.assertEqual(self.runs.summary('task')['pending_peer_indices'],0)
        self.assertTrue(any(reason.startswith('peer_final_index_unavailable:') for reason in self.runs.summary('task')['incomplete_reasons']))

    def _nav_recovery(self, command='original', revision=3):
        native=deepcopy(self._sealed_nav)
        native.update(task_id='task',command_id=command,archive_state='pending',generated_at=None,findings=[],evidence_refs=[])
        row=record(state='recovery_required',revision=revision,command=command)
        row['steps']={'NAV':{'attempts':[{'task_id':'task','command_id':command,'outcome':'failed',
            'request':{'skill':'navigate'},'progress':{'terminal':'failed','evidence':{'downstream':{
                'task_id':'task','command_id':command,'state':'failed','diagnostic_summary':native}}}}]}}
        return row

    def _start_nav_reader(self, reader):
        self._sealed_nav=json.loads(Path('tests/contracts/nav_peer_diagnostic_summary.example.json').read_text())['diagnostic_summary']
        self.runs.peer_summary_reader=reader
        row=record(revision=2);row['execution_target']={'dream':'http://nav.original.invalid'}
        self.runs.event('execution_selected',row);self.pool.drain()

    def test_command_failure_in_recovery_settles_archive_without_finishing_capture(self):
        self.make();reads=[]
        def reader(peer,task,command,target):
            reads.append((peer,task,command,target))
            final=deepcopy(self._sealed_nav);final.update(task_id=task,command_id=command);return final
        self._start_nav_reader(reader)
        recovery=self._nav_recovery();before=json.dumps(recovery,sort_keys=True)
        self.runs.event('verdict',recovery);self.pool.drain();self.runs.tick();self.pool.drain()
        self.assertEqual(len(reads),1)
        self.assertEqual([c for c in self.client.calls if c[0]=='finish'],[])
        self.assertEqual(self.runs.summary('task')['tracking'],'active')
        self.assertEqual(self.runs.summary('task')['pending_peer_indices'],0)
        self.assertEqual(json.dumps(recovery,sort_keys=True),before)
        self.assertTrue(any(c[0]=='event' and c[2].get('summary',{}).get('peer_archive_state')=='snapshot_sealed'
                            for c in self.client.calls))
        # A late reflection of the old pending Runtime snapshot cannot regress it.
        before_calls=len(self.client.calls);recovery['revision']=4
        self.runs.event('reflection_completed',recovery);self.pool.drain();self.advance(121)
        self.assertEqual(len(reads),1)
        self.assertFalse(any(c[0]=='event' and c[2].get('summary',{}).get('peer_archive_state')=='pending'
                             for c in self.client.calls[before_calls:]))

    def test_per_command_window_does_not_reopen_and_later_command_has_own_budget(self):
        self.make();reads=[]
        def reader(peer,task,command,target):
            reads.append((command,self.clock))
            if command=='later':
                final=deepcopy(self._sealed_nav);final.update(task_id=task,command_id=command);return final
            return self._nav_recovery(command)['steps']['NAV']['attempts'][0]['progress']['evidence']['downstream']['diagnostic_summary']
        self._start_nav_reader(reader)
        old=self._nav_recovery()
        self.runs.event('verdict',old);self.pool.drain()
        for at in (10,40,105):
            old['revision']+=1;self.runs.event('recovery_advised',old)
            self.advance(at-self.clock)
        self.assertEqual(reads,[('original',0.),('original',10.),('original',40.),('original',105.)])
        self.advance(25)
        for rev in range(20,25):
            old['revision']=rev;self.runs.event('recovery_advised',old);self.pool.drain()
        self.assertEqual(len(reads),4)
        self.assertEqual(self.runs.summary('task')['pending_peer_indices'],0)
        self.runs.event('verdict',self._nav_recovery('later',revision=30));self.pool.drain()
        self.assertEqual(reads[-1],('later',130.))
        self.assertEqual([c for c in self.client.calls if c[0]=='finish'],[])

    def test_nav_archive_after_ninety_five_seconds_is_collected_on_final_budgeted_read(self):
        self.make();reads=[]
        def reader(peer,task,command,target):
            reads.append(self.clock)
            if self.clock>=95:
                final=deepcopy(self._sealed_nav);final.update(task_id=task,command_id=command);return final
            return self._nav_recovery(command)['steps']['NAV']['attempts'][0]['progress']['evidence']['downstream']['diagnostic_summary']
        self._start_nav_reader(reader)
        self.runs.event('verdict',self._nav_recovery());self.pool.drain()
        for at in (10,40,95,105):self.advance(at-self.clock)
        self.runs.tick();self.pool.drain()
        self.assertEqual(reads,[0.,10.,40.,105.])
        self.assertEqual(self.runs.summary('task')['pending_peer_indices'],0)
        self.assertFalse(any('unavailable' in reason or 'timeout' in reason for reason in self.runs.summary('task')['incomplete_reasons']))

    def test_queued_peer_read_after_120_second_budget_is_skipped_and_gap_visible(self):
        self.make();reads=[]
        self._start_nav_reader(lambda *args:reads.append(args))
        self.runs.event('verdict',self._nav_recovery())
        self.clock=121.;self.pool.drain();self.runs.tick();self.pool.drain()
        self.assertEqual(reads,[])
        self.assertEqual(self.runs.summary('task')['pending_peer_indices'],0)
        self.assertTrue(any(reason.startswith('peer_final_index_settlement_timeout:')
                            for reason in self.runs.summary('task')['incomplete_reasons']))

    def test_missing_or_failed_optional_hook_never_blocks_cancel_projection(self):
        from brain.application import BrainApplication
        from unittest.mock import Mock
        app=BrainApplication.__new__(BrainApplication)
        app._advice_pending=None;app._execution_ports=[];app.owner=Mock();app.body_control=Mock()
        app.config={'brain':{'capture_timeline':True}};app.media=Mock()
        saved={'task_id':'original','state':'cancelled'}
        with patch('brain.application.emit') as emit:
            app._event('task_cancelled',saved)
            app.diagnostic_runs=Mock()
            app.diagnostic_runs.event.side_effect=RuntimeError('synthetic diagnostics unavailable')
            app._event('task_cancelled',saved)
        self.assertEqual(app.body_control.cancel_task.call_count,2)
        self.assertEqual(app.media.event.call_count,2)
        self.assertEqual(emit.call_count,2)

    def test_wait_hook_progresses_transport_retry_and_cancel_stays_responsive(self):
        self.make()
        self.client.failures['begin']=[DiagnosticReportError('UNAVAILABLE','synthetic timeout')]
        self.start()
        owner=RuntimeOwner();self.addCleanup(owner.close)
        waiting=Future();progress=threading.Event()
        def tick():
            self.clock+=3;self.runs.tick();self.pool.drain()
            if len([c for c in self.client.calls if c[0]=='begin'])==2:progress.set()
        owner.on_wait_tick=tick
        original=owner.submit(lambda:owner.wait(waiting))
        self.assertTrue(progress.wait(1))
        started=time.monotonic();owner.call(owner.interrupt_waits,control=True)
        with self.assertRaises(Rejected):original.result(1)
        self.assertLess(time.monotonic()-started,.2)

    def test_wait_hook_exception_never_replaces_business_future_result(self):
        owner=RuntimeOwner();self.addCleanup(owner.close)
        waiting=Future();entered=threading.Event()
        def broken_tick():entered.set();raise ValueError('synthetic diagnostic hook failure')
        owner.on_wait_tick=broken_tick
        with patch('brain.workers.report_exception') as diagnostic:
            original=owner.submit(lambda:owner.wait(waiting))
            self.assertTrue(entered.wait(1));waiting.set_result('business-result')
            self.assertEqual(original.result(1),'business-result')
            self.assertEqual(diagnostic.call_count,1)

    def test_blocked_remote_begin_does_not_block_events_summary_or_close(self):
        client=FakeClient();entered=threading.Event();release=threading.Event()
        original=client.begin_run
        def blocked(body):entered.set();release.wait(2);return original(body)
        client.begin_run=blocked
        runs=DiagnosticRuns({'brain':{'diagnostics':{'enabled':True}}},client=client)
        try:
            start=time.monotonic();runs.event('execution_selected',record())
            self.assertLess(time.monotonic()-start,.2)
            self.assertTrue(entered.wait(1))
            start=time.monotonic();runs.event('task_cancelled',record(state='cancelled',revision=10));runs.summary('task');runs.close(.01)
            self.assertLess(time.monotonic()-start,.2)
        finally:release.set();runs.close(2)
        self.assertTrue(all(c[0]!='finish' for c in client.calls))


if __name__=='__main__':unittest.main()
