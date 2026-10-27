"""Opt-in NAV source/HTTP integration; a copied source tree, never a live NX service."""
from copy import deepcopy
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import importlib
import ipaddress
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.serving import make_server
from brain.adapters.body_control import ControlDriver, ControlledBodyAdapter
from brain.adapters.dream_client import DreamClient
from brain.adapters.http_client import HttpClient, HttpContractError
from brain.api.body_control import register_body_control
from brain.kernel.body_control import BodyControl
from brain.storage.tasks import KernelStore
from contracts.body_control import ControlError, stamp
from tests.brain.test_body_control import Wire, configuration, nav_request, Immediate
from tests.brain.test_control_peer import QuietHandler

BRAIN_TOKEN='offline-brain-nav-authority-token-0000001'
NAV_TOKEN='offline-navigation-http-token-00000000001'


class NavigationFixture:
    @classmethod
    def setUpClass(cls):
        source=os.environ.get('CONNECTION_NAV_SOURCE')
        if not source:raise unittest.SkipTest('Set CONNECTION_NAV_SOURCE to a copied NAV source root')
        cls.tools=Path(source).resolve()/'dream/tools'
        if not (cls.tools/'g1_brain_navigation_control.py').is_file():raise RuntimeError('NAV source missing')
        cls.bytecode=sys.dont_write_bytecode;sys.dont_write_bytecode=True
        sys.path.insert(0,str(cls.tools))
        cls.nav=importlib.import_module('g1_brain_navigation_control')
        cls.service=importlib.import_module('g1_agent_navigation_service')
    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.tools));sys.dont_write_bytecode=cls.bytecode

    def serve(self,server):
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():server.shutdown();server.server_close();thread.join(3)
        self.addCleanup(close)
        return 'http://127.0.0.1:'+str(server.server_port)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.wire=Wire();self.effects=[]
        env=patch.dict(os.environ,{'BRAIN_CONTROL_TOKEN':BRAIN_TOKEN,'NAV_CONTROL_TOKEN':NAV_TOKEN,
            'BRAIN_CONTROL_TOKEN_FILE':'','NAV_CONTROL_TOKEN_FILE':'',
            'HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'','NO_PROXY':'127.0.0.1,localhost',
            'http_proxy':'','https_proxy':'','all_proxy':'','no_proxy':'127.0.0.1,localhost'})
        env.start();self.addCleanup(env.stop)
        connect=socket.socket.connect
        def local(sock,address):
            if not isinstance(address,tuple) or not ipaddress.ip_address(address[0]).is_loopback:
                raise AssertionError('NAV test attempted non-loopback connection')
            return connect(sock,address)
        guard=patch.object(socket.socket,'connect',local);guard.start();self.addCleanup(guard.stop)
        for action in ('run','Popen'):
            guard=patch('subprocess.'+action,side_effect=AssertionError('NAV test attempted subprocess'))
            guard.start();self.addCleanup(guard.stop)
        app=Flask(__name__)
        brain_server=make_server('127.0.0.1',0,app,threaded=True,request_handler=QuietHandler)
        brain_url='http://127.0.0.1:'+str(brain_server.server_port)
        self.nav_client=self.nav.BrainControlClient(brain_url,'test-robot','tcp://192.0.2.10:5556',token=BRAIN_TOKEN)
        outer=self
        class PhysicalFixture:
            holding=None
            observation=None
            def observe(self):return deepcopy(self.observation) if self.observation else {}
            def qualification_blockers(self):return []
            def prepare(self,association):
                outer.effects.append(('prepare',deepcopy(association)))
                outer.wire.relay['upstream_fresh']=True
                return dict(mode_confirmed=True,posture_preserved=True,old_output_fenced=True,
                            fresh_preparation_output=True,navigation_not_started=True,hold_verified=True,
                            holding=self.holding)
            def yield_output(self,association):
                outer.effects.append(('yield',deepcopy(association)))
                return dict(navigation_stopped=True,output_fenced=True,mode_idle_hold_fenced=True,hold_verified=True)
        self.physical=PhysicalFixture()
        self.participant=self.nav.NavigationParticipant(self.root/'nav.db',self.nav_client,self.physical)
        self.addCleanup(lambda:self.participant.close() if not self.participant.closed else None)
        # The production constructor deliberately refuses live joint motion. As in
        # NAV's own tests, inject only its physical execution/health boundaries.
        bridge=self.service.AgentBridge.__new__(self.service.AgentBridge)
        bridge.args=SimpleNamespace(api_token=NAV_TOKEN,motion_authorized=True,brain_controlled=True)
        bridge.brain_control=self.participant
        bridge.lock=threading.RLock();bridge.submission_lock=threading.RLock()
        bridge.commands={};bridge.active_command_id='';bridge.preemption_stop_pending=False
        bridge._persist_state=lambda:None;bridge._deactivate_six_d=lambda reason:None
        bridge._navigation_transport_ready=lambda:True;bridge.web_status=lambda:{}
        bridge.fine_graph=bridge.base_graph=bridge.map_yaml=self.root/'absent-test-map'
        bridge.six_d_session={};bridge.navigation_only=True;bridge.navigation_relay_host='127.0.0.1'
        bridge.retain_command_result_sec=60
        def run(command):
            with bridge.lock:
                bridge.commands[command].update(state='succeeded',completed_at=stamp(),
                    result=dict(success=True,reached=True,navigation_stopped=True))
                bridge.active_command_id=''
        bridge._run_navigation=run
        self.bridge=bridge
        health=patch.object(self.service,'read_health',return_value={'new_task_allowed':True,'reason':'offline'})
        health.start();self.addCleanup(health.stop)
        server=ThreadingHTTPServer(('127.0.0.1',0),self.service.make_handler(bridge))
        self.url=self.serve(server)
        self.config=configuration(self.root/'brain')
        self.config['reception_real']['dream_base_url']=self.url
        self.config['reception_real']['body_control']['nav_token_env']='NAV_CONTROL_TOKEN'
        self.config['reception_real']['body_control'].update(operation_timeout_sec=1,poll_interval_sec=.005)
        self.driver=ControlDriver(self.config,clients={'nav':HttpClient(self.url),
            'vla':self.wire.client('vla'),'relay':self.wire.client('relay')})
        # Only the initial physical idle proof is substituted. A separate test
        # calls the unchanged production status reader and requires blocking.
        self.driver.idle=lambda pending=None:{'test_physical_idle':True}
        self.authority=BodyControl(self.config,self.root/'brain',self.driver,Immediate())
        self.addCleanup(self.authority.close)
        register_body_control(app,SimpleNamespace(body_control=self.authority,start=self.authority.start))
        self.serve(brain_server)
        self.store=KernelStore(self.root/'brain')
        self.port=ControlledBodyAdapter(DreamClient(self.url),SimpleNamespace(),self.authority)

    def schedule(self,command='nav-1',required_holding=None):
        request=nav_request(command)
        if required_holding:request['required_holding']=required_holding
        state=self.store.load_state() or {'revision':0,'steps':{}}
        state.update(task_id='test-task',state='running',open_command_id=command,dispatch_closed=False,control_request='',
                     execution_backend='reception_real',execution_target={'dream':self.url,'vla':'http://vla.invalid'})
        state['steps'][command]={'attempts':[{'attempt_id':command+'-a1','command_id':command,'request':deepcopy(request)}]}
        self.store.save_state(state)
        return request

    def prepare(self,command='nav-1'):
        request=self.schedule(command)
        return request,self.authority.prepare_navigation(request)

    def call(self,path,payload=None):
        return HttpClient(self.url).request_json('GET' if payload is None else 'POST',path,payload,
            accepted_statuses=(200,202),headers={'Authorization':'Bearer '+NAV_TOKEN})[0]

