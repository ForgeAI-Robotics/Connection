from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from contracts.vla import holding_expiry, validate_current_holding, validate_evidence
from contracts.body_control import ControlError
from tests.brain.holding_fixture import holding_status
from tests.brain import test_body_control as body_tests


class HoldingContractTests(unittest.TestCase):
    def setUp(self):
        self.status = holding_status('task', 'grasp', dict(lease_id='lease', authority_id='brain', ownership_epoch=3))
        self.association = deepcopy(self.status['holding_evidence']['anchor']['association'])

    def check(self, status):
        return validate_current_holding(status, 'cola_can_1', robot_serial='test-robot',
                                        task_id='task', source_association=self.association)

    def test_valid_proof_is_read_only_and_inventory_alone_never_suffices(self):
        before = deepcopy(self.status)
        proof = self.check(self.status)
        self.assertEqual(proof['proof_id'], before['holding_evidence']['proof_id'])
        self.assertEqual(self.status, before)
        for change in ({'current_holding': None}, {'holding_evidence': None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.check(dict(self.status, **change))

    def test_expired_invalid_foreign_and_inconsistent_proofs_stay_blocked(self):
        for field, value in [('valid', False), ('signature_verified', False),
                ('invalid_reason', 'FEEDBACK_INTERRUPTED'), ('schema', 'old'), ('proof_id', 'wrong'),
                ('robot_serial', 'other'), ('task_id', 'other'), ('command_id', 'other'),
                ('holding', 'other'), ('valid_until', '2000-01-01T00:00:00+00:00'),
                ('observed_at', (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat())]:
            candidate = deepcopy(self.status)
            candidate['holding_evidence'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(candidate)

    def test_new_instances_and_target_or_feedback_generations_cannot_reuse_proof(self):
        for field, value in [('receiver_instance_id', 'new-receiver'), ('output_instance_id', 'new-output'),
                ('command_writes', 3), ('feedback_generation', 1), ('receiver_output_fenced', False),
                ('hold_verified', None), ('capabilities', [])]:
            candidate = deepcopy(self.status)
            candidate['execution_facts']['o6'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(candidate)
        candidate = deepcopy(self.status)
        candidate['execution_facts']['controller_instance_id'] = 'new-controller'
        with self.assertRaises(ValueError):
            self.check(candidate)

    def test_lifetime_uses_original_observation_and_never_query_time(self):
        now = datetime.now(timezone.utc)
        evidence = {'observed_at': now.isoformat()}
        self.assertEqual(holding_expiry(evidence, now=now + timedelta(seconds=59)), now + timedelta(seconds=60))
        with self.assertRaises(ValueError):
            holding_expiry(evidence, now=now + timedelta(seconds=60))
        evidence['valid_until'] = (now + timedelta(seconds=300)).isoformat()
        self.assertEqual(holding_expiry(evidence, now=now), now + timedelta(seconds=300))
        for until in ((now + timedelta(seconds=301)).isoformat(), now.replace(tzinfo=None).isoformat(), False):
            with self.subTest(until=until), self.assertRaises(ValueError):
                holding_expiry(dict(evidence, valid_until=until), now=now)

    def test_expiry_does_not_erase_historically_observed_grasp_success(self):
        observed = datetime.now(timezone.utc) - timedelta(minutes=2)
        evidence = dict(source='operator_observation', reference='original-observation',
            observed_at=observed.isoformat(), valid_until=(observed + timedelta(seconds=60)).isoformat(),
            holding='cola_can_1', object_grasped=True, clear_of_fridge=True)
        validate_evidence('grasp_cola', 'cola_can_1', evidence, (observed - timedelta(seconds=1)).isoformat())
        with self.assertRaises(ValueError):
            holding_expiry(evidence)


class HoldingRuntimeTests(unittest.TestCase):
    setUp = body_tests.RuntimeIntegrationTests.setUp
    finish = body_tests.RuntimeIntegrationTests.finish

    def grasp(self):
        self.service.publish('开始接待', 'joint-reception', options={'reflect': False})
        self.finish()
        self.service.control('continue', task_id='joint-reception')
        self.finish()

    def test_expired_current_proof_blocks_delivery_without_replaying_grasp(self):
        self.grasp()
        self.vla.holding_proof['holding_evidence']['invalid_reason'] = 'HOLDING_EVIDENCE_EXPIRED'
        self.vla.holding_proof['holding_evidence']['valid'] = False
        self.service.control('continue', task_id='joint-reception')
        self.assertEqual(self.service.runtime.state, 'recovery_required')
        self.assertEqual(len(self.nav.commands), 1)
        self.assertEqual(len(self.vla.posts), 2)
        self.assertFalse(self.wire.relay['bound'])
        self.assertEqual(self.vla.holding, 'cola_can_1')  # Historical inventory remains.
        self.assertIn('HOLDING_EVIDENCE_EXPIRED', self.service.runtime.record['handoff_observation']['error'])

    def test_expiration_during_prepare_leaves_original_authorization_unresolved(self):
        self.grasp()
        original = self.authority.prepare_navigation
        def expire_after_prepare(request):
            ref = original(request)
            self.vla.holding_proof['holding_evidence']['valid_until'] = '2000-01-01T00:00:00+00:00'
            return ref
        with patch.object(self.authority, 'prepare_navigation', side_effect=expire_after_prepare):
            self.service.control('continue', task_id='joint-reception')
        self.assertEqual(len(self.nav.commands), 1)
        self.assertEqual(self.service.runtime.state, 'recovery_required')
        state = self.authority.store.read()
        self.assertIsNotNone(state['current'])
        row = state['leases'][state['current']]
        self.assertNotIn('business_submission', row)
        self.assertTrue(self.service.runtime.record['command_unknown'])
        self.assertIn('holding_status', self.service.runtime.public_status()['failure_detail']['downstream']['error']['details'])

    def test_confirmed_grasp_from_other_task_is_not_accepted_for_current_delivery(self):
        self.grasp()
        status = self.authority.driver.status('vla')
        with self.assertRaises(ControlError):
            self.authority.check_holding(status, 'other-task', 'cola_can_1')


if __name__ == '__main__':
    unittest.main()
