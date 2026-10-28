"""Whole-task cancellation detaches local flow, never claims physical exit."""
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch
from brain.kernel.body_control import BodyControl
from brain.adapters.http_client import HttpContractError
from contracts.body_control import ControlError
from tests.brain.test_body_control import Fixture,nav_request


class DetachedCancelTests(Fixture,unittest.TestCase):
    def cancel_task(self):
        task=self.store.load_state();task.update(state='cancelled',dispatch_closed=True,control_request='cancel')
        self.store.save_state(task)
        return self.authority.cancel_task(task['task_id'])

    def test_unknown_exit_does_not_block_new_registration_and_is_not_fake_release(self):
        self.prepare();old=self.row()['lease_id']
        self.driver.yield_navigation=lambda *_: (_ for _ in ()).throw(HttpContractError('source offline'))
        self.driver.status=lambda *_: (_ for _ in ()).throw(HttpContractError('status offline'))
        status=self.cancel_task()
        self.assertFalse(status['blocked']);self.assertFalse(status['pending'])
        self.authority._cleanup_futures[old].result(2)
        state=self.authority.store.read();self.assertIsNone(state['current'])
        self.assertNotEqual(state['leases'][old]['state'],'released')
        self.assertFalse(state['leases'][old]['physical_exit_confirmed'])
        self.authority.assert_new_task_allowed()
        new=nav_request('nav-2');new['body']['task_id']='task-2';self.schedule(new)
        row=self.authority.register(new)
        self.assertNotEqual(row['lease_id'],old)
        self.assertGreater(row['ownership_epoch'],state['leases'][old]['ownership_epoch'])

    def test_late_exit_cannot_clear_new_owner_or_busy_request(self):
        self.prepare();old=self.row()['lease_id']
        self.wire.block_operation='yield'
        try:
            self.cancel_task();self.assertTrue(self.wire.entered.wait(1))
            new=nav_request('nav-2');new['body']['task_id']='task-2';self.schedule(new)
            row=self.authority.register(new)
            with self.authority.store.writer():
                state=self.authority.store.read();state['current']=row['lease_id'];state['busy']='new-prepare'
                state['requests']['new-prepare']={'body':{'lease_id':row['lease_id']},'receipt':{'state':'pending'},'children':{}}
                self.authority._save(state)
            self.wire.unblock.set();self.authority._cleanup_futures[old].result(2)
            state=self.authority.store.read()
            self.assertEqual(state['current'],row['lease_id']);self.assertEqual(state['busy'],'new-prepare')
            self.assertEqual(state['leases'][old]['state'],'released')
        finally:self.wire.unblock.set()

    def test_cancel_during_grant_then_new_registration_late_reply_never_reclaims_owner(self):
        r=nav_request();self.schedule(r);self.wire.block_operation='grant'
        pending=self.pool.submit(self.authority.prepare_navigation,r)
        try:
            self.assertTrue(self.wire.entered.wait(1));old=self.row()['lease_id']
            self.cancel_task();self.authority.assert_new_task_allowed()
            new=nav_request('nav-2');new['body']['task_id']='task-2';self.schedule(new)
            row=self.authority.register(new)
            self.wire.unblock.set()
            with self.assertRaises(ControlError):pending.result(2)
            self.authority._cleanup_futures[old].result(2)
            state=self.authority.store.read()
            self.assertNotEqual(state['current'],old)
            self.assertFalse(self.posts('allow_execute'))
            self.assertTrue(state['leases'][old]['task_cancelled'])
            self.assertFalse(state['leases'][row['lease_id']].get('revoked'))
        finally:self.wire.unblock.set()

    def test_cancelling_new_planning_task_also_detaches_retained_previous_control(self):
        self.prepare();old=self.row()['lease_id']
        task=self.store.load_state();task.update(task_id='new-planning-task',state='cancelled',dispatch_closed=True)
        self.store.save_state(task)
        status=self.authority.cancel_task('new-planning-task')
        self.assertFalse(status['blocked']);self.assertFalse(status['occupied'])
        self.assertEqual(self.authority.store.read()['leases'][old]['cancelled_by_task_id'],'new-planning-task')
        self.authority._cleanup_futures[old].result(2)

    def test_restart_does_not_restore_detached_block(self):
        self.prepare();self.wire.fail_fence=True
        self.cancel_task();old=self.row()['lease_id'];self.authority._cleanup_futures[old].result(2)
        self.authority.close()
        other=BodyControl(self.config,self.root,self.driver,self.pool);self.addCleanup(other.close)
        other.start();other.assert_new_task_allowed()
        self.assertFalse(other.recovery_status()['blocked'])
        self.assertIsNone(other.store.read()['current'])

    def test_repeated_cancel_is_idempotent_and_stale_task_cannot_detach_new(self):
        self.prepare();self.cancel_task();old=self.row()['lease_id'];self.authority._cleanup_futures[old].result(2)
        count=len(self.posts('revoke'))
        self.cancel_task();self.assertEqual(len(self.posts('revoke')),count)
        new=nav_request('nav-2');new['body']['task_id']='task-2';self.schedule(new)
        self.authority.prepare_navigation(new)
        with self.assertRaises(ControlError):self.authority.cancel_task('test-task')
        self.assertEqual(self.authority.recovery_status()['source']['task_id'],'task-2')
