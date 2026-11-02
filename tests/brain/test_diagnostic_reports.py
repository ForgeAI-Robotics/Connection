"""Strict diagnostic summary identity, bounded HTTPS requests and inert evidence."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from brain.adapters.diagnostic_reports import DiagnosticReportsClient
from brain.adapters.http_client import HttpContractError
from contracts.diagnostic_reports import DiagnosticReportError, validate_report, validate_request


FIXTURE = Path(__file__).resolve().parents[1] / 'contracts/robot_diagnostic_report.example.json'
COMPLETE_FIXTURE = Path(__file__).resolve().parents[1] / 'contracts/robot_diagnostic_report_complete.example.json'


def example():
    return json.loads(FIXTURE.read_text())


class DiagnosticReportsTests(unittest.TestCase):
    def setUp(self):
        self.raw = example()
        self.http = Mock()
        self.http.request_json.return_value = (self.raw, 200, {})
        self.client = DiagnosticReportsClient({'gateway_base_url':'https://gateway.invalid',
            'gateway_ca_file':'/not-opened-with-fake-client', 'gateway_token_env':'TEST_GATEWAY_TOKEN'}, http=self.http)
        self.run_id, self.task_id = self.raw['run_id'], self.raw['task_id']

    def begin(self):
        return {'run_id':self.run_id, 'task_id':self.task_id,
                'started_at':self.raw['window']['requested_start'], 'profile':'navigation',
                'command_ids':[], 'request_ids':[]}

    def get(self):
        return self.client.get_report(self.run_id, task_id=self.task_id)

    def test_get_is_one_bounded_read_of_original_run_without_action(self):
        result = self.get()
        args, kwargs = self.http.request_json.call_args
        self.assertEqual(args, ('GET', '/v1/diagnostics/runs/diag-task-001/report', None))
        self.assertEqual(kwargs['max_response_bytes'], 65536)
        self.assertLessEqual(kwargs['timeout_sec'], 2)
        self.assertEqual(self.http.request_json.call_count, 1)
        self.assertFalse(result['state_authority'])
        self.assertEqual(result['quality']['missing_evidence'], ['recording_disabled'])
        self.assertNotIn('success', result)

    def test_get_does_not_accept_202_as_a_cached_report(self):
        self.http.request_json.return_value=(self.raw,202,{})
        with self.assertRaises(DiagnosticReportError) as caught:self.get()
        self.assertEqual(caught.exception.code,'INVALID_RESPONSE')
        self.http.request_json.assert_called_once()

    def test_begin_and_finish_do_not_refresh_original_timestamps_or_mutate_input(self):
        body = self.begin()
        expected = deepcopy(body)
        self.client.begin_run(body)
        self.client.begin_run(body)
        self.assertEqual(body, expected)
        self.assertEqual(self.http.request_json.call_args_list[0].args[2], expected)
        self.assertEqual(self.http.request_json.call_args_list[1].args[2], expected)
        finish = {'task_id':self.task_id,'ended_at':'2026-11-02T16:01:00+08:00',
                  'reason':'completed','command_ids':['nav-original'],'request_ids':['gate-original']}
        self.client.finish_run(self.run_id, finish)
        self.assertEqual(self.http.request_json.call_args.args[2], finish)

    def test_event_receipt_must_preserve_original_event_identity(self):
        event = {'event_id':'event-001','task_id':self.task_id,
                 'at':'2026-11-02T16:00:01+08:00','source':'brain','kind':'command','command_id':'nav-original'}
        with self.assertRaises(DiagnosticReportError) as caught:
            self.client.add_event(self.run_id, event)
        self.assertEqual(caught.exception.code, 'IDENTITY_MISMATCH')
        self.raw['event_id'] = 'event-001'
        self.assertEqual(self.client.add_event(self.run_id, event)['event_id'], 'event-001')

    def test_wrong_task_run_schema_and_authority_are_rejected(self):
        for key, value in [('task_id','foreign'),('run_id','foreign'),('schema','wrong'),('state_authority',True)]:
            with self.subTest(key=key):
                changed = deepcopy(self.raw)
                changed[key] = value
                with self.assertRaises(DiagnosticReportError):
                    validate_report(changed, run_id=self.run_id, task_id=self.task_id)

    def test_unknown_enums_invalid_types_and_unbounded_lists_are_rejected(self):
        variants = []
        for key, value in [('status','success'),('findings',[self.raw['findings'][0]] * 33),
                           ('evidence_refs',[{}] * 33),('generated_at','2026-11-02T16:00:00')]:
            changed=deepcopy(self.raw);changed[key]=value;variants.append(changed)
        changed=deepcopy(self.raw);changed['quality']['recording_ready']='true';variants.append(changed)
        changed=deepcopy(self.raw);changed['findings'][0]['assessment']='proven';variants.append(changed)
        changed=deepcopy(self.raw);changed['window']['requested_end']='2026-11-01T16:00:00+08:00';variants.append(changed)
        changed=deepcopy(self.raw);changed['quality']['capture']['value']=float('inf');variants.append(changed)
        for value in variants:
            with self.assertRaises(DiagnosticReportError):
                validate_report(value,run_id=self.run_id,task_id=self.task_id)

    def test_oversize_response_rejected_before_projection(self):
        self.raw['untrusted_sensor_dump'] = 'x' * 70000
        with self.assertRaises(DiagnosticReportError) as raised:
            self.get()
        self.assertEqual(raised.exception.code, 'RESPONSE_TOO_LARGE')

    def test_error_responses_are_explicit_and_never_fall_back_to_latest(self):
        for status, code in [(404,'NOT_FOUND'),(401,'PERMISSION_DENIED'),(403,'PERMISSION_DENIED'),
                             (409,'CONFLICT'),(503,'UNAVAILABLE'),(400,'INVALID_REQUEST'),(200,'INVALID_RESPONSE')]:
            self.http.reset_mock()
            self.http.request_json.side_effect = HttpContractError('raw credential must-not-print', status_code=status)
            with self.assertRaises(DiagnosticReportError) as raised:
                self.get()
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn('must-not-print', str(raised.exception))
            self.assertEqual(self.http.request_json.call_count, 1)
            self.assertEqual(self.http.request_json.call_args.args[0], 'GET')
        self.http.request_json.side_effect = TimeoutError('must-not-print')
        with self.assertRaises(DiagnosticReportError) as raised:
            self.get()
        self.assertEqual(raised.exception.code, 'UNAVAILABLE')

    def test_only_exact_gateway_busy_code_is_retryable_busy(self):
        for remote_code, expected in [('DIAGNOSTIC_BUSY','BUSY'),('DIAGNOSTIC_RUN_CONFLICT','CONFLICT'),
                ('DIAGNOSTIC_TASK_CONFLICT','CONFLICT'),('DIAGNOSTIC_FINISH_CONFLICT','CONFLICT'),
                ('DIAGNOSTIC_EVENT_CONFLICT','CONFLICT'),('OTHER_BUSY','CONFLICT')]:
            self.http.reset_mock()
            self.http.request_json.side_effect=HttpContractError('untrusted secret=must-not-leak',status_code=409,
                payload={'error':{'code':remote_code}})
            with self.assertRaises(DiagnosticReportError) as caught:self.client.begin_run(self.begin())
            self.assertEqual(caught.exception.code,expected)
            self.assertEqual(caught.exception.status_code,409)
            self.assertNotIn('must-not-leak',str(caught.exception))
            self.http.request_json.assert_called_once()

    def test_begin_and_finish_validate_requested_times_without_using_recording_times(self):
        begin=self.begin();begin['started_at']='2026-11-02T08:00:00+00:00'
        self.client.begin_run(begin)  # Same instant with different timezone representation.
        begin['started_at']='2026-11-02T08:00:01+00:00'
        with self.assertRaises(DiagnosticReportError) as caught:self.client.begin_run(begin)
        self.assertEqual(caught.exception.code,'IDENTITY_MISMATCH')
        self.raw['window']['requested_end']='2026-11-02T08:00:01+00:00'
        self.raw['window']['recording_start']='2026-11-02T08:00:02+00:00'
        self.raw['window']['recording_end']='2026-11-02T08:00:03+00:00'
        finish={'task_id':self.task_id,'ended_at':'2026-11-02T16:00:01+08:00','reason':'completed',
                'command_ids':[],'request_ids':[]}
        self.client.finish_run(self.run_id,finish)
        finish['ended_at']='2026-11-02T16:00:02+08:00'
        with self.assertRaises(DiagnosticReportError) as caught:self.client.finish_run(self.run_id,finish)
        self.assertEqual(caught.exception.code,'IDENTITY_MISMATCH')

    def test_report_refs_are_inert_redacted_summaries_not_sensor_data(self):
        self.raw['evidence_refs'] = [{'evidence_id':'e1','kind':'log','source':'gateway',
            'available':False,'reference':'https://outside.invalid/log?token=must-not-leak'}]
        self.raw['findings'][0]['message'] = 'access_token=must-not-leak'
        self.raw['raw_sensor_data'] = {'ignored':'payload'}
        result = self.get()
        self.assertNotIn('must-not-leak', json.dumps(result))
        self.assertNotIn('raw_sensor_data', result)
        self.assertFalse(result['evidence_refs'][0]['available'])
        self.assertEqual(self.http.request_json.call_count, 1)

    def test_complete_status_does_not_promote_partial_quality_or_business_success(self):
        self.raw['status']='complete'
        self.raw['quality']['missing_evidence']=['nav_log_missing']
        self.raw['quality']['loss_visibility']='unknown'
        result=self.get()
        self.assertEqual(result['quality']['missing_evidence'],['nav_log_missing'])
        self.assertEqual(result['quality']['loss_visibility'],'unknown')
        self.assertFalse(result['state_authority'])
        self.assertNotIn('success',result)

    def test_nested_quality_budgets_reasons_and_navigation_only_scope_survive(self):
        self.raw['quality'].update(scope='navigation_profile_only',whole_robot_complete=False,
            analysis={'state':'finished','truncated':True,'budgets':{'max_messages':100},
                      'truncation_reasons':['message_budget'],'missing':['optional_topic']},
            capture={'resource_samples':3,'errors':[],'cpu_budget':{'percent':10.0}},
            archive={'hash_budgets':{'max_files':10},'scanned_files':2,'truncated':False})
        result=self.get()
        self.assertEqual(result['quality']['scope'],'navigation_profile_only')
        self.assertFalse(result['quality']['whole_robot_complete'])
        self.assertEqual(result['quality']['analysis']['budgets'],{'max_messages':100})
        self.assertTrue(result['quality']['analysis']['truncated'])
        self.raw['quality']['whole_robot_complete']=True
        with self.assertRaises(DiagnosticReportError):self.get()

    def test_gateway_complete_fixture_retains_quality_and_external_claim_boundaries(self):
        raw=json.loads(COMPLETE_FIXTURE.read_text())
        result=validate_report(raw,run_id=raw['run_id'],task_id=raw['task_id'])
        for key in ('scope','whole_robot_complete','applicability','navigation_physical',
                    'manipulation_physical','recorder_released','processing_complete','finish_reason',
                    'required_topics_observed','indexed_events','external_summaries',
                    'topics','analysis','capture','archive'):
            self.assertEqual(result['quality'][key],raw['quality'][key],key)
        self.assertEqual(result['quality']['external_summaries'][0]['verification'],'source_declared')
        self.assertEqual(result['quality']['loss_visibility'],'unknown')
        self.assertFalse(result['state_authority'])
        self.assertNotIn('success',result)
        raw['quality']['external_summaries'][0]['verification']='independently_verified'
        with self.assertRaises(DiagnosticReportError):
            validate_report(raw,run_id=raw['run_id'],task_id=raw['task_id'])

    def test_topic_measurement_types_do_not_turn_strings_or_bools_into_counts(self):
        for key,value in [('messages_observed',True),('messages_observed',-1),('hz','10'),('max_gap_ms',False)]:
            raw=json.loads(COMPLETE_FIXTURE.read_text())
            raw['quality']['topics']['/localization/odom_nav'][key]=value
            with self.assertRaises(DiagnosticReportError):
                validate_report(raw,run_id=raw['run_id'],task_id=raw['task_id'])

    def test_request_ids_keep_leading_punctuation_without_relaxing_business_ids(self):
        body=self.begin();body['request_ids']=['_gateway-original',':gateway-original']
        self.client.begin_run(body)
        self.assertEqual(self.http.request_json.call_args.args[2]['request_ids'],body['request_ids'])
        body['command_ids']=['_not-a-business-command']
        with self.assertRaises(DiagnosticReportError):self.client.begin_run(body)

    def test_unknown_request_fields_or_route_escape_are_not_sent(self):
        body=self.begin();body['control_authorization']='not-allowed'
        with self.assertRaises(DiagnosticReportError):self.client.begin_run(body)
        for value in ('../other','a/b','..','.', 'https://other.invalid'):
            with self.assertRaises(DiagnosticReportError):
                self.client.get_report(value,task_id=self.task_id)
        self.http.request_json.assert_not_called()

    def test_configuration_requires_trusted_existing_https_identity(self):
        for url in ('http://gateway.invalid','https://user:password@gateway.invalid',
                    'https://gateway.invalid?token=secret','https://gateway.invalid#fragment'):
            with self.assertRaises(DiagnosticReportError):
                DiagnosticReportsClient({'gateway_base_url':url,'gateway_ca_file':'test'},http=Mock())

    def test_outbound_summary_redaction_keeps_original_identity_and_input(self):
        self.raw['event_id']='event-001'
        event={'event_id':'event-001','task_id':self.task_id,'at':'2026-11-02T16:00:01+08:00',
               'source':'brain','kind':'failure','summary':{'error':'Bearer test-only-secret'}}
        original=deepcopy(event)
        self.client.add_event(self.run_id,event)
        sent=self.http.request_json.call_args.args[2]
        self.assertNotIn('test-only-secret',json.dumps(sent))
        self.assertEqual(sent['event_id'],event['event_id'])
        self.assertEqual(sent['at'],event['at'])
        self.assertEqual(event,original)

    def test_evidence_index_cannot_silently_omit_referenced_evidence(self):
        self.raw['findings'][0]['evidence_ids']=['missing']
        with self.assertRaises(DiagnosticReportError):self.get()


class DiagnosticReportHTTPSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='diagnostic-report-tls-')
        cls.cert=str(Path(cls.temp.name)/'server.crt')
        key=str(Path(cls.temp.name)/'server.key')
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1',
            '-keyout',key,'-out',cls.cert],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.calls=[]
        cls.mode='normal'
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.respond()
            def do_POST(self):self.respond()
            def respond(self):
                body=None
                if self.command=='POST':body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                cls.calls.append((self.command,self.path,body))
                if self.headers.get('Authorization')!='Bearer diagnostic-test-secret':
                    self.send_response(401);self.end_headers();return
                if cls.mode=='redirect':
                    self.send_response(302);self.send_header('Location','https://192.0.2.99/never-open');self.end_headers();return
                if cls.mode=='missing':
                    self.send_response(404);self.end_headers();self.wfile.write(b'{"error":{"code":"DIAGNOSTIC_RUN_NOT_FOUND"}}');return
                data=example()
                if body and 'event_id' in body:data['event_id']=body['event_id']
                raw=(b'not json secret=diagnostic-test-secret' if cls.mode=='bad_json' else
                     b'x'*70000 if cls.mode=='oversize' else json.dumps(data).encode())
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                try:self.wfile.write(raw)
                except OSError:pass
            def log_message(self,*args):pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cls.cert,key)
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(1);cls.temp.cleanup()

    def setUp(self):
        type(self).calls=[];type(self).mode='normal'
        self.environment=patch.dict(os.environ,{'DIAGNOSTIC_CLIENT_TEST_TOKEN':'diagnostic-test-secret',
                                               'DIAGNOSTIC_CLIENT_TEST_TOKEN_FILE':''})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.client=DiagnosticReportsClient({'gateway_base_url':f'https://127.0.0.1:{self.server.server_port}',
            'gateway_ca_file':self.cert,'gateway_token_env':'DIAGNOSTIC_CLIENT_TEST_TOKEN'},timeout_sec=.5)

    def test_exact_run_lifecycle_over_trusted_https_and_read_only_get(self):
        begin={'run_id':'diag-task-001','task_id':'task-001','started_at':'2026-11-02T16:00:00+08:00',
               'profile':'navigation','command_ids':[],'request_ids':[]}
        self.client.begin_run(begin)
        event={'event_id':'event-1','task_id':'task-001','at':'2026-11-02T16:00:01+08:00','source':'brain','kind':'task'}
        self.client.add_event('diag-task-001',event)
        finish={'task_id':'task-001','ended_at':'2026-11-02T16:00:02+08:00','reason':'completed',
                'command_ids':[],'request_ids':[]}
        self.client.finish_run('diag-task-001',finish)
        report=self.client.get_report('diag-task-001',task_id='task-001')
        self.assertFalse(report['state_authority'])
        self.assertEqual([c[0] for c in self.calls],['POST','POST','POST','GET'])
        self.assertEqual(self.calls[0][2],begin)
        self.assertEqual(self.calls[2][2],finish)
        self.assertTrue(all('/diagnostics/' in c[1] for c in self.calls))

    def test_missing_bad_json_oversize_and_redirect_are_explicit_without_retry(self):
        for mode,code in [('missing','NOT_FOUND'),('bad_json','INVALID_RESPONSE'),
                          ('oversize','RESPONSE_TOO_LARGE'),('redirect','INVALID_RESPONSE')]:
            type(self).mode=mode
            before=len(self.calls)
            with self.assertRaises(DiagnosticReportError) as raised:
                self.client.get_report('diag-task-001',task_id='task-001')
            self.assertEqual(raised.exception.code,code)
            self.assertNotIn('diagnostic-test-secret',str(raised.exception))
            self.assertEqual(len(self.calls),before+1)


if __name__ == '__main__':
    unittest.main()
