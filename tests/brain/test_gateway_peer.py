"""Opt-in actual gateway core with injected native/NAV facts; never opens robot sockets."""
import importlib
import os
from pathlib import Path
import sys
from copy import deepcopy
import time
import unittest
from tests.brain.test_body_control import Fixture, Wire


class GatewayPeerTests(Fixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source=os.environ.get('CONNECTION_GATEWAY_SOURCE')
        if not source:raise unittest.SkipTest('Set CONNECTION_GATEWAY_SOURCE to the copied gateway source tree')
        cls.source=str(Path(source).resolve());sys.path.insert(0,cls.source)
        cls.Gateway=importlib.import_module('g1_gateway.core').Gateway
    @classmethod
    def tearDownClass(cls):sys.path.remove(cls.source)
    def setUp(self):
        super().setUp()
        wire=self.wire
        outer=self
        class Native:
            def __init__(inner): inner.receipts={}
            def snapshot(inner):
                s=wire.status('native-observation')['native']
                p=outer.gateway.permit or outer.gateway.last or {}
                s['native_binding']=importlib.import_module('g1_gateway.protocol').native_binding(p) if p else {}
                s['request_pending']=False
                s['sample']=dict(local_stop_pending=False,native_sample_monotonic_ns=time.monotonic_ns(),
                    low_state_monotonic_ns=time.monotonic_ns(),retained_reference_id='body-ref',
                    safety_override=False,policy_output_selected=True,
                    exit_evidence=dict(receiver_callbacks_sealed=wire.isolated,
                        receiver_decoded_caches_cleared=wire.isolated,
                        retired_input_session_id=p.get('input_session_id') if wire.isolated and outer.gateway.permit is None else None))
                return s
            def call(inner,q):
                if q['operation']=='request':
                    return dict(ok=True,result=deepcopy(inner.receipts[q['request_id']]))
                wire.isolated=True;wire.active_session=None
                result=dict(state='completed',identity=deepcopy(q),
                    controller_instance_id=wire.controller,effect_status='confirmed',
                    evidence=dict(receiver_callbacks_sealed=True,receiver_decoded_caches_cleared=True,
                                  retired_input_session_id=q.get('input_session_id')))
                inner.receipts[q['request_id']]=deepcopy(result)
                return dict(ok=True,result=result)
        self.gateway=self.Gateway(dict(robot_serial='test-robot',control_resource='g1:test-robot:body',
                        nav_endpoint='tcp://127.0.0.1:5556',hand_policy='body_only',permit_mode='control_session'),str(self.root/'gateway.sqlite'),Native())
        self.addCleanup(self.gateway.close)
        original=wire.call
        def call(service,method,path,payload=None):
            if service!='gateway':
                wire.permit=deepcopy(self.gateway.permit)
                return original(service,method,path,payload)
            wire.calls.append((service,method,path,deepcopy(payload)))
            self.gateway.observe()
            if method=='GET':
                if '/requests/' in path:return self.gateway.query(path.rsplit('/',1)[1],'brain')
                return self.gateway.status(path.split('observation_id=')[1])
            return self.gateway.submit(payload,'brain',path.rsplit('/',1)[1])
        wire.call=call
    def test_actual_gateway_grant_upgrade_no_renew_revoke_fence(self):
        self.prepare()
        self.assertEqual(self.gateway.permit['phase'],'execute')
        self.assertFalse(self.posts('renew'))
        result=self.authority.release_control('test-task','nav-1')
        self.assertTrue(result['exit_confirmed']);self.assertIsNone(self.gateway.permit)
    def test_actual_gateway_rejects_retired_session(self):
        self.prepare();payload=deepcopy(self.posts('grant')[0][3])
        self.authority.release_control('test-task','nav-1')
        payload['request_id']='late-grant';payload['permit_seq']=100
        r=self.gateway.submit(payload,'brain','permits')
        self.assertEqual(r['state'],'rejected');self.assertIn('TARGET_RETIRED',r['blockers'])

    def test_takeover_pending_queries_original_and_old_revoke_cannot_stop_new(self):
        from unittest.mock import patch
        from tests.brain.test_body_control import nav_request
        self.driver.timeout=2
        self.prepare()
        old=self.row();old_permit=deepcopy(self.gateway.permit)
        task=self.store.load_state();task.update(state='cancelled',dispatch_closed=True);self.store.save_state(task)
        # Hold back source cleanup to exercise gateway-driven takeover of a still installed owner.
        with patch.object(self.authority,'_schedule_exit',return_value=None):
            self.authority.cancel_task('test-task')
        self.assertEqual(self.gateway.permit,old_permit)
        request=nav_request('nav-2');request['body']['task_id']='task-2';self.schedule(request)
        self.authority.prepare_navigation(request)
        new=self.row();self.assertNotEqual(old['control_session_id'],new['control_session_id'])
        self.assertGreater(new['ownership_epoch'],old['ownership_epoch'])
        self.assertEqual(self.gateway.permit['control_session_id'],new['control_session_id'])
        grant=self.posts('grant')[-1][3]
        self.assertIs(grant['takeover'],True)
        self.assertTrue(any(x[0]=='gateway' and x[1]=='GET' and grant['request_id'] in x[2] for x in self.wire.calls))
        self.authority._permit(old['lease_id'],'revoke')
        self.assertEqual(self.gateway.permit['control_session_id'],new['control_session_id'])
        self.assertFalse(any(x[3].get('dispatch_policy')=='replace_current' for x in self.wire.calls if x[1]=='POST'))
