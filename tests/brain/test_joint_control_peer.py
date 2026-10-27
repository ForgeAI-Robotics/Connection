"""Full Runtime with delivered NAV/VLA/relay HTTP; hardware alone is simulated.

Requires CONNECTION_NAV_SOURCE (copied sources) and CONNECTION_VLA_SOURCE.
Inherits the loopback-only/no-subprocess guard, never starts deployed services.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

from brain.adapters.body_control import ControlDriver, ControlledBodyAdapter
from brain.adapters.dream_client import DreamClient
from brain.adapters.vla_client import VlaClient
from brain.app import create_service
from brain.packages.reception_skill import BASELINE_PATH
from contracts.body_control import stamp
from tests.brain.test_control_peer import Hardware, Transport
from tests.brain.test_nav_peer import NavigationFixture, BRAIN_TOKEN, NAV_TOKEN
from tests.brain.test_native_vla import evidence


class JointControlPeerTests(NavigationFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source=os.environ.get('CONNECTION_VLA_SOURCE')
        if not source:raise unittest.SkipTest('CONNECTION_VLA_SOURCE is required')
        super().setUpClass()
        cls.peer_root=Path(source).resolve()
        sys.path.insert(0,str(cls.peer_root))
        cls.vla_module=importlib.import_module('g1_brain_vla_bridge.bridge_service')
        cls.handoff_module=importlib.import_module('g1_brain_vla_bridge.handoff_client')
        cls.relay_module=importlib.import_module('g1_navigation_vla_bridge.relay_control')

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.peer_root))
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        env=patch.dict(os.environ,{'BRAIN_CONTROL_TOKEN_FILE':'',
            'BRAIN_VLA_SONIC_SAFETY_ATTESTED':'1','EXPECTED_NX_SERIAL':'test-robot',
            'FQ_EXECUTION_STATE':str(self.root/'execution.json'),
            'FQ_EXECUTION_LOCK':str(self.root/'execution.lock'),
            'FQ_EXECUTION_BLOCK':str(self.root/'execution.blocked')})
        env.start();self.addCleanup(env.stop)
        outer=self
        class SimulatedTransport(Transport):
            def status(self):return dict(super().status(),upstream_fresh=self.fresh)
        self.transport=SimulatedTransport()
        self.client=self.handoff_module.BrainHandoff(self.nav_client.base_url,'test-robot',
            'tcp://192.0.2.10:5556',timeout=1,operation_timeout=3,poll_interval=.01)
        class SimulatedVla(Hardware):
            def stop(self,kind):
                if kind=='cola':
                    from tests.brain.holding_fixture import holding_status
                    outer.physical.holding='cola_can_1'
                    task = outer.vla_runtime._tasks[self.started[-1]]
                    grant = task.handoff['acquire']['receipt']
                    self.holding_proof = holding_status(task.task_id, task.command_id,
                        {key: grant[key] for key in ('lease_id', 'authority_id', 'ownership_epoch')})
                return super().stop(kind)
        self.hardware=SimulatedVla(self.client)
        vla_config={'runtime_dir':str(self.root/'vla'),'dream':{'base_url':self.url,'timeout_sec':1},
            'camera':{'host':'127.0.0.1','port':1,'max_age_sec':1},
            'verification':{'photo_verification_enabled':False},'operator_approval':{'required_operations':[]},
            'backend':{'timeout_sec':10,'real_action_enabled':True,
                       'native_tasks':{'enabled':True,'timeout_sec':30}}}
        self.vla_runtime=self.vla_module.BridgeRuntime(vla_config,self.peer_root/'g1_brain_vla_bridge')
        self.vla_runtime.backend.native.driver=self.hardware
        vla_url=self.serve(self.vla_module.BridgeHTTPServer(('127.0.0.1',0),self.vla_runtime))
        self.relay=self.relay_module.RelayControl(self.root/'relay',self.client,self.transport)
        self.addCleanup(self.relay.close)
        relay_url=self.serve(self.relay_module.RelayHTTPServer(('127.0.0.1',0),self.relay))

        # Simulated physical observations have their own state, not ledger-derived
        # readiness. Production observation validation/HTTP is left unchanged.
        self.nav_fenced=True
        self.standing={}
        prepare=self.physical.prepare
        yield_output=self.physical.yield_output
        def do_prepare(association):
            result=prepare(association)
            self.nav_fenced=False
            self.transport.fresh=True
            return result
        def do_yield(association):
            self.nav_fenced=True
            return yield_output(association)
        def observe():
            return dict(robot_serial='test-robot',observed_at=stamp(),controller_instance_id='sonic-test',
                output_fenced=self.nav_fenced,hold_verified=True,holding=self.physical.holding,
                navigation_stopped=True,current_xyt=[0,0,0],proof_context=deepcopy(self.standing))
        self.physical.prepare=do_prepare;self.physical.yield_output=do_yield;self.physical.observe=observe
        run=self.bridge._run_navigation
        def navigate(command):
            run(command)
            body=self.bridge.commands[command]
            self.standing=dict(dream_command_id=command,target_id=body['target_id'],valid=True,
                pose_within_tolerance=True,map_id='map-test',localization_epoch='loc-test',navigation_epoch=command)
        self.bridge._run_navigation=navigate
        self.bridge._navigation_transport_ready=lambda:self.transport.bound and self.transport.fresh

        self.config.update(brain={'reception_backend':'real','capture_timeline':False,
            'reasoning':{'recovery_advice':False}},reflection={'enabled':False})
        self.config['reception_real'].update(vla_base_url=vla_url,
            skill_file=str(BASELINE_PATH.with_name('reception_single_can_v2.yaml')))
        self.config['reception_real']['body_control'].update(relay_base_url=relay_url,operation_timeout_sec=2)
        # The registered Brain HTTP app references this same, not-yet-started
        # authority. Replace its fixture driver with the unmodified production one.
        self.driver=ControlDriver(self.config)
        self.authority.driver=self.driver
        self.authority.binding.update(vla=vla_url,relay=relay_url)
        self.pool=ThreadPoolExecutor(3)
        self.authority.executor=self.pool
        self.addCleanup(lambda:self.pool.shutdown(wait=True))
        self.port=ControlledBodyAdapter(DreamClient(self.url),VlaClient(vla_url),self.authority)
        self.service=create_service(self.config,model=lambda _:self.fail('No LLM in fixed workflow'),
                                    port_factory=lambda _:self.port)
        self.addCleanup(self.drain_vla)

    def drain_vla(self):
        for event in self.vla_runtime._cancel.values():event.set()
        deadline=time.monotonic()+4
        while self.vla_runtime._active_command_id is not None and time.monotonic()<deadline:time.sleep(.01)
        self.assertIsNone(self.vla_runtime._active_command_id,'test worker did not stop')

    def wait_vla(self,command,states):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=self.port.vla.task(command)
            if result['state'] in states:return result
            time.sleep(.01)
        self.fail('VLA did not reach '+str(states)+': '+str(result))

    def finish_current(self):
        command=self.service.runtime.record['open_command_id']
        self.service.vla_effect('joint-reception',command,'pause')
        task=self.wait_vla(command,{'waiting_result_confirmation'})
        self.service.vla_effect('joint-reception',command,'finish',evidence=evidence(task['operation']),
            authorization='Bearer '+self.vla_runtime._completion_token)
        result=self.wait_vla(command,{'succeeded','failed'})
        self.assertEqual(result['state'],'succeeded',result.get('error'))
        self.assertTrue(self.authority.released('joint-reception',command))
        self.assertFalse(self.relay.enabled)
        return result

    def test_production_o6_reference_hold_does_not_supply_carrying_proof(self):
        from types import SimpleNamespace
        from g1_brain_vla_bridge.o6_service import ManagedHands
        from g1_navigation_execution_driver import NavigationExecutionDriver
        from g1_brain_navigation_control import ControlError as NavControlError
        clock = [0.]
        robot = '1420426006229'
        hands = ManagedHands(self.root / 'o6-test.json',
            SimpleNamespace(endpoint='tcp://192.0.2.10:5556'), None,
            lambda *_: self.fail('No actuator write in this contract check'), clock=lambda: clock[0])
        hands.robot_serial = robot
        hands.targets = {side: [100] * 6 for side in ('left', 'right')}
        first = time.time() - .6
        for index in range(4):
            clock[0] = index * .2
            for side in ('left', 'right'):
                hands.observe(side, [100] * 6, stamp=first + clock[0])
        status = hands.status()
        self.assertTrue(status['hold_verified'], status['hold_evidence'])
        self.assertTrue(status['hands_stopped'])
        self.assertIsNone(status['holding'])
        # Reproduce the NAV observer's envelope around these actual receiver
        # fields. Fresh simulated encoders do not establish object retention.
        observed = dict(status, available=True, both_encoder_feedback_fresh=all(
            sample['fresh'] for sample in status['measured'].values()))
        driver = NavigationExecutionDriver.__new__(NavigationExecutionDriver)
        driver.lock = threading.RLock()
        driver.brain = SimpleNamespace(robot_serial=robot)
        driver.hold_observer = SimpleNamespace(observe=lambda: observed)
        self.assertTrue(driver._hold()['hold_verified'])
        with self.assertRaises(NavControlError) as failure:
            driver._hold(required_holding='cola_can_1')
        self.assertEqual(failure.exception.code, 'O6_OBJECT_RETENTION_UNCONFIRMED')

    def test_brain_consumes_actual_signed_o6_receipt_and_rejects_its_invalidation(self):
        from types import SimpleNamespace
        from datetime import datetime, timedelta, timezone
        from g1_brain_vla_bridge.o6_service import ManagedHands
        from g1_brain_vla_bridge import holding_evidence as signing
        from contracts.vla import validate_current_holding
        from g1_navigation_holding_evidence import check_holding_evidence
        robot = '1420426006229'
        clock = [0.]
        native = dict(controller_instance_id='sonic-test', safety_override=False,
                      reference_retained=True, receiver_accepting=False)
        credential = 'offline-verifier-credential-not-for-hardware'
        hands = ManagedHands(self.root / 'signed-o6.json', SimpleNamespace(endpoint='tcp://192.0.2.10:5556'),
            lambda: native, lambda *_: self.fail('Holding attestation must not write actuators'),
            clock=lambda: clock[0], holding_key=signing.signing_key(credential))
        hands.robot_serial = robot
        hands.targets = {side: [100] * 6 for side in ('left', 'right')}
        hands.output_instance_id = 'output-test'
        hands.bound_native = 'sonic-test'
        hands.association = dict(task_id='task', command_id='grasp', lease_id='lease',
                                 authority_id='brain', ownership_epoch=3, owner='vla')
        first = time.time() - .6
        for index in range(4):
            clock[0] = index * .2
            for side in ('left', 'right'):
                hands.observe(side, [100] * 6, stamp=first + clock[0])
        task = SimpleNamespace(task_id='task', command_id='grasp', request={'object_id':'cola_can_1'})
        observed = evidence('grasp_cola')
        observed['valid_until'] = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        proof = signing.sign(task, observed, signing.anchor(hands._snapshot(), 'sonic-test'), credential, time.time())
        receipt = hands.handle(dict(action='attest_holding', request_id='holding-1',
            receiver_instance_id=hands.instance, association=deepcopy(hands.association), proof=proof))
        self.assertEqual(receipt['status'], 'completed', receipt)
        def status():
            current = hands.status()
            return dict(current_holding=current['holding'], holding_evidence=current['holding_evidence'],
                        execution_facts=dict(controller_instance_id='sonic-test', o6=current))
        current = status()
        validate_current_holding(current, 'cola_can_1', robot_serial=robot, task_id='task',
                                 source_association=hands.association)
        nav_view = dict(current['execution_facts']['o6'], both_encoder_feedback_fresh=True)
        self.assertTrue(check_holding_evidence(nav_view, controller_instance='sonic-test')['holding_evidence_valid'])
        self.assertNotIn(credential, str(current))
        hands.commands += 1
        invalid = status()
        self.assertIsNone(invalid['current_holding'])
        with self.assertRaises(ValueError):
            validate_current_holding(invalid, 'cola_can_1', robot_serial=robot, task_id='task',
                                     source_association=hands.association)
        nav_view = dict(invalid['execution_facts']['o6'], both_encoder_feedback_fresh=True)
        self.assertFalse(check_holding_evidence(nav_view, controller_instance='sonic-test')['holding_evidence_valid'])
        self.assertEqual(hands.handle({'action':'request', 'request_id':'holding-1',
                                      'receiver_instance_id':hands.instance}), receipt)
        self.assertIsNone(status()['current_holding'])  # Historical receipt cannot revive it.

    def test_full_runtime_nav_open_grasp_and_four_delivery_legs(self):
        files={}
        for name,token in (('BRAIN_CONTROL_TOKEN',BRAIN_TOKEN),('NAV_CONTROL_TOKEN',NAV_TOKEN)):
            path=self.root/name
            path.write_text(token);path.chmod(0o600)
            files[name]='';files[name+'_FILE']=str(path)
        env=patch.dict(os.environ,files);env.start();self.addCleanup(env.stop)
        self.service.publish('开始接待','joint-reception',options={'reflect':False})
        runtime=self.service.runtime
        self.assertEqual(runtime.state,'waiting_human',runtime.public_status())
        self.assertEqual(len(self.bridge.commands),1)
        first=runtime.record['open_command_id']
        self.assertFalse(self.relay.enabled)
        self.finish_current()
        opens=self.transport.opens
        self.service.control('continue',task_id='joint-reception')
        self.assertEqual(runtime.state,'waiting_human',runtime.public_status())
        second=runtime.record['open_command_id']
        self.assertNotEqual(first,second)
        self.assertEqual(self.transport.opens,opens,'continuous VLA restored NAV')
        self.assertFalse(self.relay.enabled)
        grants=[self.vla_runtime.task(c).handoff['acquire']['receipt'] for c in (first,second)]
        self.assertNotEqual(grants[0]['lease_id'],grants[1]['lease_id'])
        self.assertGreater(grants[1]['ownership_epoch'],grants[0]['ownership_epoch'])
        self.finish_current()
        self.service.control('continue',task_id='joint-reception')
        self.assertEqual(runtime.state,'succeeded',runtime.public_status())
        self.assertEqual(len(self.bridge.commands),5)
        self.assertEqual(len(self.hardware.started),2)
        self.assertEqual(self.transport.opens,5)
        prepares=[x[1] for x in self.effects if x[0]=='prepare']
        self.assertTrue(all(x.get('required_holding')=='cola_can_1' for x in prepares[1:]))
        ledger=self.authority.store.read()
        self.assertIsNone(ledger['current'])
        self.assertEqual(len(ledger['leases']),7)
        self.assertTrue(all(x['state']=='released' for x in ledger['leases'].values()))
        self.assertFalse(self.relay.enabled)
        serialized=json.dumps(runtime.record)+json.dumps(ledger)
        for secret in (BRAIN_TOKEN,NAV_TOKEN,self.vla_runtime._completion_token):
            self.assertNotIn(secret,serialized)

    def test_missing_nav_credential_blocks_vla_without_starting_policy(self):
        with patch.dict(os.environ,{'NAV_CONTROL_TOKEN':'','NAV_CONTROL_TOKEN_FILE':'',
                                   'BRAIN_NAV_TEST_TOKEN':NAV_TOKEN}):
            # Brain can still query NAV; only VLA's independent credential is absent.
            self.config['reception_real']['body_control']['nav_token_env']='BRAIN_NAV_TEST_TOKEN'
            self.port.dream.http.auth_token_env='BRAIN_NAV_TEST_TOKEN'
            self.driver.clients['nav'].auth_token_env='BRAIN_NAV_TEST_TOKEN'
            self.service.publish('开始接待','joint-reception',options={'reflect':False})
        self.assertEqual(len(self.bridge.commands),1,self.service.runtime.public_status())
        self.assertFalse(self.hardware.started)
        self.assertNotEqual(self.service.runtime.state,'succeeded')

    def test_stale_observation_blocks_initial_grant_and_all_business(self):
        observe=self.physical.observe
        self.physical.observe=lambda:dict(observe(),observed_at='2000-01-01T00:00:00+00:00')
        self.service.publish('开始接待','joint-reception',options={'reflect':False})
        self.assertFalse(self.bridge.commands)
        self.assertFalse(self.hardware.started)
        self.assertFalse(self.relay.enabled)
        self.assertEqual(self.service.runtime.state,'recovery_required')
