"""Existing web mixed-reception entry uses the same gateway authority for every NAV leg."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from brain.adapters.body_control import ControlledBodyAdapter
from brain.adapters.simulated_manipulation import HybridReceptionAdapter, SimulatedManipulationClient
from brain.adapters.ports import build_port, target_identity
from brain.app import create_service
from brain.packages.reception_skill import BASELINE_PATH
from contracts.tasks import Rejected
from contracts.body_control import ControlError, stamp
from tests.brain.test_body_control import Fixture, nav_request
from tests.brain.test_native_vla import FakeDream


class GatewayHybridTests(Fixture, unittest.TestCase):
    def adapter(self):
        wire=self.wire
        wire.nav['active_command_state']=None
        class Nav(FakeDream):
            def status(self):return dict(wire.nav,updated_at=stamp())
            def command(self,command_id):
                raw=super().command(command_id)
                raw['current_hold']=wire.hold(raw)
                return raw
            def submit_navigation(self,body):
                super().submit_navigation(body);wire.commands=self.commands
                return {'accepted':True}
        nav=Nav()
        virtual=SimulatedManipulationClient(nav,delay_sec=0)
        return HybridReceptionAdapter(nav,virtual,navigation=ControlledBodyAdapter(nav,None,self.authority))
    def service(self,port):
        self.config.update(brain={'reception_backend':'real','capture_timeline':False,
                                 'reasoning':{'recovery_advice':False}},reflection={'enabled':False})
        self.config['reception_real']['skill_file']=str(BASELINE_PATH.with_name('reception_single_can_v2.yaml'))
        return create_service(self.config,model=lambda _:self.fail('No model'),port_factory=lambda _:port)
    def test_body_only_gateway_runs_full_mixed_workflow_without_hand_permissions(self):
        port=self.adapter();service=self.service(port)
        with patch.dict(os.environ,{'FQ_EXECUTION_STATE':str(self.root/'execution.json'),
                                    'FQ_EXECUTION_LOCK':str(self.root/'execution.lock'),
                                    'FQ_EXECUTION_BLOCK':str(self.root/'execution.blocked')}):
            service.publish('开始接待（导航真机＋操控模拟）','test-task',options={'entry':'web','reflect':False})
        self.assertEqual(service.runtime.state,'succeeded',service.runtime.public_status())
        self.assertEqual(len(self.posts('grant')),1)
        self.assertEqual(len(self.posts('allow_execute')),1)
        self.assertEqual(len(port.vla._commands),2)
        self.assertTrue(all(r['simulated'] and r['result']['physical_execution'] is False
                            for r in port.vla._commands.values()))
        self.assertEqual({call[0] for call in self.wire.calls},{'nav','gateway'})
        self.assertFalse(self.authority.recovery_status()['blocked'])
        for call in self.wire.calls:
            if call[1]=='POST':self.assertFalse(call[3].get('required_holding'))
        for op in ('grant','allow_execute','renew'):
            for call in self.posts(op):
                self.assertEqual(call[3]['hand_mode'],'not_required')
                self.assertIsNone(call[3]['hold_reference_id'])
        for row in self.authority.store.read()['leases'].values():
            self.assertFalse(row['business_submission']['body'].get('required_holding'))
            self.assertEqual(row['holding_scope'],'simulated')

    def test_missing_body_hold_blocks_hybrid_even_if_manipulation_is_simulated(self):
        port=self.adapter();service=self.service(port)
        original=self.wire.status
        def bad(observation):
            s=original(observation);s['native']['body_hold_verified']=False;return s
        self.wire.status=bad
        with patch.dict(os.environ,{'FQ_EXECUTION_STATE':str(self.root/'execution.json')}):
            service.publish('开始接待（导航真机＋操控模拟）','test-task',options={'reflect':False})
        self.assertNotEqual(service.runtime.state,'succeeded')
        self.assertFalse(self.posts('allow_execute'));self.assertFalse(port.vla._commands)
    def test_virtual_holding_cannot_authorize_real_backend_carry(self):
        request=nav_request();request['required_holding']='cola_can_1';self.schedule(request)
        with self.assertRaises(ControlError):self.authority.register(request)
    def test_real_backends_never_fall_back_when_gateway_disabled(self):
        config=deepcopy(self.config);config['reception_real']['body_control']['enabled']=False
        for backend in ('body_real','reception_real','reception_real_sim_manipulation'):
            with self.subTest(backend=backend),self.assertRaises(Rejected):build_port(config,backend)
    def test_hybrid_target_keeps_gateway_binding_and_virtual_vla(self):
        value=target_identity(self.config,'reception_real_sim_manipulation')
        self.assertEqual(value['vla'],'local:simulated-manipulation/v1')
        self.assertEqual(value['body_control']['gateway_base_url'],'https://gateway.invalid')
    def test_nav_cancel_uses_authority_and_mock_cancel_stays_local(self):
        port=self.adapter();request=nav_request()
        with patch.object(self.authority,'cancel_navigation',return_value={'state':'completed'}) as cancel:
            self.assertTrue(port.cancel('nav-1',request)['completed'])
            cancel.assert_called_once_with('test-task','nav-1')
