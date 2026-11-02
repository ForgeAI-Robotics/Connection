"""Exercise bounded read-only evidence queries without touching live services."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from brain.observability import diagnostics as d


class DiagnosticQueryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def write(self, rows, day='2026-10-31', name='brain.jsonl'):
        path = self.root / day / 'master' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
        return path

    def test_exact_identity_across_days_rotations_and_instances(self):
        self.write([dict(kind='CALL', id='task-one', cmd='cmd-one', event='start', time='2026-10-30T23:59:00+08:00')], day='2026-10-30')
        self.write([dict(log_schema_version=1, task_id='task-one', command_id='cmd-one',
                         service_instance_id='second-boot', code_revision='abc', event_id='event-a', event='observed')], name='brain.010000.abcd.jsonl')
        self.write([dict(task_id='task-two', command_id='cmd-two', error='mentions task-one cmd-one'),
                    dict(task_id='task-one-extra', command_id='cmd-one'),
                    dict(task_id='task-one', command_id='cmd-one', request_id='gate-one')])
        result = d.query_diagnostics(self.root, task_id='task-one', command_id='cmd-one')
        self.assertEqual(result['coverage']['returned'], 3)
        self.assertEqual({x['identity']['task_id'] for x in result['records']}, {'task-one'})
        self.assertIn('legacy', [x['format'] for x in result['records']])
        self.assertIn('v1', [x['format'] for x in result['records']])
        self.assertEqual(len(d.query_diagnostics(self.root, request_id='gate-one')['records']), 1)
        self.assertEqual({p['service'] for p in result['missing_evidence']}, set(d.PEERS))
        self.assertFalse(result['state_authority'])

    def test_date_scope_and_startup_metadata_do_not_export_argv(self):
        self.write([dict(task_id='old')], day='2026-10-29')
        self.write([dict(task_id='new')])
        startup = self.root/'2026-10-31/master/11-22-33.log'
        startup.write_text('# service=master\n# started_at=2026-10-31T11:22:33+08:00\n# pid=123\n# argv=--secret=do-not-export\n')
        result = d.query_diagnostics(self.root, date_from='2026-10-31', date_to='2026-10-31')
        self.assertEqual([x['identity']['task_id'] for x in result['records']], ['new'])
        self.assertEqual(result['startup_sources'][0]['metadata']['pid'], '123')
        self.assertNotIn('do-not-export', json.dumps(result))
        self.assertIn('not_task_proof', result['startup_sources'][0]['association'])

    def test_display_time_normalizes_timezone_and_preserves_unknown_time(self):
        self.write([{'task_id':'t','time':'2026-10-31T09:00:00+08:00','event_id':'later'},
                    {'task_id':'t','time':'2026-10-31T02:00:00+02:00','event_id':'earlier'},
                    {'task_id':'t','time':'2026-10-31T00:00:00','event_id':'unknown'}])
        result=d.query_diagnostics(self.root)
        self.assertEqual([r['event']['event_id'] for r in result['records']],['earlier','later','unknown'])
        self.assertEqual(result['coverage']['issues']['event_time_unknown'],1)

    def test_corrupt_partial_unsupported_and_nonfinite_lines_report_gaps(self):
        path = self.write([dict(task_id='good'), dict(log_schema_version=99, task_id='future'), []])
        with path.open('a') as f:
            f.write('not-json\n{"value": NaN}\n{"task_id":"unfinished"')
        result = d.query_diagnostics(self.root)
        self.assertEqual(len(result['records']), 1)
        for key in ('invalid_json_line', 'partial_line_skipped', 'unsupported_log_schema', 'non_object_line'):
            self.assertIn(key, result['coverage']['issues'])
        self.assertTrue(result['coverage']['incomplete'])

    def test_record_and_byte_budgets_are_explicit(self):
        self.write([dict(task_id='t', event_id=f'e-{n}', message='x'*80) for n in range(20)])
        result = d.query_diagnostics(self.root, limit=3)
        self.assertEqual(len(result['records']), 3)
        self.assertEqual(result['coverage']['issues']['record_limit'], 17)
        self.assertEqual({x['event']['event_id'] for x in result['records']}, {'e-17','e-18','e-19'})
        with patch.object(d, 'MAX_SCAN_BYTES', 350):
            result = d.query_diagnostics(self.root)
        self.assertLessEqual(result['coverage']['bytes_scanned'], 350)
        self.assertIn('file_prefix_not_scanned', result['coverage']['issues'])
        with patch.object(d, 'MAX_RESULT_BYTES', 10):
            result = d.query_diagnostics(self.root)
        self.assertFalse(result['records'])
        self.assertIn('result_byte_limit', result['coverage']['issues'])

    def test_file_directory_and_record_count_limits(self):
        for day in ('2026-10-28','2026-10-29','2026-10-30'):
            self.write([dict(task_id='t') for _ in range(5)], day=day)
        with patch.object(d, 'MAX_FILES', 1):
            result = d.query_diagnostics(self.root)
        self.assertEqual(len(result['coverage']['files_scanned']), 1)
        self.assertEqual(result['coverage']['issues']['file_limit'], 2)
        with patch.object(d, 'MAX_SCANNED_RECORDS', 2):
            result = d.query_diagnostics(self.root)
        self.assertLessEqual(len(result['records']), 2)
        self.assertIn('scan_record_limit', result['coverage']['issues'])
        with patch.object(d, 'MAX_ENTRIES', 1):
            result = d.query_diagnostics(self.root)
        self.assertIn('directory_entry_limit', result['coverage']['issues'])

    def test_symlink_files_directories_and_root_are_not_followed(self):
        outside = self.root/'private.jsonl'
        outside.write_text('{"task_id":"must-not-export"}\n')
        day = self.root/'2026-10-31/master'
        day.mkdir(parents=True)
        (day/'brain.jsonl').symlink_to(outside)
        (self.root/'2026-10-30').symlink_to(self.root/'2026-10-31', target_is_directory=True)
        result = d.query_diagnostics(self.root)
        self.assertFalse(result['records'])
        self.assertEqual(result['coverage']['issues']['symlink_skipped'], 2)
        linked = self.root/'linked'
        linked.symlink_to(self.root/'2026-10-31')
        self.assertFalse(d.query_diagnostics(linked)['records'])
        with self.assertRaises(OSError):
            with d._open_file(linked/'master/brain.jsonl'):
                self.fail('symlink should not be opened')

    def test_sensitive_values_redacted_even_in_legacy_archives(self):
        self.write([dict(task_id='t', error={'Authorization':'Bearer fake-secret'},
                         message='https://u:pw@example.invalid/x?token=top-secret')])
        result = d.query_diagnostics(self.root, task_id='t')
        text = json.dumps(result)
        for secret in ('fake-secret', 'top-secret', 'u:pw'):
            self.assertNotIn(secret, text)
        self.assertIn('REDACTED', text)

    def test_explicit_peer_import_is_bounded_filtered_and_local_only(self):
        peer = self.root/'nav_events.jsonl'
        peer.write_text('{"task_id":"t","command_id":"c","event":"failed","time":"2026-10-31T12:00:00+08:00"}\n')
        result = d.query_diagnostics(self.root, task_id='t', peer_events=[('nav',peer)], date_from='2026-10-31')
        self.assertEqual(result['records'][0]['identity']['command_id'], 'c')
        self.assertTrue(result['records'][0]['source']['imported'])
        self.assertNotIn('nav', [x['service'] for x in result['missing_evidence']])
        with self.assertRaises(ValueError):
            d.query_diagnostics(self.root, peer_events=[('unknown',peer)])
        with self.assertRaises(ValueError):
            d.query_diagnostics(self.root, peer_events=[('nav',self.root/'.env')])

    def test_empty_unrelated_and_capped_peer_evidence_are_not_claimed_present(self):
        peer=self.root/'nav.jsonl';peer.write_text('')
        result=d.query_diagnostics(self.root, task_id='t', peer_events=[('nav',peer)])
        self.assertEqual(result['peer_evidence']['nav']['files_read'],1)
        self.assertIn({'service':'nav','reason':'no_matching_peer_evidence'},result['missing_evidence'])
        peer.write_text('{"task_id":"another","time":"2026-10-31T12:00:00+08:00"}\n')
        self.assertEqual(d.query_diagnostics(self.root, task_id='t', peer_events=[('nav',peer)])['peer_evidence']['nav']['matching_records_scanned'],0)
        peer.write_text('{"task_id":"t","time":"2026-10-31T12:00:00+08:00"}\n')
        with patch.object(d,'MAX_RESULT_BYTES',1):
            result=d.query_diagnostics(self.root, task_id='t', peer_events=[('nav',peer)])
        self.assertIn({'service':'nav','reason':'matching_peer_evidence_not_returned_within_limits'},result['missing_evidence'])

    def test_attachment_manifest_only_imports_whitelisted_metadata(self):
        manifest = self.root/'manifest.json'
        value={'schema':'fq/diagnostic-attachment/v1','kind':'rosbag','session_id':'s',
               'started_at':'2026-10-31T12:00:00+08:00','ended_at':None,
               'Authorization':'Bearer do-not-export','files':[{'path':'/not/read/private.bag','password':'secret'}],
               'associations':[{'task_id':'t','command_id':'c','unexpected':'discard'}]}
        manifest.write_text(json.dumps(value))
        result=d.query_diagnostics(self.root, attachments=[manifest])['attachments'][0]
        self.assertEqual(result['association_basis'],'operator_supplied')
        self.assertFalse(result['referenced_files_read'])
        self.assertEqual(result['verification'],'metadata_only_unverified')
        self.assertEqual(result['files'],[{'path':'/not/read/private.bag'}])
        self.assertNotIn('Authorization',result)
        linked=d.query_diagnostics(self.root,task_id='t',attachments=[manifest])['attachments'][0]
        self.assertEqual(linked['query_association'],'explicit_identity_match_metadata_only')
        unrelated=d.query_diagnostics(self.root,task_id='different',attachments=[manifest])['attachments'][0]
        self.assertEqual(unrelated['query_association'],'not_matched_to_query')
        value['started_at']='2026-10-31T12:00:00'
        manifest.write_text(json.dumps(value))
        with self.assertRaises(ValueError):d.load_attachment_manifest(manifest)
        manifest.unlink();manifest.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):d.load_attachment_manifest(manifest)

    def test_invalid_inputs_fail_without_scan(self):
        for options in ({'task_id':'../../etc/passwd'}, {'date_from':'2026-02-30'},
                        {'date_from':'2026-11-02','date_to':'2026-10-31'},
                        {'limit':0}, {'limit':True}, {'limit':2001}, {'limit':'1.5'}):
            with self.subTest(options=options), patch.object(d,'_discover',side_effect=AssertionError('must not scan')):
                with self.assertRaises(ValueError):d.query_diagnostics(self.root,**options)

    def test_gateway_native_attachment_and_event_fixtures(self):
        source=Path('tests/contracts/gateway_rosbag_manifest.example.json')
        value=json.loads(source.read_text())
        event=json.loads(Path('tests/contracts/gateway_diagnostic_event.example.json').read_text())
        peer=self.root/'events.jsonl.1';peer.write_text(json.dumps(event)+'\n')
        result=d.query_diagnostics(self.root, request_id='synthetic-select-001',
                                   peer_events=[('gateway',peer)],attachments=[source])
        self.assertEqual(result['records'][0]['event']['receipt']['state'],'unknown')
        attachment=result['attachments'][0]
        self.assertEqual(attachment['session_path'],'/synthetic/session')
        self.assertEqual(attachment['files'][0]['bytes'],123)
        self.assertEqual(attachment['bag']['total_messages'],300)
        self.assertEqual(attachment['query_association'],'unassociated')
        # Matching run_id alone never proves a task association.
        result=d.query_diagnostics(self.root, task_id=value['run_id'],attachments=[source])
        self.assertEqual(result['attachments'][0]['query_association'],'unassociated')
        value.update(ended_at=None,ended_monotonic=None,active=None,data_available=None,
                     recording_status='running',snapshot_consistent=None)
        value['associations']=[{'task_id':'declared-task','request_id':'synthetic-select-001'}]
        copied=self.root/'annotation.json';copied.write_text(json.dumps(value))
        result=d.query_diagnostics(self.root,task_id='declared-task',attachments=[copied])['attachments'][0]
        self.assertEqual(result['query_association'],'explicit_identity_match_metadata_only')
        self.assertIsNone(result['active']);self.assertIsNone(result['ended_monotonic'])
        self.assertFalse(result['referenced_files_read'])
        value['task_authority']=True;copied.write_text(json.dumps(value))
        with self.assertRaises(ValueError):d.load_attachment_manifest(copied)

    def selection(self, task='task-one', command='cmd-one', request='request-one', receiver='receiver-one', event_id='selection-one'):
        return {'kind':'GATEWAY','component':'source_gate','event':'selection_intent_saved',
                'task_id':task,'command_id':command,'request_id':request,
                'receiver_instance_id':receiver,'event_id':event_id,'time':'2026-10-31T12:00:00+08:00'}

    def peer(self, rows):
        path=self.root/'peer_events.jsonl'
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        return path

    def test_task_query_joins_gateway_request_only_using_brain_provenance(self):
        self.write([self.selection()])
        peer=self.peer([{'event':'http_response','request_id':'request-one','receipt':{'receiver_instance_id':'receiver-one','state':'unknown'},'time':'2026-10-31T04:00:01+00:00'}])
        result=d.query_diagnostics(self.root,task_id='task-one',command_id='cmd-one',peer_events=[('gateway',peer)])
        self.assertEqual(len(result['records']),2)
        joined=next(r for r in result['records'] if r['source']['imported'])
        self.assertIsNone(joined['identity']['task_id'])
        self.assertNotIn('task_id',joined['event'])
        self.assertEqual(joined['correlation']['resolved_identity']['task_id'],'task-one')
        self.assertEqual(joined['correlation']['receiver_check'],'matched')
        self.assertEqual(joined['correlation']['sources'][0]['event_id'],'selection-one')
        self.assertEqual(joined['correlation']['sources'][0]['identity']['request_id'],'request-one')
        self.assertEqual(result['peer_evidence']['gateway']['records_returned'],1)

    def test_request_identity_preserves_gateway_punctuation_and_length_contract(self):
        for request_id in ('_select-001', ':r1', '-select', '.request', '_' * 128):
            with self.subTest(request_id=request_id):
                self.write([self.selection(request=request_id)])
                peer=self.peer([{'request_id':request_id,'time':'2026-10-31T04:00:01+00:00'}])
                result=d.query_diagnostics(self.root,request_id=request_id,peer_events=[('gateway',peer)])
                self.assertEqual(len(result['records']),2)
                result=d.query_diagnostics(self.root,task_id='task-one',peer_events=[('gateway',peer)])
                joined=next(r for r in result['records'] if r['source']['imported'])
                self.assertEqual(joined['identity']['request_id'],request_id)
                self.assertEqual(joined['correlation']['resolved_identity']['task_id'],'task-one')
        for request_id in ('_' * 129, 'has space', 'a/b', 'a\nb'):
            with self.subTest(invalid=request_id), self.assertRaises(ValueError):
                d.query_diagnostics(self.root,request_id=request_id)
        for fields in ({'task_id':'_task'}, {'command_id':':command'}, {'task_id':'t'*129}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                d.query_diagnostics(self.root,**fields)

    def test_foreign_task_command_and_receiver_claims_are_not_rewritten(self):
        self.write([self.selection()])
        peer=self.peer([{'request_id':'request-one','task_id':'foreign'},
                        {'request_id':'request-one','command_id':'foreign'},
                        {'request_id':'request-one','receiver_instance_id':'foreign'},
                        {'request_id':'request-one','receipt':{'receiver_instance_id':'foreign'}},
                        {'request_id':'request-one','task_id':['malformed']}])
        result=d.query_diagnostics(self.root,task_id='task-one',peer_events=[('gateway',peer)])
        self.assertEqual(result['peer_evidence']['gateway']['records_returned'],0)
        self.assertEqual(result['coverage']['issues']['conflicting_peer_identity'],2)
        self.assertEqual(result['coverage']['issues']['conflicting_peer_receiver'],2)
        self.assertEqual(result['coverage']['issues']['invalid_peer_identity_claim'],1)

    def test_ambiguous_mappings_excluded_but_consistent_duplicates_are_not(self):
        first=self.selection();duplicate=self.selection(event_id='same-fact-again')
        path=self.write([first,duplicate])
        peer=self.peer([{'request_id':'request-one'}])
        result=d.query_diagnostics(self.root,task_id='task-one',peer_events=[('gateway',peer)])
        self.assertEqual(result['peer_evidence']['gateway']['records_returned'],1)
        with path.open('a') as f:f.write(json.dumps(self.selection(task='foreign-task',event_id='conflicting-fact'))+'\n')
        result=d.query_diagnostics(self.root,task_id='task-one',peer_events=[('gateway',peer)])
        self.assertEqual(result['peer_evidence']['gateway']['records_returned'],0)
        self.assertIn('ambiguous_peer_association',result['coverage']['issues'])

    def test_only_authoritative_top_level_events_establish_correlations(self):
        self.write([{'kind':'CALL','task_id':'task-one','command_id':'cmd-one','raw':{'request_id':'request-one'}},
                    {**self.selection(),'event':'identity_mismatch'},
                    {**self.selection(),'event':'receipt_observed'}])
        peer=self.peer([{'request_id':'request-one'}])
        result=d.query_diagnostics(self.root,task_id='task-one',peer_events=[('gateway',peer)])
        self.assertEqual(result['peer_evidence']['gateway']['records_returned'],0)
        self.assertIn('peer_identity_mapping_missing',result['coverage']['issues'])
        self.write([{'kind':'TASK','component':'runtime','task_id':'task-one','command_id':'cmd-one'}])
        peer=self.peer([{'command_id':'cmd-one','event':'navigation_failed'}])
        result=d.query_diagnostics(self.root,task_id='task-one',peer_events=[('nav',peer)])
        self.assertEqual(result['peer_evidence']['nav']['records_returned'],1)

    def test_capped_association_scan_never_infers_a_missing_peer_task(self):
        self.write([self.selection()])
        peer=self.peer([{'request_id':'request-one'}])
        with patch.object(d,'MAX_ASSOCIATIONS',1):
            result=d.query_diagnostics(self.root,task_id='task-one',peer_events=[('gateway',peer)])
        self.assertEqual(result['peer_evidence']['gateway']['records_returned'],0)
        self.assertIn('association_limit',result['coverage']['associations']['local_scan_gaps'])
        self.assertIn('peer_association_unavailable_incomplete_local_scan',result['coverage']['issues'])

    def test_cli_only_reads_explicit_inputs_and_reports_json(self):
        self.write([dict(task_id='t',event='failed')])
        result=subprocess.run(['venv/core/bin/python','scripts/brain_diagnostics.py','--task-id','t'],
            env={**os.environ,'FQPLANNER_LOG_ROOT':str(self.root)},capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['coverage']['returned'],1)


if __name__=='__main__':unittest.main()
