"""Opt-in real VLA HTTP/worker integration; gateway/native/CAN remain synthetic."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from brain.adapters.body_control import ControlDriver, ControlledBodyAdapter
from brain.adapters.http_client import HttpClient
from brain.adapters.vla_client import VlaClient
from brain.kernel.body_control import BodyControl
from brain.storage.tasks import KernelStore
from tests.brain.test_body_control import configuration
from tests.brain.test_vla_gateway import VlaWire, vla_request


class VlaGatewayPeerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source=os.environ.get('CONNECTION_VLA_SOURCE')
        if not source:
            raise unittest.SkipTest('Set CONNECTION_VLA_SOURCE to the VLA workspace; requires its test dependencies')
        cls.source=Path(source).resolve()
        sys.path.insert(0,str(cls.source))
        from g1_brain_vla_bridge.tests.test_gateway_integration import GatewayIntegrationTests
        cls.Peer=GatewayIntegrationTests

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.source))

    def setUp(self):
        self.peer=self.Peer('test_actual_http_prepare_execute_finish_stop_reuses_worker')
        self.addCleanup(self.peer.doCleanups)
        cwd=Path.cwd()
        try:
            os.chdir(self.source)
            self.peer.setUp()
        finally:
            os.chdir(cwd)
        credential=patch.dict(os.environ,{'BRAIN_GATEWAY_TOKEN':'isolated-gateway-credential'})
        credential.start();self.addCleanup(credential.stop)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        config=configuration(root)
        robot=self.peer.hw.env['EXPECTED_NX_SERIAL']
        config['reception_real']['body_control'].update(robot_serial=robot,control_resource='g1:'+robot+':body',
                                                       operation_timeout_sec=5)
        url='http://127.0.0.1:'+str(self.peer.server.server_port)
        config['reception_real']['vla_base_url']=url
        peer=self.peer
        class Wire(VlaWire):
            def exit_status(w):
                s=super().exit_status()
                if s:s['hold_reference_id']='hand-ref'
                return s
            def status(w, observation):
                if w.permit and peer.hw.phase=='active':
                    w.active_session=w.permit['input_session_id'];w.isolated=False
                s=super().status(observation)
                s['o6'].update(receiver_instance_id='hands',hold_reference_id='hand-ref')
                return s
            def call(w,s,m,p,b=None):
                assert s=='gateway', 'Unexpected remote source: '+s
                result=super().call(s,m,p,b)
                if m=='POST' and b.get('operation') in {'grant','allow_execute'}:
                    peer.peer.permit=deepcopy(w.permit)
                return result
        self.wire=Wire();self.wire.instance='gw';self.wire.controller='boot:1:2'
        original_current=peer.peer.current
        def current(b,phase=None,**kw):
            original_current(b,phase,**kw)
            return self.wire.status('vla-current')
        peer.peer.current=current
        original_prepared=peer.peer.prepared
        def prepared(b,phase='prepare'):
            original_prepared(b,phase)
            return self.wire.status('vla-prepared')
        peer.peer.prepared=prepared
        pool=ThreadPoolExecutor(4);self.addCleanup(pool.shutdown)
        self.driver=ControlDriver(config,clients={'gateway':self.wire.client('gateway'),
            'vla':HttpClient(url,timeout_sec=3)})
        self.authority=BodyControl(config,root,self.driver,pool);self.addCleanup(self.authority.close)
        self.store=KernelStore(root)
        self.request=vla_request()
        self.request['body']['worksite_evidence'].update(robot_serial=robot,controller_instance_id='boot:1:2')
        self.store.save_state(dict(task_id='test-task',state='running',execution_backend='reception_real',
            execution_target={'vla':url},open_command_id='open-1',dispatch_closed=False,control_request='',
            steps={'open':{'attempts':[{'command_id':'open-1','request':deepcopy(self.request)}]}}))
        self.port=ControlledBodyAdapter(None,VlaClient(url),self.authority)

    def test_actual_prepare_execute_cancel_and_current_exit(self):
        result=self.port.submit('open-1',self.request)
        self.assertTrue(result['accepted'],result)
        self.peer.wait(lambda:self.peer.runtime.task('open-1').state=='running')
        self.assertEqual(self.peer.resume_count,1)
        row=self.authority.command_row('test-task','open-1')
        self.assertEqual(row['prepared_business'],self.peer.runtime.task('open-1').request)
        self.assertTrue(self.port.cancel('open-1',self.request)['completed'])
        self.assertTrue(self.authority.released('test-task','open-1'))
        self.assertFalse(self.peer.alive)
        self.peer.nav_query.assert_not_called()

    def test_actual_pause_finish_effect_and_revocation_are_separate(self):
        from contracts.body_control import stamp
        self.assertTrue(self.port.submit('open-1',self.request)['accepted'])
        self.peer.wait(lambda:self.peer.runtime.task('open-1').state=='running')
        self.port.effect_control('pause','open-1',self.request)
        self.peer.wait(lambda:self.peer.runtime.task('open-1').state=='waiting_result_confirmation')
        self.port.effect_control('finish','open-1',self.request,
            evidence=dict(source='operator_observation',reference='isolated-effect-not-physical',
                          observed_at=stamp(),door_open=True,door_stable_open=True,hands_clear_of_door=True),
            authorization='Bearer '+self.peer.runtime._completion_token)
        self.peer.wait(lambda:self.peer.runtime.task('open-1').state in ('succeeded','failed'))
        self.assertEqual(self.peer.runtime.task('open-1').state,'succeeded')
        writes=len([c for c in self.wire.calls if c[1]=='POST'])
        viewed=self.port.effect_control('query','open-1',self.request)
        self.assertEqual(len([c for c in self.wire.calls if c[1]=='POST']),writes)
        viewed=self.port.query('open-1',self.request)  # Runtime reconciliation owns exit.
        self.assertTrue(viewed['resources_released'],viewed)
        self.assertTrue(viewed['evidence']['supports'],viewed)
        self.assertFalse(viewed['raw']['result']['authorization_released'])
        self.assertTrue(self.authority.released('test-task','open-1'))
