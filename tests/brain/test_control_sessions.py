"""Persistent ownership, independent commands, cancellation and migration boundaries."""
from copy import deepcopy
import json
import time
import unittest
from contracts.body_control import ControlError
from brain.adapters.http_client import HttpContractError
from brain.storage.body_control import ControlStore
from scripts.migrate_body_control_gateway import migrate_session
from tests.brain.test_body_control import Fixture, nav_request
from contracts.body_control import stamp


class ControlSessionTests(Fixture,unittest.TestCase):
    def submit_and_hold(self, command='nav-1', task='test-task'):
        r=nav_request(command);r['body']['task_id']=task;self.schedule(r)
        self.authority.prepare_navigation(r);body=self.authority.business_body(r)
        self.authority.record_submission(r,body)
        raw=dict(body,state='succeeded',completed_at=stamp(),result=dict(navigation_stopped=True,reached=True))
        raw['current_hold']=self.wire.hold(raw);self.wire.commands[command]=raw
        self.authority.settle_navigation(task,command,raw)
        return r,body,raw

    def test_two_tasks_use_same_session_and_increasing_command_seq(self):
        _,a,_=self.submit_and_hold()
        _,b,_=self.submit_and_hold('nav-2','different-task')
        self.assertEqual(a['control_session_id'],b['control_session_id'])
        self.assertEqual(a['control_authorization'],b['control_authorization'])
        self.assertEqual((a['command_seq'],b['command_seq']),(1,2))
        self.assertEqual(len(self.posts('prepare')),1);self.assertEqual(len(self.posts('grant')),1)
        self.assertFalse(self.posts('renew'));self.assertFalse(self.posts('revoke'))
        self.assertFalse(self.authority.released('test-task','nav-1'))
        self.assertFalse(self.authority.recovery_status()['blocked'])
        self.assertTrue(self.authority.recovery_status()['occupied'])

    def test_old_hold_cannot_clear_new_active_command(self):
        _,_,old=self.submit_and_hold()
        r=nav_request('nav-2');self.schedule(r);self.authority.prepare_navigation(r)
        self.authority.settle_navigation('test-task','nav-1',old)
        self.assertEqual(self.row()['active_command_id'],'nav-2')
        raw=deepcopy(old);raw['current_hold']['applicable']=False
        with self.assertRaises(ControlError):self.authority.settle_navigation('test-task','nav-1',raw)

    def test_unknown_stop_or_wrong_command_cannot_admit_next(self):
        r,_=self.prepare();body=self.authority.business_body(r);self.authority.record_submission(r,body)
        raw=dict(body,state='succeeded',result=dict(navigation_stopped=True))
        raw['current_hold']=self.wire.hold(raw);raw['current_hold']['identity']['command_seq']=999
        with self.assertRaises(ControlError):self.authority.settle_navigation('test-task','nav-1',raw)
        r=nav_request('nav-2');self.schedule(r)
        with self.assertRaises(ControlError):self.authority.prepare_navigation(r)

    def test_command_cancel_pending_queries_original_request_no_repost(self):
        _,body,_=self.submit_and_hold()
        original=self.wire.call
        posts=[]
        def call(service,method,path,payload=None):
            value=original(service,method,path,payload)
            if method=='POST' and path.endswith('/cancel'):
                posts.append(payload);value=dict(value,state='pending',accepted=False)
            return value
        self.wire.call=call
        self.authority.cancel_navigation('test-task','nav-1')
        self.authority.cancel_navigation('test-task','nav-1')
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0]['cancel_scope'],'command')
        self.assertEqual(posts[0]['command_seq'],body['command_seq'])
        self.assertFalse(self.posts('revoke'));self.assertTrue(self.wire.permit)

    def test_old_command_cannot_release_new_session_activity(self):
        self.submit_and_hold()
        r=nav_request('nav-2');self.schedule(r);self.authority.prepare_navigation(r)
        with self.assertRaises(ControlError):self.authority.release_control('test-task','nav-1')
        self.assertFalse(self.posts('revoke'))

    def test_revoke_known_binding_does_not_wait_for_status_query(self):
        self.prepare()
        self.driver.status=lambda *_: (_ for _ in ()).throw(HttpContractError('status unavailable'))
        self.authority._permit(self.row()['lease_id'],'revoke')
        self.assertEqual(len(self.posts('revoke')),1)
        self.assertIsNone(self.wire.permit)

    def test_rejected_renew_does_not_send_anything(self):
        self.prepare()
        with self.assertRaises(ControlError):self.authority._permit(self.row()['lease_id'],'renew')
        self.assertFalse(self.posts('renew'))

    def test_session_migration_retains_original_records_and_epoch_floor(self):
        self.prepare();self.authority.release_control('test-task','nav-1');self.authority.close()
        store=ControlStore(self.root);old=store.read();old['schema']=2;store.write(old)
        raw=store.path.read_bytes()
        receipt=dict(migration_id='peer-migration',minimum_next_ownership_epoch=100,
                     permission_restored=False,original_results_preserved=True,exit_evidence={'exit_confirmed':True})
        preview=migrate_session(self.root,receipt)
        self.assertEqual(store.path.read_bytes(),raw)
        result=migrate_session(self.root,receipt,apply=True)
        updated=store.read()
        self.assertEqual(updated['requests'],old['requests']);self.assertEqual(updated['leases'],old['leases'])
        self.assertEqual(updated['epoch'],99);self.assertEqual(updated['schema'],3)
        from pathlib import Path
        self.assertEqual(Path(result['backup']).read_bytes(),raw)

    def test_live_lock_and_unresolved_state_prevent_migration(self):
        self.prepare()
        receipt=dict(migration_id='peer',minimum_next_ownership_epoch=3,permission_restored=False,
                     original_results_preserved=True,exit_evidence={'exit_confirmed':True})
        with self.assertRaises(BlockingIOError):migrate_session(self.root,receipt,apply=True)
        self.authority.close();store=ControlStore(self.root);old=store.read();old['schema']=2;store.write(old)
        before=store.path.read_bytes()
        with self.assertRaises(ValueError):migrate_session(self.root,receipt,apply=True)
        self.assertEqual(before,store.path.read_bytes())

    def test_matching_migration_exit_can_release_unknown_without_rewriting_requests(self):
        self.prepare();self.authority._mark_revoked(self.row()['lease_id'])
        self.authority.close();store=ControlStore(self.root);old=store.read();old['schema']=2;store.write(old)
        row=next(iter(old['leases'].values()))
        proof={k:True for k in ('exit_confirmed','source_output_fenced','body_forwarding_fenced',
            'receiver_callbacks_sealed','receiver_decoded_caches_cleared','input_session_retired','body_stopped','body_hold_verified')}
        proof['confirmation_basis']='old_controller_absent_new_unused_hold'
        receipt=dict(migration_id='peer-verified',minimum_next_ownership_epoch=20,permission_restored=False,
            original_results_preserved=True,exit_evidence=proof,previous_gateway_state=deepcopy(row['gateway_binding']))
        # Synthetic legacy identity: old short-permit ledger included business keys.
        for k in ('task_id','command_id'):receipt['previous_gateway_state'][k]=row[k]
        old['leases'][row['lease_id']]['gateway_binding']=deepcopy(receipt['previous_gateway_state']);store.write(old)
        before=store.path.read_bytes()
        bad=deepcopy(receipt);bad['previous_gateway_state']['input_session_id']='foreign'
        with self.assertRaises(ValueError):migrate_session(self.root,bad,apply=True)
        self.assertEqual(before,store.path.read_bytes())
        migrate_session(self.root,receipt,apply=True)
        current=store.read()
        self.assertEqual(current['requests'],old['requests'])
        self.assertEqual(current['leases'][row['lease_id']]['migration_original_evidence'],row['evidence'])
        self.assertEqual(current['leases'][row['lease_id']]['state'],'released')
        self.assertIsNone(current['current'])

    def test_staged_migration_applies_code_without_clearing_unknown_occupancy(self):
        self.prepare();self.authority.close();store=ControlStore(self.root)
        old=store.read();old['schema']=2;store.write(old)
        result=migrate_session(self.root,apply=True,preserve_unresolved=True)
        current=store.read()
        self.assertTrue(result['unresolved_preserved'])
        self.assertEqual(current['current'],old['current'])
        self.assertEqual(current['requests'],old['requests'])
        self.assertTrue(current['leases'][current['current']]['legacy_control'])
        from brain.kernel.body_control import BodyControl
        other=BodyControl(self.config,self.root,self.driver,self.pool);self.addCleanup(other.close)
        other.start()
        self.assertTrue(other.recovery_status()['blocked'])
        with self.assertRaises(ControlError):other.assert_new_task_allowed()
