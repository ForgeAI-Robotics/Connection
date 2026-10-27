"""New current-exit requests preserve old unknown effects and exact identities."""
from copy import deepcopy
import unittest

from brain.adapters.http_client import HttpContractError
from contracts.body_control import VERSION, REVISION, ControlError
from tests.brain import test_body_control as fixture


class CurrentExitTests(unittest.TestCase):
    def setUp(self):
        fixture.AuthorityTests.setUp(self)
        self.wire.relay.update(enabled=False,recovery_required=False,local_subscriber_queue_closed=True)
    schedule = fixture.AuthorityTests.schedule

    def receipt(self, *, observation=False):
        request=fixture.nav_request();self.schedule(request)
        row=self.authority.register(request);row['ownership_epoch']=6
        payload=dict(self.driver.payload(row,'yield'),request_id='current-exit')
        association={k:deepcopy(payload[k]) for k in ('task_id','command_id','control_authorization')}
        evidence=dict(navigation_stopped=True,output_fenced=True,hold_verified=True,
            mode_idle_hold_fenced=True,native_receiver_sealed=True)
        if observation:
            previous=deepcopy(association);previous['command_id']='old-command'
            previous['control_authorization']['ownership_epoch']=5
            evidence.update(observation_only=True,stop_command_sent=False,
                original_request_outcome='unknown',requested_association=association,observed_association=previous)
        result=dict(association,contract_version=VERSION,coordination_revision=REVISION,
            request_id='current-exit',action='yield',state='completed',accepted=True,evidence=evidence,
            execution_authority=False,historical=False,effect_status='confirmed',
            current_local_state='yielded',current_local_association=None)
        return result,payload

    def test_normal_yield_with_cleared_local_association_is_valid(self):
        result,payload=self.receipt()
        view=self.driver.operation_receipt(result,payload,'nav')
        self.assertTrue(view['navigation_stopped'])
        self.assertTrue(view['output_fenced'])

    def test_observation_only_yield_preserves_unknown_original_outcome(self):
        result,payload=self.receipt(observation=True)
        view=self.driver.operation_receipt(result,payload,'nav')
        self.assertEqual(view['evidence']['original_request_outcome'],'unknown')
        self.assertFalse(view['evidence']['stop_command_sent'])

    def test_wrong_current_requested_or_observed_identity_is_rejected(self):
        result,payload=self.receipt(observation=True)
        for field in ('requested_association','observed_association'):
            with self.subTest(field=field):
                changed=deepcopy(result)
                changed['evidence'][field]['control_authorization']['authority_id']='foreign'
                with self.assertRaises(ControlError):self.driver.operation_receipt(changed,payload,'nav')
        for field,value in [('historical',True),('current_local_state','prepared'),
                            ('current_local_association',{'command_id':'other'})]:
            with self.subTest(field=field):
                changed=deepcopy(result);changed[field]=value
                with self.assertRaises(ControlError):self.driver.operation_receipt(changed,payload,'nav')

    def test_cleared_association_without_exit_facts_is_rejected(self):
        result,payload=self.receipt(observation=True)
        for key in ('hold_verified','output_fenced','navigation_stopped','mode_idle_hold_fenced','native_receiver_sealed'):
            with self.subTest(key=key):
                changed=deepcopy(result);changed['evidence'][key]=False
                with self.assertRaises(ControlError):self.driver.operation_receipt(changed,payload,'nav')

    def test_new_exit_request_keeps_old_unknown_yield_and_does_not_reprepare(self):
        request=fixture.nav_request();self.schedule(request)
        grant=self.authority.prepare_navigation(request);lease=grant['lease_id']
        task=self.store.load_state();task.update(state='cancelled',dispatch_closed=True);self.store.save_state(task)
        original=self.wire.call; old_ids=[]
        def unavailable_first_yield(service,method,path,payload=None):
            if service=='nav' and method=='POST' and payload.get('action')=='yield' and not old_ids:
                old_ids.append(payload['request_id'])
                self.wire.calls.append((service,method,path,deepcopy(payload)))
                raise HttpContractError('lost old reply')
            if method=='GET' and any(path.endswith(rid) for rid in old_ids):
                raise HttpContractError('unknown request',status_code=404,payload={'error':{'code':'UNKNOWN_REQUEST'}})
            return original(service,method,path,payload)
        self.wire.call=unavailable_first_yield
        self.authority.request_cleanup('test-task',lease)
        self.authority._cleanup_future.result(timeout=2)
        before=deepcopy(self.authority.store.read()['requests'])
        self.assertTrue(self.authority.recovery_status()['blocked'])
        self.wire.drop_action='yield'  # New exit reply lost: recover by original GET, no duplicate POST.
        self.authority.request_cleanup('test-task',lease,new_exit_check=True)
        self.authority._cleanup_future.result(timeout=2)
        after=self.authority.store.read()
        self.assertFalse(self.authority.recovery_status()['blocked'])
        for rid,op in before.items():self.assertEqual(after['requests'][rid],op)
        actions=[body for _,method,_,body in self.wire.calls if method=='POST']
        self.assertEqual(sum(body['action']=='prepare' for body in actions),1)
        yields=[body for body in actions if body['action']=='yield']
        self.assertEqual(len(yields),2)
        self.assertNotEqual(yields[0]['request_id'],yields[1]['request_id'])
        self.assertEqual(yields[0]['control_authorization'],yields[1]['control_authorization'])

    def test_repeated_new_exit_checks_do_not_generate_unbounded_requests(self):
        request=fixture.nav_request();self.schedule(request)
        grant=self.authority.prepare_navigation(request);lease=grant['lease_id']
        task=self.store.load_state();task.update(state='cancelled',dispatch_closed=True);self.store.save_state(task)
        original=self.wire.call
        def unknown_yield(service,method,path,payload=None):
            if service=='nav' and method=='POST' and payload.get('action')=='yield':
                self.wire.calls.append((service,method,path,deepcopy(payload)))
                raise HttpContractError('lost response')
            if service=='nav' and '/requests/' in path:
                raise HttpContractError('not found',status_code=404)
            return original(service,method,path,payload)
        self.wire.call=unknown_yield
        for _ in range(2):
            self.authority.request_cleanup('test-task',lease,new_exit_check=True)
            self.authority._cleanup_future.result(timeout=2)
        self.assertTrue(self.authority.recovery_status()['blocked'])
        self.assertEqual(sum(method=='POST' and body.get('action')=='yield'
            for _,method,_,body in self.wire.calls if body),1)


if __name__=='__main__':unittest.main()
