"""Concurrent cancellation and receive-side exit evidence for NAV gateway."""
from copy import deepcopy
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from contracts.body_control import ControlError
from tests.brain.test_body_control import Fixture, nav_request


class ControlCleanupTests(Fixture,unittest.TestCase):
    def test_exit_and_new_session_without_relay(self):
        request,ref=self.prepare()
        result=self.authority.release_control('test-task','nav-1')
        self.assertTrue(result['exit_confirmed'])
        self.assertTrue(result['source_output_fenced'])
        self.assertEqual(len(self.posts('revoke')),1)
        self.assertEqual(len(self.posts('yield')),1)
        time.sleep(.1)
        self.prepare('nav-2')
        self.assertNotEqual(self.posts('grant')[0][3]['input_session_id'],self.posts('grant')[1][3]['input_session_id'])
        self.assertGreater(self.row()['ownership_epoch'],ref['ownership_epoch'])
    def test_incomplete_native_is_not_exit_success(self):
        self.prepare();self.wire.fail_fence=True
        with self.assertRaises(ControlError):self.authority.release_control('test-task','nav-1')
        self.assertTrue(self.authority.recovery_status()['blocked'])
        self.assertTrue(self.row()['revoked'])
    def test_revoke_reaches_gateway_while_source_stop_is_blocked(self):
        self.prepare();self.wire.block_operation='yield'
        future=self.pool.submit(self.authority.release_control,'test-task','nav-1')
        try:
            self.assertTrue(self.wire.entered.wait(1))
            end=time.monotonic()+1
            while not self.posts('revoke') and time.monotonic()<end:time.sleep(.002)
            self.assertTrue(self.posts('revoke'))
            self.assertFalse(future.done())
        finally:self.wire.unblock.set()
        future.result(2)
    def test_cancel_during_grant_does_not_commit_late_permission(self):
        request=nav_request();self.schedule(request);self.wire.block_operation='grant'
        preparing=self.pool.submit(self.authority.prepare_navigation,request)
        self.assertTrue(self.wire.entered.wait(1))
        lease=self.row()['lease_id']
        task=self.store.load_state();task.update(state='cancelled',dispatch_closed=True);self.store.save_state(task)
        self.authority.request_cleanup('test-task',lease)
        try:
            end=time.monotonic()+1
            while not self.posts('revoke') and time.monotonic()<end:time.sleep(.002)
            self.assertTrue(self.posts('revoke'))
        finally:self.wire.unblock.set()
        with self.assertRaises(ControlError):preparing.result(2)
        self.assertFalse(self.posts('allow_execute'))
        self.assertIsNone(self.wire.permit)
        self.assertTrue(self.row()['revoked'])
    def test_missing_source_reply_can_use_full_receiver_exit_without_inventing_source_fact(self):
        self.prepare()
        old=self.driver.yield_navigation
        self.driver.yield_navigation=lambda *a: (_ for _ in ()).throw(ControlError('UNKNOWN','source unreachable'))
        result=self.authority.release_control('test-task','nav-1')
        self.assertTrue(result['exit_confirmed']);self.assertIsNone(result['source_output_fenced'])
    def test_business_cancel_and_duplicate_cleanup_do_not_repost(self):
        request,ref=self.prepare()
        self.authority.record_submission(request,dict(request['body'],control_authorization=ref))
        self.wire.commands['nav-1']=dict(request['body'],state='running')
        self.authority.release_control('test-task','nav-1')
        self.authority.release_control('test-task','nav-1')
        self.assertEqual(len([c for c in self.wire.calls if c[1]=='POST' and c[2]=='/v1/navigation/handoff/yield']),1)
    def test_foreign_task_or_current_owner_cannot_be_cancelled(self):
        self.prepare()
        with self.assertRaises(ControlError):self.authority.release_control('foreign','nav-1')
        self.assertFalse(self.posts('revoke'))
    def test_repeated_current_exit_uses_original_effect_requests_and_keeps_failure(self):
        self.prepare();self.wire.fail_fence=True
        with self.assertRaises(ControlError):self.authority.release_control('test-task','nav-1')
        original=deepcopy(self.authority.store.read()['requests'])
        task=self.store.load_state();task.update(state='cancelled',dispatch_closed=True);self.store.save_state(task)
        lease=self.row()['lease_id']
        self.wire.fail_fence=False
        self.authority.request_cleanup('test-task',lease,new_exit_check=True)
        self.authority._cleanup_future.result(2)
        self.assertEqual(self.row()['state'],'released')
        for key,value in original.items():
            self.assertEqual(value,self.authority.store.read()['requests'][key])
    def test_instance_change_is_not_exit_proof(self):
        self.prepare();self.wire.controller='new-sonic'
        with self.assertRaises(ControlError):self.authority.release_control('test-task','nav-1')
        self.assertTrue(self.authority.recovery_status()['blocked'])


