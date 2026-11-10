"""Real NAV HTTP/Runner/Verifier path, isolated from any robot or workflow."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brain.adapters.http_client import HttpContractError
from brain.skills.body_requests import navigation_body
from brain.skills.catalog import registered_ids
from brain.skills.tianji_navigation import TianjiNavigationSkill, navigation_request
from contracts.steps import StepSpec
from scripts.tianji_navigation import main as atomic_cli
from scripts.tianji_navigation_offline import fixture_server, run_suite, main as offline_cli


class TianjiNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = fixture_server()
        cls.fixture = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def setUp(self):
        self.fixture['rows'].clear()
        self.fixture['posts'].clear()
        self.skill = TianjiNavigationSkill(self.fixture['base_url'],
            token_env=self.fixture['token_env'], offline=True, request_timeout_sec=.3)

    def body(self, target='contract_success', command='nav-1'):
        return navigation_request('task-1', command, target_id=target, goal_xyt=[1.2, -.8, 1.57])

    def execute(self, body, **options):
        return self.skill.execute(body, deadline_sec=.3, poll_interval_sec=.01, **options)

    def test_wire_matches_existing_g1_builder_without_workflow_registration(self):
        body = self.body()
        step = StepSpec('nav', 'navigate', navigation={k: body[k] for k in (
            'target_id', 'route_phase', 'leg_index', 'goal_xyt', 'motion_mode')})
        self.assertEqual(body, navigation_body(step, 'task-1', 'nav-1'))
        self.assertNotIn('tianji_navigation', registered_ids())
        self.assertFalse(self.skill.port.capabilities()['workflow_registered'])

    def test_end_to_end_suite_uses_actual_atomic_port(self):
        report = run_suite(self.fixture['base_url'], token_env=self.fixture['token_env'], deadline=.5)
        self.assertEqual(report['status'], 'OFFLINE_CONTRACT_PASS', report)
        self.assertEqual(len(report['cases']), 8)
        self.assertTrue(all(row['passed'] for row in report['cases']))
        self.assertEqual(report['source'], 'external_navigation')
        self.assertFalse(report['physical_execution'])

    def test_request_validation_precedes_network_or_business_post(self):
        body = self.body()
        variants = [dict(body, goal=1), dict(body, command_id=''),
            dict(body, command_id='bad/id'), dict(body, frame_id='odom'),
            dict(body, leg_index=True), dict(body, leg_index=0),
            dict(body, goal_xyt=[math.nan, 0, 0]), dict(body, goal_xyt=[True, 0, 0]),
            dict(body, goal_xyt=[0, 0, math.inf]), dict(body, motion_mode='unknown'),
            dict(body, require_final_orientation=False)]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.skill.submit(value)
        self.assertEqual(self.fixture['posts'], [])

    def test_invalid_envelope_and_vla_context_cannot_reach_navigation(self):
        body = self.body()
        request = self.skill.contract(body).request
        for changed in (dict(request, skill='pick'), dict(request, task_id='foreign'),
                        dict(request, required_holding='cola'), dict(request, requires_vla_clearance=True)):
            with self.assertRaises(ValueError):
                self.skill.port.submit(body['command_id'], changed)
        self.assertEqual(self.fixture['posts'], [])

    def test_real_dispatch_is_closed_by_default_and_does_not_claim_original_never_ran(self):
        skill = TianjiNavigationSkill(self.fixture['base_url'], token_env=self.fixture['token_env'])
        with patch.object(skill.port.dream, 'status', return_value=dict(
                contract_version='fq/reception-lan/v1', interface_online=True)):
            result = skill.execute(self.body(), deadline_sec=.1, poll_interval_sec=.01)
        self.assertEqual(result['verdict'], 'UNKNOWN')
        raw = result['progress']['evidence']['downstream']
        self.assertEqual(raw['error']['code'], 'TIANJI_DISPATCH_BLOCKED')
        self.assertNotIn('action_started', raw['error']['details'])
        self.assertEqual(self.fixture['posts'], [])

    def test_offline_client_refuses_real_service_before_post(self):
        with patch.object(self.skill.port.dream, 'status', return_value=dict(
                contract_version='fq/reception-lan/v1', interface_online=True)):
            result = self.execute(self.body())
        self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertEqual(self.fixture['posts'], [])

    def test_real_client_refuses_fixture_before_post(self):
        skill = TianjiNavigationSkill(self.fixture['base_url'], token_env=self.fixture['token_env'], allow_motion=True)
        result = skill.execute(self.body(), deadline_sec=.1, poll_interval_sec=.01)
        self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertEqual(self.fixture['posts'], [])

    def test_real_observation_cannot_promote_simulated_success(self):
        self.execute(self.body())
        real = TianjiNavigationSkill(self.fixture['base_url'], token_env=self.fixture['token_env'])
        self.assertEqual(real.query(self.body())['verdict'], 'UNKNOWN')

    def test_real_interface_accepts_only_complete_real_scope_receipt(self):
        self.execute(self.body())
        row = self.fixture['rows']['nav-1']
        row.pop('simulated')
        row.pop('physical_execution')
        real = TianjiNavigationSkill(self.fixture['base_url'], token_env=self.fixture['token_env'], allow_motion=True)
        self.assertEqual(real.query(self.body())['verdict'], 'PASS')
        row['result']['navigation_stopped'] = False
        self.assertEqual(real.query(self.body())['verdict'], 'UNKNOWN')

    def test_network_loss_after_post_only_queries_original_id(self):
        result = self.execute(self.body('contract_post_lost'))
        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(len(self.fixture['posts']), 1)
        self.assertEqual(self.fixture['posts'][0], self.body('contract_post_lost'))

    def test_post_and_query_unknown_never_replays_motion(self):
        body = self.body('contract_post_unknown')
        result = self.execute(body)
        self.assertEqual(result['verdict'], 'UNKNOWN')
        fresh_client = TianjiNavigationSkill(self.fixture['base_url'],
            token_env=self.fixture['token_env'], offline=True)
        result = fresh_client.query(body)
        self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertEqual(len(self.fixture['posts']), 1)

    def test_query_timeout_does_not_cancel_or_submit_again(self):
        body = self.body('contract_pending')
        result = self.execute(body)
        self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertNotEqual(self.fixture['rows']['nav-1']['state'], 'cancelled')
        self.assertEqual(len(self.fixture['posts']), 1)

    def test_cancel_ack_is_not_stop_and_wrong_identity_is_rejected(self):
        body = self.body('contract_cancel')
        self.assertTrue(self.skill.submit(body)['accepted'])
        result = self.skill.cancel(body)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['completed'])
        self.assertEqual(result['raw']['command_id'], body['command_id'])
        progress = self.skill.query(body)['progress']
        self.assertEqual(progress['terminal'], 'cancelled')
        self.assertTrue(progress['publisher_stopped'])
        with patch.object(self.skill.port.dream, 'cancel', return_value={'command_id': 'foreign'}):
            self.assertFalse(self.skill.cancel(body)['accepted'])

    def test_offline_cancel_cannot_reach_real_instance(self):
        with patch.object(self.skill.port.dream, 'status', return_value=dict(
                contract_version='fq/reception-lan/v1', interface_online=True)), \
                patch.object(self.skill.port.dream, 'cancel') as cancel:
            with self.assertRaises(ValueError):
                self.skill.cancel(self.body())
            cancel.assert_not_called()

    def test_authorization_failure_is_not_an_execution_result(self):
        skill = TianjiNavigationSkill(self.fixture['base_url'], token_env='TIANJI_NAV_NO_SUCH_TOKEN', offline=True)
        with patch.dict('os.environ', {'TIANJI_NAV_NO_SUCH_TOKEN': 'wrong'}):
            result = skill.execute(self.body())
        self.assertEqual(result['verdict'], 'UNKNOWN')
        self.assertEqual(self.fixture['posts'], [])

    def test_finite_deadline_and_poll_are_required(self):
        for value in (math.inf, math.nan, 0, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.skill.contract(self.body(), deadline_sec=value)

    def test_invalid_metadata_is_unknown_and_does_not_post(self):
        for status in ({'interface_online': True}, {'contract_version': 'fq/reception-lan/v1',
                'interface_online': True, 'simulation': 'bad'}):
            with patch.object(self.skill.port.dream, 'status', return_value=status):
                self.assertEqual(self.execute(self.body())['verdict'], 'UNKNOWN')
        self.assertEqual(self.fixture['posts'], [])

    def test_missing_cancel_ack_does_not_prove_stop_and_preserves_receipt(self):
        with patch.object(self.skill.port.dream, 'cancel', return_value={'accepted': True}):
            result = self.skill.cancel(self.body())
        self.assertFalse(result['accepted'])
        self.assertFalse(result['completed'])
        self.assertEqual(result['raw'], {'accepted': True})

    def test_invalid_client_settings_are_rejected(self):
        for settings in ({'token_env': ''}, {'token_env': 'invalid-name'},
                         {'request_timeout_sec': math.nan}, {'request_timeout_sec': 0},
                         {'allow_motion': True, 'offline': True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                TianjiNavigationSkill(self.fixture['base_url'], **settings)

    def test_reports_exclude_credentials_and_preserve_original_requests(self):
        import os
        token = os.environ[self.fixture['token_env']]
        report = run_suite(self.fixture['base_url'], token_env=self.fixture['token_env'], deadline=.5)
        serialized = json.dumps(report)
        self.assertNotIn(token, serialized)
        self.assertNotIn('Authorization', serialized)
        self.assertTrue(report['requests'])
        self.assertTrue(all(r['command_id'] for r in report['requests']))

    def test_cli_query_reuses_saved_body_and_never_posts(self):
        body = self.body()
        self.execute(body)
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / 'request.json'
            request.write_text(json.dumps(body))
            with patch('builtins.print'):
                code = atomic_cli(['query', '--base-url', self.fixture['base_url'], '--offline',
                                   '--token-env', self.fixture['token_env'], '--request', str(request)])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fixture['posts']), 1)

    def test_offline_cli_never_overwrites_evidence_or_contacts_peer_on_path_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            report.write_text('original evidence')
            with self.assertRaises(FileExistsError):
                offline_cli(['--base-url', self.fixture['base_url'], '--report', str(report)])
            self.assertEqual(report.read_text(), 'original evidence')
        self.assertEqual(self.fixture['posts'], [])

    def test_request_snapshot_does_not_change_with_callers_mutation(self):
        body = self.body()
        original = deepcopy(body)
        contract = self.skill.contract(body)
        body['goal_xyt'][0] = 99
        self.assertEqual(contract.request['body'], original)


if __name__ == '__main__':
    unittest.main()
