"""Final peer indices are small reads of original commands, not control adapters."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from brain.adapters.diagnostic_peer_summaries import DiagnosticPeerSummaryReader
from brain.adapters.http_client import HttpContractError
from contracts.diagnostic_reports import DiagnosticReportError


class DiagnosticPeerSummaryReaderTests(unittest.TestCase):
    def setUp(self):
        self.summary=json.loads(Path('tests/contracts/vla_peer_diagnostic_summary.example.json').read_text())['diagnostic_summary']
        self.config={'reception_real':{'vla_base_url':'http://vla.original.invalid',
            'dream_base_url':'http://nav.original.invalid','body_control':{'vla_token_env':'VLA_SERVICE_TOKEN','nav_token_env':'NAV_SERVICE_TOKEN'}}}
        self.http=Mock();self.factory=Mock(return_value=self.http)
        self.raw={'task_id':'synthetic-task','command_id':'synthetic-command',
                  'diagnostic_summary':self.summary,'private_control_context':'never-copy'}
        self.http.request_json.return_value=(self.raw,200,{})
        self.reader=DiagnosticPeerSummaryReader(self.config,http_factory=self.factory)

    def call(self):
        return self.reader('vla','synthetic-task','synthetic-command','http://vla.original.invalid')

    def test_get_only_uses_configured_original_target_and_small_index(self):
        result=self.call()
        self.assertEqual(self.http.request_json.call_args.args,('GET','/v1/vla/tasks/synthetic-command'))
        self.assertEqual(self.http.request_json.call_args.kwargs['max_response_bytes'],512*1024)
        self.assertEqual(self.http.request_json.call_args.kwargs['timeout_sec'],2.)
        self.assertEqual(self.http.auth_token_env,'VLA_SERVICE_TOKEN')
        self.assertNotIn('private_control_context',result)
        self.assertEqual(result['evidence_refs'][0]['sha256'],self.summary['evidence_refs'][0]['sha256'])

    def test_changed_endpoint_is_refused_before_any_http(self):
        self.config['reception_real']['vla_base_url']='http://new.invalid'
        with self.assertRaises(DiagnosticReportError) as raised:self.call()
        self.assertEqual(raised.exception.code,'PEER_TARGET_CHANGED')
        self.factory.assert_not_called()

    def test_foreign_summary_and_state_authority_are_rejected(self):
        for change in ({'task_id':'other'},{'command_id':'other'},{'peer':'nav'},{'state_authority':True}):
            with self.subTest(change=change):
                raw=deepcopy(self.raw);raw['diagnostic_summary'].update(change)
                self.http.request_json.return_value=(raw,200,{})
                with self.assertRaises(DiagnosticReportError):self.call()

    def test_index_size_is_bounded_and_read_failure_is_not_latest_fallback(self):
        huge=deepcopy(self.raw);huge['diagnostic_summary']['findings']=[{'code':'LARGE','message':'x'*17000}]
        self.http.request_json.return_value=(huge,200,{})
        with self.assertRaises(DiagnosticReportError) as raised:self.call()
        self.assertEqual(raised.exception.code,'PEER_SUMMARY_TOO_LARGE')
        self.http.request_json.side_effect=HttpContractError('private failure body',status_code=404)
        with self.assertRaises(DiagnosticReportError) as raised:self.call()
        self.assertEqual(raised.exception.status_code,404)
        self.assertNotIn('private failure body',str(raised.exception))
        self.assertEqual(self.http.request_json.call_count,2)

    def test_quality_and_ref_allowlists_exclude_raw_arrays(self):
        raw=deepcopy(self.raw);raw['diagnostic_summary']['quality']['raw_joint_array']=[1,2,3]
        raw['diagnostic_summary']['evidence_refs'][0]['raw_frame_array']=[4,5,6]
        self.http.request_json.return_value=(raw,200,{})
        result=self.call()
        self.assertNotIn('raw_joint_array',json.dumps(result))
        self.assertNotIn('raw_frame_array',json.dumps(result))

    def test_nav_archive_state_null_generation_and_confidence_survive_read(self):
        native=json.loads(Path('tests/contracts/nav_peer_diagnostic_summary.example.json').read_text())['diagnostic_summary']
        for archive,generated in (('pending',None),('snapshot_sealed',native['generated_at'])):
            with self.subTest(archive=archive):
                summary=deepcopy(native);summary.update(archive_state=archive,generated_at=generated)
                self.http.request_json.return_value=({'task_id':'synthetic-task','command_id':'synthetic-command',
                                                       'diagnostic_summary':summary},200,{})
                result=self.reader('nav','synthetic-task','synthetic-command','http://nav.original.invalid')
                self.assertEqual(result['archive_state'],archive)
                self.assertEqual(result['generated_at'],generated)
                self.assertEqual(result['findings'][0]['confidence'],'confirmed')
                self.assertEqual(result['findings'][0]['meaning'],native['findings'][0]['meaning'])
                self.assertEqual(result['evidence_refs'][0]['ref_id'],native['evidence_refs'][0]['ref_id'])
                self.assertEqual(result['evidence_refs'][0]['time_window'],native['evidence_refs'][0]['time_window'])
        self.assertEqual(self.http.request_json.call_args.args,('GET','/v1/commands/synthetic-command'))

    def test_conflicting_classification_aliases_are_invalid(self):
        raw=deepcopy(self.raw)
        raw['diagnostic_summary']['findings'][0]['confidence']='unknown'
        self.http.request_json.return_value=(raw,200,{})
        with self.assertRaises(DiagnosticReportError) as raised:self.call()
        self.assertEqual(raised.exception.code,'PEER_SUMMARY_INVALID')


if __name__=='__main__':unittest.main()