class NavigationPeerTests(NavigationFixture, unittest.TestCase):
    def test_actual_authority_client_prepare_and_goal_bind_same_authorization(self):
        request=self.schedule()
        result=self.port.submit('nav-1',request)
        self.assertTrue(result['accepted'],result)
        value=self.port.wait('nav-1',request)
        self.assertEqual(value['terminal'],'succeeded')
        body=self.bridge.commands['nav-1']
        self.assertEqual(body['control_authorization'],self.participant.state['association']['control_authorization'])
        self.assertTrue(self.nav_client.current(body)['authorization_current'])

    def test_yield_preserves_business_result_and_late_yield_cannot_touch_next(self):
        request=self.schedule();self.assertTrue(self.port.submit('nav-1',request)['accepted'])
        self.port.wait('nav-1',request)
        before=deepcopy(self.bridge.commands['nav-1'])
        self.authority.settle_navigation('test-task','nav-1')
        self.assertEqual(self.bridge.commands['nav-1'],before)
        operations=self.authority.store.read()['requests'].values()
        old=next(child['wire']['payload'] for op in operations for child in op['children'].values()
                 if child['wire']['payload']['action']=='yield')
        _,ref=self.prepare('nav-2')
        count=len(self.effects)
        self.call('/v1/navigation/handoff/yield',old)
        self.assertEqual(len(self.effects),count)
        self.assertEqual(self.participant.state['association']['control_authorization'],ref)
        with self.assertRaises(HttpContractError):
            self.call('/v1/navigation/handoff/yield',dict(old,request_id='late-new-id'))

    def test_actual_resume_preserves_new_authorization(self):
        request=self.schedule();self.assertTrue(self.port.submit('nav-1',request)['accepted'])
        self.port.wait('nav-1',request)
        self.bridge.commands['nav-1'].update(state='failed',result={'navigation_stopped':True})
        self.authority.settle_navigation('test-task','nav-1')
        _,ref=self.prepare('nav-2')
        result=self.call('/v1/navigation/resume',dict(task_id='test-task',command_id='nav-2',
            resume_of_command_id='nav-1',control_authorization=ref))
        self.assertEqual(result['control_authorization'],ref)
        self.assertEqual(result['recovery_of_command_id'],'nav-1')

    def test_unqualified_real_driver_remains_blocked(self):
        self.participant.driver=self.nav.UnqualifiedNavigationDriver()
        with self.assertRaisesRegex(ControlError,'NAV_OUTPUT_FENCING_UNIMPLEMENTED'):
            self.prepare()
        self.assertFalse(self.effects)

    def test_current_nav_status_without_physical_facts_stays_unconfirmed(self):
        with self.assertRaises(ControlError):self.driver.status('nav')
        raw=self.call('/v1/status')
        self.assertIsNone(raw['output_fenced'])
        self.assertIsNone(raw['updated_at'])
        self.assertIn('BRAIN_CONTROL_NOT_QUALIFIED',raw['motion_blockers'])

    def test_current_nav_observation_is_consumed_without_refreshing_its_time(self):
        observed=stamp()
        self.physical.observation=dict(robot_serial='test-robot',observed_at=observed,
            controller_instance_id='sonic-test',output_fenced=True,hold_verified=True,
            navigation_stopped=True,current_xyt=[0,0,0])
        result=self.driver.status('nav')
        self.assertTrue(result['output_fenced'])
        self.assertTrue(result['hold_verified'])
        self.assertEqual(result['updated_at'],observed)
        self.physical.observation['observed_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaises(ControlError):self.driver.status('nav')
        self.assertIsNone(self.call('/v1/status')['output_fenced'])

    def test_posture_preserved_is_not_upgraded_to_hold_verified(self):
        old=self.physical.prepare
        def prepare(association):
            result=old(association);result.pop('hold_verified');return result
        self.physical.prepare=prepare
        with self.assertRaisesRegex(ControlError,'NAV_EVIDENCE_UNCONFIRMED'):self.prepare()

    def test_carry_constraint_requires_an_explicit_matching_holding_receipt(self):
        request=self.schedule(required_holding='cola_can_1')
        with self.assertRaisesRegex(ControlError,'NAV_HOLD_UNCONFIRMED'):
            self.authority.prepare_navigation(request)
        self.assertEqual(self.effects[-1][1]['required_holding'],'cola_can_1')

    def test_matching_held_object_reaches_prepare_driver_and_brain(self):
        self.physical.holding='cola_can_1'
        request=self.schedule(required_holding='cola_can_1')
        self.authority.prepare_navigation(request)
        self.assertEqual(self.effects[-1][1]['required_holding'],'cola_can_1')

    def test_wrong_held_object_remains_blocked(self):
        self.physical.holding='different_object'
        with self.assertRaisesRegex(ControlError,'NAV_HOLD_UNCONFIRMED'):
            self.authority.prepare_navigation(self.schedule(required_holding='cola_can_1'))

    def test_navigation_participant_restart_cannot_reuse_historical_prepare(self):
        _,ref=self.prepare()
        self.participant.close()
        self.participant=self.nav.NavigationParticipant(self.root/'nav.db',self.nav_client,self.physical)
        self.bridge.brain_control=self.participant
        op=next(iter(self.authority.store.read()['requests'].values()))
        wire=op['children']['nav-prepare']['wire']
        with self.assertRaisesRegex(ControlError,'历史|重启'):
            self.driver.exchange(wire,send=False)
        with self.assertRaises(self.nav.ControlError):self.participant.require_prepared(dict(
            task_id='test-task',command_id='nav-1',control_authorization=ref))

    def test_joint_business_credential_never_follows_redirect_or_cross_origin(self):
        with self.assertRaisesRegex(HttpContractError,'其他服务'):
            self.port.dream.http.request_json('GET','http://192.0.2.99/status')
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302);self.send_header('Location','http://192.0.2.99/status')
                self.end_headers()
            def log_message(self,*args):pass
        url=self.serve(ThreadingHTTPServer(('127.0.0.1',0),Redirect))
        client=HttpClient(url);client.auth_token_env='NAV_CONTROL_TOKEN'
        with self.assertRaises(HttpContractError) as caught:client.request_json('GET','/redirect')
        self.assertEqual(caught.exception.status_code,302)

    def test_vla_fact_client_uses_nav_credential_and_missing_credential_is_refused(self):
        source=os.environ.get('CONNECTION_VLA_SOURCE')
        if not source:raise unittest.SkipTest('VLA source is required for cross-peer credential check')
        sys.path.insert(0,str(Path(source).resolve()))
        try:
            bridge=importlib.import_module('g1_brain_vla_bridge.bridge_service')
            client=bridge.DreamClient(self.url,1)
            self.assertTrue(client._get('/v1/status')['interface_online'])
            request=self.schedule();self.assertTrue(self.port.submit('nav-1',request)['accepted'])
            self.port.wait('nav-1',request)
            self.assertEqual(client._get('/v1/commands/nav-1')['state'],'succeeded')
            with patch.dict(os.environ,{'NAV_CONTROL_TOKEN':'','NAV_CONTROL_TOKEN_FILE':''}):
                with self.assertRaises(bridge.BridgeFailure) as caught:client._get('/v1/status')
            self.assertEqual(caught.exception.code,'NAVIGATION_STATUS_UNVERIFIABLE')
            self.assertIn('401',str(caught.exception))
        finally:sys.path.pop(0)
