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


class HoldingRuntimeTests(body_tests.Fixture, unittest.TestCase):
    def test_carrying_task_is_blocked_until_o6_gateway_support_exists(self):
        request=body_tests.nav_request();request['required_holding']='cola_can_1'
        with self.assertRaises(ControlError):self.authority.register(request)
        self.assertFalse(self.wire.calls)

    def test_old_hand_task_pending_never_grants(self):
        original=self.wire.status
        def missing(observation):
            status=original(observation);status['native']['old_hand_task_pending']=True
            return status
        self.wire.status=missing
        with self.assertRaises(ControlError):self.prepare()
        self.assertFalse(self.posts('grant'))