class CleanupApplicationTests(unittest.TestCase):
    def test_explicit_current_exit_check_is_forwarded_without_continuing_task(self):
        from brain.application import BrainApplication
        app = BrainApplication.__new__(BrainApplication)
        app.start = lambda: None; app.body_control = Mock()
        app.recover_control('task','old',new_exit_check=True)
        app.body_control.request_cleanup.assert_called_once_with('task','old',new_exit_check=True)

    def test_runtime_cancel_event_also_cleans_up_confirmed_advice_cancellation(self):
        from brain.application import BrainApplication
        app = BrainApplication.__new__(BrainApplication)
        app.config = {'brain': {'capture_timeline': False}}
        app.owner = Mock(); app._execution_ports = []
        app.body_control = Mock()
        app.body_control.recovery_status.return_value = {'blocked':True,'source':{'lease_id':'old'}}
        with patch('brain.application.emit'):
            app._event('task_cancelled', {'task_id':'task','state':'cancelled'})
        app.body_control.cancel_task.assert_called_once_with('task')

    def test_application_cancel_requests_cleanup_but_status_never_does(self):
        from brain.application import BrainApplication
        app = BrainApplication.__new__(BrainApplication)
        app.start = lambda: None
        app.owner = SimpleNamespace(call=lambda fn,**kw:fn())
        app.service = Mock()
        app.service.runtime = None
        app.service.control.return_value = {'accepted':True,'state':'cancelled','task_id':'task'}
        app.service.status.return_value = {'task_id':'task'}
        app.learning = Mock(); app.learning.result.return_value = None
        app.body_control = Mock()
        app.body_control.recovery_status.return_value = {'blocked':True,'source':{'lease_id':'old'}}
        app.body_control.cancel_task.return_value = {'blocked':False,'pending':False}
        with patch('shared.brain_journal.emit'):
            result = app.control('cancel',task_id='task')
        self.assertFalse(result['control_recovery']['pending'])
        app.body_control.cancel_task.assert_called_once_with('task')
        app.status(); app.status()
        self.assertEqual(app.body_control.cancel_task.call_count, 1)


if __name__ == '__main__': unittest.main()


class ExitWorkerLifecycleTests(unittest.TestCase):
    def test_shutdown_waits_for_threads_to_start_before_join(self):
        import threading
        from brain.workers import WorkerPool
        pool=WorkerPool('exit-start-race',1)
        entered,release,closed=threading.Event(),threading.Event(),threading.Event()
        errors=[];original=threading.Thread.start
        def start(thread):
            if thread.name.startswith('exit-start-race'):
                entered.set();release.wait(2)
            return original(thread)
        def close():
            try:pool.close()
            except Exception as exc:errors.append(exc)
            finally:closed.set()
        with patch.object(threading.Thread,'start',start):
            starter=threading.Thread(target=pool.start);starter.start()
            self.assertTrue(entered.wait(1))
            stopper=threading.Thread(target=close);stopper.start()
            try:self.assertFalse(closed.wait(.05))
            finally:release.set();starter.join(2);stopper.join(2)
        self.assertFalse(errors)
        self.assertTrue(closed.is_set())


class CrossRestartExitTests(Fixture, unittest.TestCase):
    def prepare_restart_proof(self):
        self.prepare();self.authority._stop.set();row=self.row()
        self.authority._mark_revoked(row['lease_id'])
        self.wire.last=deepcopy(self.wire.permit);self.wire.permit=None
        self.wire.instance='gateway-restarted';self.wire.controller='new-controller'
        self.wire.active_session=None;self.wire.isolated=True
        original=self.wire.status
        def restarted(observation):
            s=original(observation)
            s['installed_at_boot_without_permit']=True
            s['native'].update(native_binding={},request_pending=False)
            s['native']['sample']['local_stop_pending']=False
            s['exit_status'].update(confirmation_basis='old_receiver_process_absent_and_current_hold',
                                   old_controller_absent=True,current_controller_instance_id='new-controller')
            return s
        self.wire.status=restarted
        return row

    def test_verified_old_process_absence_and_fresh_new_hold_can_release(self):
        row=self.prepare_restart_proof()
        proof=self.driver.verify_exit(row,{},self.wire.status('fresh'))
        self.assertTrue(proof['exit_confirmed']);self.assertIsNone(proof['source_output_fenced'])

    def test_recovery_consumes_current_proof_without_sending_to_old_instance(self):
        row=self.prepare_restart_proof()
        with self.authority.store.writer():
            s=self.authority.store.read()
            s['requests']['prior-exit']={'body':{'lease_id':row['lease_id'],'action':'exit'},
                'receipt':{'state':'unknown'},'children':{}}
            self.authority._save(s)
        task=self.store.load_state();task['state']='cancelled';self.store.save_state(task)
        count=len([c for c in self.wire.calls if c[1]=='POST'])
        self.authority.request_cleanup('test-task',row['lease_id'],new_exit_check=True)
        self.authority._cleanup_future.result(2)
        self.assertTrue(self.authority.released('test-task','nav-1'))
        self.assertEqual(count,len([c for c in self.wire.calls if c[1]=='POST']))
        self.assertEqual(self.authority.store.read()['requests']['prior-exit']['receipt']['state'],'unknown')

    def test_restart_alone_foreign_binding_or_pending_effect_never_releases(self):
        row=self.prepare_restart_proof()
        for change in (lambda s:s['exit_status'].update(old_controller_absent=False),
                       lambda s:s['exit_status'].update(current_controller_instance_id='foreign'),
                       lambda s:s['native'].update(native_binding={'command_id':'foreign'}),
                       lambda s:s['native'].update(request_pending=True),
                       lambda s:s['native']['sample'].update(local_stop_pending=True),
                       lambda s:s.update(installed_at_boot_without_permit=False),
                       lambda s:s['native'].update(old_hand_task_pending=True)):
            s=self.wire.status('check-'+str(id(change)));change(s)
            with self.assertRaises(ControlError):self.driver.verify_exit(row,{},s)
