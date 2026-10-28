"""Original POST errors survive query failures; no uncertain effect is replayed."""
from copy import deepcopy
import unittest
from brain.adapters.http_client import HttpContractError
from contracts.body_control import VERSION, REVISION, ControlError, nav_prepare_refused
from tests.brain.test_body_control import Fixture, nav_request


class HandoffHTTPFailureTests(Fixture,unittest.TestCase):
    def reject(self,code='NAV_NOT_YIELDED',status=409):
        original=self.wire.call
        response=dict(contract_version=VERSION,coordination_revision=REVISION,accepted=False,
                      error={'code':code,'message':'first failure'})
        def call(service,method,path,body=None):
            if service=='nav' and method=='POST' and path.endswith('/handoff/prepare'):
                self.wire.calls.append((service,method,path,deepcopy(body)))
                raise HttpContractError('first failure',status_code=status,payload=response)
            return original(service,method,path,body)
        self.wire.call=call
        with self.assertRaises(ControlError) as raised:self.prepare()
        op=next(o for o in self.authority.store.read()['requests'].values() if o['body']['action']=='acquire')
        return raised.exception,op
    def test_original_refusal_is_saved_without_404_overwriting_it(self):
        error,op=self.reject()
        first=op['children']['nav-prepare']['observations']['first_post']
        self.assertEqual(first['status_code'],409)
        self.assertEqual(error.details['first_post'],first)
        self.assertTrue(error.details['nav_prepare_not_accepted'])
    def test_conflict_and_500_never_prove_nonexecution(self):
        for code,status in [('REQUEST_CONFLICT',409),('NAV_NOT_YIELDED',500)]:
            with self.subTest(code=code,status=status):
                wire=dict(service='nav',path='/prepare',payload={'action':'prepare'})
                post=dict(method='POST',path='/prepare',status_code=status,payload=dict(
                    contract_version=VERSION,coordination_revision=REVISION,accepted=False,error={'code':code}))
                self.assertFalse(nav_prepare_refused(wire,post))
    def test_lost_reply_then_missing_query_remains_unknown(self):
        error,op=self.reject('INTERNAL_ERROR',503)
        observations=op['children']['nav-prepare']['observations']
        self.assertEqual(observations['first_post']['status_code'],503)
        self.assertEqual(observations['last_query']['status_code'],404)
        self.assertFalse(error.details['nav_prepare_not_accepted'])
        self.assertEqual(len(self.posts('prepare')),1)
        self.assertTrue(self.authority.recovery_status()['blocked'])
    def test_lost_prepare_reply_recovers_same_session_without_reposting(self):
        self.wire.drop='prepare';self.prepare()
        self.assertEqual(len(self.posts('prepare')),1)
        self.assertEqual(self.wire.session,1)
    def test_mismatching_permit_receipt_blocks_business(self):
        self.wire.corrupt='allow_execute'
        with self.assertRaises(ControlError):self.prepare()
        self.assertNotIn('business_submission',self.row())
    def test_management_timeout_does_not_revoke_existing_session(self):
        self.prepare()
        def unavailable(*args):raise HttpContractError('management link interrupted')
        self.driver.status=unavailable
        request=nav_request('nav-2');self.schedule(request)
        # Mark the old command held to exercise new-command admission, not busy rejection.
        with self.authority.store.writer():
            state=self.authority.store.read();state['leases'][self.row()['lease_id']]['active_command_id']=None
            self.authority._save(state)
        with self.assertRaises(HttpContractError):self.authority.prepare_navigation(request)
        self.assertFalse(self.row().get('revoked'))
        self.assertEqual(len(self.posts('grant')),1);self.assertFalse(self.posts('revoke'))
