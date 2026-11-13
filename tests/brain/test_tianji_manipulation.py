"""Tianji VLA real client, fake HTTP peers, and the downloaded result-only bridge."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from brain.skills.catalog import registered_ids
from brain.skills.tianji_manipulation import TianjiManipulationSkill, manipulation_request
from contracts.tianji_manipulation import observe_legacy_event
from contracts.vla_direct import FIELDS as G1_FIELDS
from scripts.tianji_manipulation import main as atomic_cli
from scripts.tianji_manipulation_offline import fixture_server, run_suite, main as offline_cli


class TianjiManipulationTests(unittest.TestCase):
    def setUp(self):
        self.context = fixture_server()
        self.fixture = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.skill = TianjiManipulationSkill(self.fixture['base_url'],
            token_env=self.fixture['token_env'], offline=True, request_timeout_sec=.3)

    def body(self, obj='fixture_success', operation='grasp_trash', command='command-1'):
        return manipulation_request('mission-1', command, operation=operation, object_id=obj,
                                    target_area='test_bin' if operation == 'dispose_trash' else 'test_table', arm='left')

    def execute(self, body):
        return self.skill.execute(body, deadline_sec=.2, poll_interval_sec=.01)

    def result_row(self):
        body = self.body()
        self.assertEqual(self.execute(body)['verdict'], 'PASS')
        return body, self.fixture['rows']['command-1']

    def test_shared_g1_fields_plus_explicit_arm_and_no_planner_registration(self):
        self.assertEqual(set(self.body()), set(G1_FIELDS) | {'arm'})
        self.assertNotIn('grasp_trash', registered_ids())
        self.assertNotIn('dispose_trash', registered_ids())
        self.assertFalse(self.execute(self.body())['workflow_registered'])

    def test_real_http_offline_suite(self):
        report = run_suite(self.fixture['base_url'], token_env=self.fixture['token_env'], deadline=.5)
        self.assertEqual(report['status'], 'OFFLINE_CONTRACT_PASS', report)
        self.assertEqual(len(report['cases']), 13)
        self.assertFalse(report['physical_execution'])

    def test_invalid_requests_never_reach_http(self):
        body = self.body()
        for invalid in (dict(body, arm='both'), dict(body, arm=None), dict(body, object_id=''),
                        dict(body, command_id='bad/id'), dict(body, result_path='/tmp/file'),
                        dict(body, operation='grasp_cola'), {k: v for k, v in body.items() if k != 'arm'}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.skill.submit(invalid)
        self.assertEqual(self.fixture['posts'], [])

    def test_contract_snapshots_and_finite_budgets(self):
        body = self.body()
        contract = self.skill.contract(body)
        body['arm'] = 'right'
        self.assertEqual(contract.request['body']['arm'], 'left')
        for value in (math.inf, math.nan, 0, -1, True):
            with self.assertRaises(ValueError):
                self.skill.contract(self.body(), deadline_sec=value)

    def test_mismatched_envelope_is_rejected_before_post(self):
        body = self.body()
        request = self.skill.contract(body).request
        for wrong in (dict(request, task_id='foreign'), dict(request, skill='navigate')):
            with self.assertRaises(ValueError):
                self.skill.port.submit(body['command_id'], wrong)
        self.assertEqual(self.fixture['posts'], [])

    def test_mode_and_token_configuration_are_validated(self):
        for settings in ({'token_env': ''}, {'token_env': 'not-valid'},
                         {'request_timeout_sec': math.nan}, {'request_timeout_sec': 0},
                         {'offline': True, 'allow_motion': True}):
            with self.assertRaises(ValueError):
                TianjiManipulationSkill(self.fixture['base_url'], **settings)

    def test_default_closed_and_offline_cannot_dispatch_to_real_peer(self):
        real_status = dict(contract_version='fq/reception-lan/v1')
        for skill in (self.skill, TianjiManipulationSkill(self.fixture['base_url'], token_env=self.fixture['token_env'])):
            with patch.object(skill.port.vla, 'control_status', return_value=real_status):
                result = skill.execute(self.body())
            self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertEqual(self.fixture['posts'], [])

    def test_real_client_rejects_offline_instance_and_offline_results(self):
        real = TianjiManipulationSkill(self.fixture['base_url'], token_env=self.fixture['token_env'], allow_motion=True)
        self.assertEqual(real.execute(self.body())['verdict'], 'UNKNOWN')
        self.assertEqual(self.fixture['posts'], [])
        self.execute(self.body())
        self.assertEqual(real.query(self.body())['verdict'], 'UNKNOWN')

    def test_complete_real_scope_receipt_passes_without_navigation_permission(self):
        body, row = self.result_row()
        row.pop('simulated')
        row.pop('physical_execution')
        real = TianjiManipulationSkill(self.fixture['base_url'], token_env=self.fixture['token_env'])
        result = real.query(body)
        self.assertEqual(result['verdict'], 'PASS')
        self.assertFalse(result['navigation_allowed'])
        self.assertNotIn('physical_execution', result['progress']['evidence'])

    def test_running_with_null_result_is_pending_not_a_transport_timeout(self):
        body = self.body('fixture_pending')
        self.skill.submit(body)
        result = self.skill.query(body)
        self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertFalse(result['progress']['timed_out'])
        self.assertEqual(result['progress']['terminal'], '')

    def test_identity_time_and_stop_failures_remain_unconfirmed(self):
        for obj in ('fixture_foreign', 'fixture_wrong_arm', 'fixture_bad_time', 'fixture_missing_stop', 'fixture_wrong_target'):
            with self.subTest(obj=obj):
                self.fixture['rows'].clear()
                self.assertEqual(self.execute(self.body(obj))['verdict'], 'UNKNOWN')

    def test_effect_proof_bound_to_original_identity_and_event(self):
        body, row = self.result_row()
        original = deepcopy(row)
        mutations = [('run_id', 'wrong-run'), ('event_id', 'wrong-event'), ('arm', 'right'),
                     ('source', 'model_probability'), ('reference', ''), ('target_identity_verified', False)]
        for field, value in mutations:
            row.clear(); row.update(deepcopy(original))
            row['result']['evidence'][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.skill.query(body)['verdict'], 'UNKNOWN')
        row.clear(); row.update(deepcopy(original))
        row['result']['evidence']['observed_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.assertEqual(self.skill.query(body)['verdict'], 'UNKNOWN')

    def test_bin_confirmation_cannot_be_replaced_by_empty_hand(self):
        body = self.body(operation='dispose_trash')
        self.assertEqual(self.execute(body)['verdict'], 'PASS')
        row = self.fixture['rows']['command-1']
        row['result']['bin_verified'] = False
        self.assertEqual(self.skill.query(body)['verdict'], 'FAIL')
        row['result'].pop('bin_verified')
        self.assertEqual(self.skill.query(body)['verdict'], 'UNKNOWN')

    def test_unavailable_is_explicit_refusal_not_success(self):
        result = self.execute(self.body('fixture_unavailable', operation='dispose_trash'))
        self.assertEqual(result['verdict'], 'FAIL')
        self.assertEqual(result['progress']['evidence']['submission_rejected']['code'], 'CAPABILITY_UNAVAILABLE')
        self.assertFalse(self.fixture['rows'])

    def test_lost_post_queries_original_once_without_resubmission(self):
        result = self.execute(self.body('fixture_post_lost'))
        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(len(self.fixture['posts']), 1)

    def test_unknown_post_and_404_are_not_evidence_of_nonexecution(self):
        body = self.body('fixture_post_unknown')
        self.assertEqual(self.execute(body)['verdict'], 'UNKNOWN')
        self.assertEqual(self.skill.query(body)['verdict'], 'UNKNOWN')
        self.assertEqual(len(self.fixture['posts']), 1)

    def test_wait_timeout_does_not_cancel(self):
        result = self.execute(self.body('fixture_pending'))
        self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertEqual(self.fixture['cancels'], [])
        self.assertEqual(len(self.fixture['posts']), 1)

    def test_cancel_acceptance_is_separate_from_stopped_query(self):
        body = self.body('fixture_pending')
        self.skill.submit(body)
        ack = self.skill.cancel(body)
        self.assertTrue(ack['accepted'])
        self.assertFalse(ack['completed'])
        observed = self.skill.query(body)
        self.assertEqual(observed['progress']['terminal'], 'cancelled')
        self.assertTrue(observed['progress']['publisher_stopped'])
        with patch.object(self.skill.port.vla, 'cancel', return_value={'accepted': True, 'task_id': 'foreign'}):
            self.assertFalse(self.skill.cancel(body)['accepted'])

    def test_offline_cancel_cannot_cancel_real_command(self):
        with patch.object(self.skill.port.vla, 'control_status', return_value={'contract_version': 'fq/reception-lan/v1'}):
            with self.assertRaises(ValueError):
                self.skill.cancel(self.body())
        self.assertEqual(self.fixture['cancels'], [])

    def test_authentication_failure_never_posts(self):
        skill = TianjiManipulationSkill(self.fixture['base_url'], token_env='TIANJI_WRONG_TEST_TOKEN', offline=True)
        with patch.dict(os.environ, {'TIANJI_WRONG_TEST_TOKEN': 'wrong'}):
            self.assertEqual(skill.execute(self.body())['verdict'], 'UNKNOWN')
        self.assertEqual(self.fixture['posts'], [])

    def test_legacy_event_is_stable_but_not_task_success_or_navigation(self):
        first = self.skill.observe_event('legacy-task', 'legacy-run', 'left')
        second = self.skill.observe_event('legacy-task', 'legacy-run', 'left')
        self.assertTrue(first['event_received'])
        self.assertEqual(first['event_id'], second['event_id'])
        self.assertFalse(first['task_completed'])
        self.assertFalse(first['navigation_allowed'])
        self.assertEqual(self.fixture['posts'], [])

    def test_stale_or_foreign_legacy_event_never_becomes_a_completion(self):
        raw = self.skill.observe_event('legacy-task', 'legacy-run', 'left')['raw']
        for value in (dict(raw, run_id='foreign'), dict(raw, arm='right'),
                      dict(raw, event_timestamp=0), dict(raw, event_timestamp=True),
                      dict(raw, holding_at_event=False), dict(raw, event_id='foreign')):
            seen = observe_legacy_event(value, task_id='legacy-task', run_id='legacy-run', arm='left')
            self.assertFalse(seen['event_received'])
            self.assertFalse(seen['task_completed'])

    def test_cli_query_never_posts_and_report_never_overwrites(self):
        body, _ = self.result_row()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'request.json'
            path.write_text(json.dumps(body))
            with patch('builtins.print'):
                code = atomic_cli(['query', '--base-url', self.fixture['base_url'], '--request', str(path),
                                   '--offline', '--token-env', self.fixture['token_env']])
            self.assertEqual(code, 0)
            report = Path(directory) / 'report.json'
            report.write_text('original evidence')
            with self.assertRaises(FileExistsError):
                offline_cli(['--base-url', self.fixture['base_url'], '--report', str(report)])
            self.assertEqual(report.read_text(), 'original evidence')
        self.assertEqual(len(self.fixture['posts']), 1)

    def test_report_excludes_authentication_values(self):
        token = os.environ[self.fixture['token_env']]
        report = run_suite(self.fixture['base_url'], token_env=self.fixture['token_env'], deadline=.5)
        self.assertEqual(report['status'], 'OFFLINE_CONTRACT_PASS', report)
        self.assertNotIn(token, json.dumps(report))


class DownloadedGatewayTests(unittest.TestCase):
    def test_unmodified_downloaded_gateway_can_feed_brain_event_reader(self):
        root = Path(__file__).resolve().parents[2] / 'data/tianji_vla_handoff_20261113/unpacked'
        if not (root / 'agent_cleanup_http.py').is_file():
            self.skipTest('Optional downloaded handoff package is not part of source checkout')
        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        bridge = load('test_tianji_downloaded_bridge', root / 'agent_grasp_bridge.py')
        with patch.dict(sys.modules, {'agent_grasp_bridge': bridge}):
            gateway = load('test_tianji_downloaded_gateway', root / 'agent_cleanup_http.py')
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result = dict(run_id='actual-run', arm='left', classifier_sha256=bridge.SOURCE_SHA256,
                timestamp=datetime.now(timezone.utc).timestamp(), p_success=.99, success=True,
                status='success_holding', mode='stop_hold', policy_stopped=True, holding=True)
            (folder / 'task_result.json').write_text(json.dumps(result))
            registry = folder / 'registry.json'
            registry.write_text(json.dumps({'tasks': {'actual-task': dict(run_id='actual-run',
                                                arm='left', result_path='task_result.json')}}))
            server = gateway.make_server(registry, port=0, token='downloaded-gateway-test-token')
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {'TIANJI_GATEWAY_TEST_TOKEN': 'downloaded-gateway-test-token'}):
                    skill = TianjiManipulationSkill('http://127.0.0.1:' + str(server.server_port),
                                                   token_env='TIANJI_GATEWAY_TEST_TOKEN')
                    caps = skill.legacy_check()['capabilities']
                    self.assertFalse(caps['grasp_start'])
                    event = skill.observe_event('actual-task', 'actual-run', 'left')
                    self.assertTrue(event['event_received'])
                    self.assertFalse(event['task_completed'])
                    self.assertFalse(event['navigation_allowed'])
                    self.assertFalse(event['raw']['nav_ready'])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
