"""Brain requests takeover explicitly; NAV business admission stays when_idle."""
from copy import deepcopy
import unittest
from contracts.body_control import ControlError
from tests.brain.test_body_control import Fixture


class TakeoverTests(Fixture,unittest.TestCase):
    def test_completed_prepare_with_invalid_current_state_keeps_diagnostics_and_never_executes(self):
        original = self.wire.call
        def invalid_current(service, method, path, payload=None):
            result = original(service, method, path, payload)
            if service == 'nav' and method == 'GET' and result.get('action') == 'prepare' and result.get('state') == 'completed':
                result = deepcopy(result)
                result.update(current_prepare_valid=False,
                    current_prepare_blockers=['PREPARE_BODY_HOLD_UNCONFIRMED'],
                    current_prepare_observation={'body_hold_verified': False, 'valid': False})
            return result
        self.wire.call = invalid_current
        with self.assertRaises(ControlError) as caught:
            self.prepare()
        error = caught.exception
        self.assertEqual(error.code, 'STATIC_ACTIVATION_UNCONFIRMED')
        self.assertIn('身体保持未确认', str(error))
        self.assertEqual(error.details['original_receipt']['state'], 'completed')
        self.assertFalse(error.details['current_prepare_observation']['body_hold_verified'])
        self.assertFalse(self.posts('allow_execute'))

    def test_only_initial_grant_requests_takeover(self):
        request,_=self.prepare()
        self.assertIs(self.posts('grant')[0][3]['takeover'],True)
        self.assertNotIn('takeover',self.posts('allow_execute')[0][3])
        self.assertEqual(self.authority.business_body(request)['dispatch_policy'],'when_idle')

    def test_missing_takeover_capability_is_not_silently_downgraded(self):
        original=self.wire.status
        def without(observation):
            s=original(observation);s['features']=[];return s
        self.wire.status=without
        with self.assertRaises(ControlError):self.prepare()
        self.assertFalse(self.posts('grant'))

    def test_pending_grant_is_queried_not_resent(self):
        original=self.wire.call
        def pending(service,method,path,payload=None):
            result=original(service,method,path,payload)
            if service=='gateway' and method=='POST' and payload.get('operation')=='grant':
                result=deepcopy(result);result.update(state='pending',accepted=False,effect_status='unknown',evidence={})
            return result
        self.wire.call=pending
        self.prepare()
        grant=self.posts('grant')[0][3]
        self.assertEqual(len(self.posts('grant')),1)
        self.assertTrue(any(c[0]=='gateway' and c[1]=='GET' and grant['request_id'] in c[2] for c in self.wire.calls))
