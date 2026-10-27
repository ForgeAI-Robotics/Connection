"""First handoff replies stay durable; query failures never prove non-execution."""
from copy import deepcopy
import unittest

from brain.adapters.body_control import ControlledBodyAdapter
from brain.adapters.http_client import HttpContractError
from brain.kernel.body_control import BodyControl
from contracts.body_control import VERSION, REVISION, ControlError, nav_prepare_refused
from tests.brain import test_body_control as fixture


class HandoffHTTPFailureTests(unittest.TestCase):
    setUp = fixture.AuthorityTests.setUp
    schedule = fixture.AuthorityTests.schedule

    def fail_prepare(self, code='NAV_NOT_YIELDED', status=409, *, payload=None, after_effect=False):
        response = payload if payload is not None else dict(contract_version=VERSION,
            coordination_revision=REVISION, accepted=False, error={'code':code, 'message':'original rejection'})
        self.response = deepcopy(response)
        self.wire.nav.update(navigation_stopped=True, brain_control={
            'operation_pending':False, 'control_association':{'command_id':'old-nav'}})
        self.wire.relay.update(enabled=False, recovery_required=False,
            local_subscriber_queue_closed=True, relay_instance_id='relay-test')
        original = self.wire.call
        def call(service, method, path, body=None):
            if service == 'nav' and method == 'POST' and body['action'] == 'prepare':
                if after_effect:
                    original(service, method, path, body)
                else:
                    self.wire.calls.append((service, method, path, deepcopy(body)))
                raise HttpContractError('original rejection', status_code=status, payload=deepcopy(response))
            if '/requests/' in path and path.rsplit('/',1)[1] not in self.wire.operations:
                self.wire.calls.append((service, method, path, deepcopy(body)))
                raise HttpContractError('not found', status_code=404, payload=dict(
                    contract_version=VERSION, coordination_revision=REVISION, accepted=False,
                    error={'code':'UNKNOWN_REQUEST','message':'Unknown handoff request'}))
            if service == 'relay' and method == 'POST':
                self.wire.relay['enabled'] = body['action'] == 'enable_navigation'
            return original(service, method, path, body)
        self.wire.call = call
        self.request = fixture.nav_request(); self.schedule(self.request)

    def attempt(self):
        with self.assertRaises(ControlError) as raised:
            self.authority.prepare_navigation(self.request)
        state = self.authority.store.read(); self.lease = state['current']
        self.acquire_id = next(k for k,v in state['requests'].items() if v['body']['action']=='acquire')
        return raised.exception, state['requests'][self.acquire_id]

    def test_explicit_refusal_is_durable_and_not_replaced_by_404(self):
        self.fail_prepare()
        error, op = self.attempt()
        first = op['children']['nav-prepare']['observations']['first_post']
        self.assertEqual(first['status_code'], 409)
        self.assertEqual(first['payload'], self.response)
        self.assertEqual(error.code, 'HANDOFF_REJECTED')
        self.assertEqual(error.details['first_post'], first)
        self.assertEqual(op['receipt']['error']['details']['first_post'], first)
        self.assertTrue(error.details['nav_prepare_not_accepted'])
        self.assertFalse(any(service=='nav' and method=='GET' and '/requests/' in path
            for service,method,path,_ in self.wire.calls))

    def test_refused_prepare_closes_only_current_relay_and_preserves_failure(self):
        self.fail_prepare(); _, before = self.attempt()
        task = self.store.load_state(); task.update(state='cancelled', dispatch_closed=True)
        self.store.save_state(task)
        self.authority.request_cleanup('test-task', self.lease)
        self.authority._cleanup_future.result(timeout=2)
        state = self.authority.store.read()
        self.assertFalse(self.authority.recovery_status()['blocked'])
        self.assertEqual(state['requests'][self.acquire_id], before)
        self.assertTrue(state['leases'][self.lease]['evidence']['navigation_prepare_not_accepted'])
        actions = [body['action'] for _,method,_,body in self.wire.calls if method=='POST']
        self.assertEqual(actions, ['enable_navigation','prepare','close'])

    def test_refusal_cannot_retire_an_active_nav_source(self):
        self.fail_prepare(); self.attempt()
        self.wire.nav['output_fenced'] = False
        task=self.store.load_state();task['state']='cancelled';self.store.save_state(task)
        self.authority.request_cleanup('test-task', self.lease)
        self.authority._cleanup_future.result(timeout=2)
        self.assertTrue(self.authority.recovery_status()['blocked'])
        self.assertFalse(any(body and body.get('action')=='yield' for _,_,_,body in self.wire.calls))

    def test_generic_rejection_and_missing_query_preserve_both_without_exit_proof(self):
        self.fail_prepare(code='NEW_NAV_REJECTION')
        error, op = self.attempt()
        observations = op['children']['nav-prepare']['observations']
        self.assertEqual(observations['first_post']['payload'], self.response)
        self.assertEqual(observations['last_query']['status_code'], 404)
        self.assertGreater(observations['query_count'], 0)
        self.assertEqual(error.details['request_outcome'], 'rejected')
        self.assertFalse(error.details['nav_prepare_not_accepted'])
        self.assertIn('NEW_NAV_REJECTION', str(error))
        self.assertIsNone(self.authority._relay_only_prepare(self.authority.store.read(),
            self.authority.store.read()['leases'][self.lease]))

    def test_connection_failure_then_404_is_unknown(self):
        self.fail_prepare(status=None, payload='connection lost')
        error, op = self.attempt()
        self.assertEqual(error.code, 'HANDOFF_UNCONFIRMED')
        self.assertEqual(error.details['request_outcome'], 'unknown')
        self.assertIsNone(op['children']['nav-prepare']['observations']['first_post']['status_code'])
        self.assertFalse(error.details['nav_prepare_not_accepted'])

    def test_500_with_a_refusal_code_is_still_unknown(self):
        self.fail_prepare(status=500)
        error, _ = self.attempt()
        self.assertEqual(error.details['request_outcome'], 'unknown')
        self.assertFalse(error.details['nav_prepare_not_accepted'])

    def test_request_conflict_never_proves_no_previous_execution(self):
        self.fail_prepare(code='REQUEST_CONFLICT')
        error, _ = self.attempt()
        self.assertFalse(error.details['nav_prepare_not_accepted'])

    def test_lost_error_reply_after_effect_recovers_by_query_without_reposting(self):
        self.fail_prepare(status=500, after_effect=True)
        result = self.authority.prepare_navigation(self.request)
        state = self.authority.store.read()
        child = next(iter(state['requests'].values()))['children']['nav-prepare']
        self.assertTrue(result['lease_id'])
        self.assertEqual(child['state'], 'confirmed')
        self.assertEqual(child['observations']['first_post']['status_code'], 500)
        self.assertEqual(child['observations']['last_query']['payload']['state'], 'succeeded')
        self.assertEqual(sum(method=='POST' and body.get('action')=='prepare'
            for _,method,_,body in self.wire.calls if body), 1)

    def test_restart_and_query_only_failure_keep_original_post(self):
        self.fail_prepare(code='NEW_NAV_REJECTION'); _, op = self.attempt()
        wire = op['children']['nav-prepare']['wire']
        original = deepcopy(op['children']['nav-prepare']['observations']['first_post'])
        self.authority.close()
        restored = BodyControl(self.config, self.root, self.driver, fixture.Immediate())
        self.addCleanup(restored.close); restored.start()
        # Requery the original acquire child, using the exact durable wire.
        task=self.store.load_state();task.update(state='running', dispatch_closed=False);self.store.save_state(task)
        calls = len(self.wire.calls)
        with self.assertRaises(ControlError) as caught:
            restored.exchange(self.acquire_id, 'nav-prepare', wire['service'], wire['path'],
                {k:v for k,v in wire['payload'].items() if k!='request_id'}, '/v1/navigation/handoff/requests/{request_id}')
        self.assertEqual(caught.exception.details['first_post'], original)
        self.assertFalse(any(method=='POST' for _,method,_,_ in self.wire.calls[calls:]))
        self.assertEqual(restored.store.read()['requests'][self.acquire_id]['children']['nav-prepare']
            ['observations']['first_post'], original)

    def test_wrong_echoed_identity_does_not_prove_refusal_before_effects(self):
        self.fail_prepare()
        self.response['command_id']='wrong-command'
        # Test the same source body via the shared contract classifier.
        wire={'service':'nav','path':'/prepare','payload':dict(self.request['body'],action='prepare')}
        self.assertFalse(nav_prepare_refused(wire, {'method':'POST','path':'/prepare',
            'status_code':409,'payload':self.response}))

    def test_prepare_sent_before_diagnostic_support_remains_unknown(self):
        self.fail_prepare(); _, op = self.attempt()
        state=self.authority.store.read()
        state['requests'][self.acquire_id]['children']['nav-prepare'].pop('observations')
        self.assertIsNone(self.authority._relay_only_prepare(state,state['leases'][self.lease]))

    def test_conflicting_success_receipt_prevents_using_prior_refusal_for_cleanup(self):
        self.fail_prepare(); self.attempt()
        state=self.authority.store.read()
        state['requests'][self.acquire_id]['children']['nav-prepare']['state']='confirmed'
        self.assertIsNone(self.authority._relay_only_prepare(state,state['leases'][self.lease]))

    def test_prepare_refusal_with_positive_effect_flag_is_not_exit_proof(self):
        self.fail_prepare()
        value=dict(self.response, action_started=True)
        wire={'service':'nav','path':'/prepare','payload':dict(self.request['body'],action='prepare')}
        self.assertFalse(nav_prepare_refused(wire, {'method':'POST','path':'/prepare',
            'status_code':409,'payload':value}))

    def test_malformed_error_payload_is_not_a_proven_refusal(self):
        self.fail_prepare(payload={'accepted':False,'error':'bad response'})
        error, _=self.attempt()
        self.assertFalse(error.details['nav_prepare_not_accepted'])

    def test_adapter_exposes_original_http_diagnostic(self):
        self.fail_prepare()
        adapter=ControlledBodyAdapter(None, None, self.authority)
        result=adapter.submit(self.request['body']['command_id'],self.request)
        self.assertTrue(result['unclear'])  # relay is still open until cleanup.
        self.assertEqual(result['raw']['error']['details']['first_post']['payload'], self.response)


if __name__ == '__main__':
    unittest.main()
