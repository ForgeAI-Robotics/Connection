"""Opt-in loopback integration with the delivered VLA/relay source tree.

Run with CONNECTION_VLA_SOURCE=/path/to/sonic_wbc. Production HTTP handlers,
lease clients and workers are used; NAV facts and physical drivers are test
fixtures. No production service is launched and all non-loopback connects fail.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import ipaddress
import json
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
from werkzeug.serving import make_server, WSGIRequestHandler
from brain.adapters.body_control import ControlDriver, ControlledBodyAdapter
from brain.adapters.dream_client import DreamClient
from brain.adapters.vla_client import VlaClient
from brain.adapters.http_client import HttpClient
from brain.api.body_control import register_body_control
from brain.kernel.body_control import BodyControl
from brain.storage.tasks import KernelStore
from contracts.body_control import stamp, ControlError
from tests.brain.test_body_control import configuration, nav_request, vla_request, Wire
from tests.brain.test_native_vla import evidence

TOKEN = 'offline-peer-control-test-token-000000001'


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs): pass


class FactsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self): self.route('GET')
    def do_POST(self): self.route('POST')
    def route(self, method):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)))) if method == 'POST' else None
        result = self.server.fixture(method, self.path, data)
        body = json.dumps(result).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


class Hardware:
    """Only the physical boundary is replaced, never the lease client or task loop."""
    def __init__(self, client):
        self.client, self.stopped = client, True
        self.started = []
        self.facts = dict(policy_stopped=True, publisher_stopped=True, hands_stopped=True,
            output_fenced=True, action_port_released=True, hold_verified=True, controller_instance_id='sonic-test')
        self.persist_handoff = lambda: None
    def execution_readiness(self): return {'ready':True, 'test_fixture':True}
    def handoff_status(self): return self.client.capabilities()
    def preflight(self, kind): self.client.capabilities()
    def acquire(self, task): return self.client.transfer('acquire', task, self.persist_handoff)
    def release(self, task): return self.client.transfer('release', task, self.persist_handoff)
    def check_authorization(self, task): self.client.current_owner(task)
    def control_observation(self, task):
        acquired = task.handoff.get('acquire', {}).get('receipt')
        if not acquired: raise RuntimeError('No acquired lease yet')
        return self.client.current(dict(self.client.identity(task), authority_id=acquired['authority_id'],
                                        ownership_epoch=acquired['ownership_epoch']))
    def start(self, kind, task, cancel): self.stopped=False; self.started.append(task.command_id)
    def check_alive(self, kind): pass
    def pause(self, kind, cancel): pass
    def check_paused(self, kind): pass
    def stop(self, kind): self.stopped=True; return deepcopy(self.facts)
    def inspect_stopped(self, task):
        # The current peer preserves the physical sample time instead of using
        # HTTP response time. This fixture takes a new simulated sample per read.
        result = dict(deepcopy(self.facts), observed_at=stamp()) if self.stopped else {}
        if getattr(self, 'holding_proof', None):
            result.update(holding=self.holding_proof['current_holding'],
                          holding_evidence=deepcopy(self.holding_proof['holding_evidence']),
                          o6=deepcopy(self.holding_proof['execution_facts']['o6']))
        return result
    def verify_idle(self, task): return self.client.confirmed_release(task)


class Transport:
    def __init__(self): self.bound=False; self.fresh=False; self.opens=0
    def readiness(self): return {'ready':True, 'test_fixture':True}
    def open(self): self.bound=True; self.opens+=1
    def close(self): self.bound=False; self.fresh=False
    def pump(self): pass
    def status(self):
        return dict(port_bound=self.bound, forwarding=self.bound, upstream_frame_observed=self.fresh,
                    last_upstream_frame_at=stamp() if self.fresh else None)


class ProtocolDriver(ControlDriver):
    """Substitute only NAV's physical preparation proof to isolate the VLA HTTP flow.

    The delivered relay cannot yet attest a fresh stream. Separate tests exercise
    the unchanged production prepare check and require that it remain blocked.
    """
    def prepare_navigation(self, row, exchange):
        enabled = exchange('relay-enable', 'relay', '/v1/relay/control',
                           self.payload(row, 'enable_navigation'), '/v1/relay/requests/{request_id}')
        self.require(enabled, 'bound', 'forwarding_enabled')
        prepared = exchange('nav-prepare', 'nav', '/v1/navigation/handoff/prepare',
                            self.payload(row, 'prepare'), '/v1/navigation/handoff/requests/{request_id}')
        self.require(prepared, 'mode_ready', 'hold_verified')
        return dict(prepared=True, navigation=prepared, relay=enabled, test_physical_proof=True)


class DeliveredPeerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get('CONNECTION_VLA_SOURCE')
        if not value:
            raise unittest.SkipTest('Set CONNECTION_VLA_SOURCE to run delivered-peer integration')
        cls.peer_root = Path(value).resolve()
        if not (cls.peer_root/'g1_brain_vla_bridge/bridge_service.py').is_file():
            raise RuntimeError('CONNECTION_VLA_SOURCE does not contain the delivered VLA source')
        cls.old_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(cls.peer_root))
        cls.bridge = importlib.import_module('g1_brain_vla_bridge.bridge_service')
        cls.client_module = importlib.import_module('g1_brain_vla_bridge.handoff_client')
        cls.relay_module = importlib.import_module('g1_navigation_vla_bridge.relay_control')

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.peer_root))
        sys.dont_write_bytecode = cls.old_bytecode

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join(3)
        self.addCleanup(close)
        return 'http://127.0.0.1:'+str(server.server_port)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ, {'BRAIN_CONTROL_TOKEN':TOKEN, 'BRAIN_CONTROL_TOKEN_FILE':'',
            'BRAIN_VLA_SONIC_SAFETY_ATTESTED':'1', 'EXPECTED_NX_SERIAL':'test-robot',
            'HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'','http_proxy':'','https_proxy':'','all_proxy':'',
            'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost'})
        self.env.start();self.addCleanup(self.env.stop)
        original_connect=socket.socket.connect
        def only_loopback(sock, address):
            if not isinstance(address, tuple) or not ipaddress.ip_address(address[0]).is_loopback:
                raise AssertionError('Integration attempted a non-loopback connection')
            return original_connect(sock,address)
        guard=patch.object(socket.socket,'connect',only_loopback);guard.start();self.addCleanup(guard.stop)
        for name in ('run','Popen'):
            guard=patch('subprocess.'+name, side_effect=AssertionError('Integration attempted a process launch'))
            guard.start();self.addCleanup(guard.stop)
        self.wire=Wire();self.transport=Transport()
        self.nav_server=ThreadingHTTPServer(('127.0.0.1',0),FactsHandler)
        def nav(method,path,body):
            clean=path.split('?')[0]
            if method=='POST' and clean=='/v1/navigation/goals':
                self.wire.nav_command(body);return {'accepted':True}
            result=self.wire.call('nav',method,path,body)
            if method=='POST' and body['action']=='prepare': self.transport.fresh=True
            return result
        self.nav_server.fixture=nav; nav_url=self.serve(self.nav_server)
        self.config=configuration(self.root/'brain')
        self.config['reception_real']['dream_base_url']=nav_url
        # Reserve actual HTTP listeners before constructing their circular clients.
        vla_config={'runtime_dir':str(self.root/'vla'),'dream':{'base_url':nav_url,'timeout_sec':1},
            'camera':{'host':'127.0.0.1','port':1,'max_age_sec':1},
            'verification':{'photo_verification_enabled':False}, 'operator_approval':{'required_operations':[]},
            'backend':{'timeout_sec':5,'real_action_enabled':True,'native_tasks':{'enabled':True,'timeout_sec':8}}}
        self.runtime=self.bridge.BridgeRuntime(vla_config,self.peer_root/'g1_brain_vla_bridge')
        self.vla_server=self.bridge.BridgeHTTPServer(('127.0.0.1',0),self.runtime)
        vla_url=self.serve(self.vla_server)
        control_app=Flask(__name__)
        self.http_server=make_server('127.0.0.1',0,control_app,threaded=True,request_handler=QuietHandler)
        brain_url='http://127.0.0.1:'+str(self.http_server.server_port)
        self.client=self.client_module.BrainHandoff(brain_url,'test-robot','tcp://192.0.2.10:5556',
                                                  timeout=1,operation_timeout=2,poll_interval=.01)
        self.hardware=Hardware(self.client);self.runtime.backend.native.driver=self.hardware
        self.relay=self.relay_module.RelayControl(self.root/'relay',self.client,self.transport)
        self.addCleanup(self.relay.close)
        self.relay_server=self.relay_module.RelayHTTPServer(('127.0.0.1',0),self.relay)
        relay_url=self.serve(self.relay_server)
        self.config['reception_real'].update(vla_base_url=vla_url)
        self.config['reception_real']['body_control'].update(relay_base_url=relay_url,operation_timeout_sec=.5,poll_interval_sec=.01)
        self.driver=ProtocolDriver(self.config)
        self.pool=ThreadPoolExecutor(3);self.addCleanup(lambda:self.pool.shutdown(wait=True))
        self.authority=BodyControl(self.config,self.root/'brain',self.driver,self.pool)
        self.addCleanup(self.authority.close)
        register_body_control(control_app,SimpleNamespace(body_control=self.authority,start=self.authority.start))
        self.serve(self.http_server)
        self.store=KernelStore(self.root/'brain')
        self.vla_http=HttpClient(vla_url,timeout_sec=2)
        self.addCleanup(self.drain)
        # Initial measured-standstill history, without an active task. This seed
        # lets the actual status implementation ask its injected physical driver.
        old=self.bridge.TaskRecord(vla_request('history')['body'],'fixture-history')
        old.state='succeeded';old.exit_state={'policy_stopped':True}
        self.runtime._tasks[old.command_id]=old

    def drain(self):
        for event in self.runtime._cancel.values(): event.set()
        end=time.monotonic()+3
        while self.runtime._active_command_id is not None and time.monotonic()<end: time.sleep(.01)

    def schedule(self, request):
        old=self.store.load_state() or {'revision':0,'steps':{}}
        command=request['body']['command_id']
        old.update(task_id='test-task',state='running',open_command_id=command,dispatch_closed=False,control_request='',
                   execution_backend='reception_real', execution_target={
                       'dream':self.config['reception_real']['dream_base_url'],
                       'vla':self.config['reception_real']['vla_base_url']})
        old['steps'][command]={'attempts':[{'attempt_id':command+'-a1','command_id':command,'request':deepcopy(request)}]}
        self.store.save_state(old)

    def prepare_source_navigation(self):
        request=nav_request();self.schedule(request)
        ref=self.authority.prepare_navigation(request)
        self.wire.nav_command(dict(request['body'],control_authorization=ref))
        return ref

    def submit_vla(self, command='open-1'):
        request=vla_request(command);self.schedule(request)
        self.authority.register(request);self.authority.record_submission(request,request['body'])
        self.vla_http.request_json('POST','/v1/vla/tasks',request['body'],accepted_statuses=(200,202))
        return self.wait_task(command, {'running','failed','cancelled'})

    def wait_task(self, command, states):
        deadline=time.monotonic()+4
        value={}
        while time.monotonic()<deadline:
            value=self.vla_http.request_json('GET','/v1/vla/tasks/'+command)[0]
            if value['state'] in states:return value
            time.sleep(.01)
        self.fail('Peer task did not reach '+str(states)+': '+str(value.get('state')))

    def finish(self,command='open-1'):
        self.vla_http.request_json('POST',f'/v1/vla/tasks/{command}/pause',{'task_id':'test-task'},accepted_statuses=(200,202))
        self.wait_task(command,{'waiting_result_confirmation'})
        self.vla_http.request_json('POST',f'/v1/vla/tasks/{command}/finish',
            {'task_id':'test-task','evidence':evidence()},accepted_statuses=(200,202),
            headers={'Authorization':'Bearer '+self.runtime._completion_token})
        return self.wait_task(command,{'succeeded','failed'})

    def test_actual_relay_accepts_navigation_preparation_authorization(self):
        ref=self.prepare_source_navigation()
        self.assertTrue(self.relay.enabled)
        self.assertEqual(self.relay.current['lease_id'],ref['lease_id'])
        self.authority.settle_navigation('test-task','nav-1')
        self.assertFalse(self.relay.enabled)

    def test_actual_native_task_release_does_not_restore_navigation(self):
        self.prepare_source_navigation()
        task=self.submit_vla()
        self.assertEqual(task['state'],'running',task.get('error'))
        result=self.finish()
        self.assertEqual(result['state'],'succeeded',result.get('error'))
        self.assertTrue(result['result']['authorization_released'])
        self.assertIsNone(result['result']['navigation_port_ready'])
        self.assertFalse(self.relay.enabled)
        self.assertTrue(self.authority.released('test-task','open-1'))
        port=ControlledBodyAdapter(DreamClient(self.config['reception_real']['dream_base_url']),
                                   VlaClient(self.config['reception_real']['vla_base_url']),self.authority)
        viewed=port.query('open-1',vla_request())
        self.assertTrue(viewed['stopped'])
        self.assertTrue(viewed['resources_released'])
        self.assertTrue(viewed['evidence']['supports'])

    def test_actual_stop_record_is_consumed_while_release_runs(self):
        self.prepare_source_navigation()
        task=self.submit_vla();self.assertEqual(task['state'],'running',task.get('error'))
        stopped=self.authority.stop_vla('test-task','open-1')
        self.assertEqual(stopped['state'],'stopped',stopped.get('error'))
        result=self.wait_task('open-1',{'failed'})
        self.assertEqual(result['error']['code'],'OUTPUT_STOPPED_BEFORE_COMPLETION')
        self.assertFalse(self.runtime.task('open-1').cancel_requested)
        self.assertTrue(result['result']['authorization_released'])

    def test_actual_client_recovers_lost_acquire_reply_without_second_post(self):
        self.prepare_source_navigation()
        original=self.client.call;posts=[]
        def call(method,path,body=None):
            reply=original(method,path,body)
            if method=='POST' and path.endswith('/acquire'):
                posts.append(deepcopy(body))
                if len(posts)==1:raise OSError('offline lost response after actual HTTP request')
            return reply
        with patch.object(self.client,'call',side_effect=call):
            task=self.submit_vla()
        self.assertEqual(task['state'],'running',task.get('error'))
        self.assertEqual(len(posts),1)
        self.assertEqual(self.finish()['state'],'succeeded')

    def test_actual_relay_historical_close_cannot_close_next_navigation(self):
        self.prepare_source_navigation()
        self.authority.settle_navigation('test-task','nav-1')
        old=next(op['request'] for op in self.relay.requests.values() if op['request']['action']=='close')
        request=nav_request('nav-2');self.schedule(request)
        ref=self.authority.prepare_navigation(request)
        http=HttpClient(self.config['reception_real']['body_control']['relay_base_url'])
        http.request_json('POST','/v1/relay/control',old,accepted_statuses=(200,202),
                          headers={'Authorization':'Bearer '+TOKEN})
        self.assertTrue(self.relay.enabled)
        self.assertEqual(self.relay.current['lease_id'],ref['lease_id'])

    def test_continuous_vla_requires_factual_pending_idle_status(self):
        self.prepare_source_navigation()
        self.assertEqual(self.submit_vla()['state'],'running')
        self.assertEqual(self.finish()['state'],'succeeded')
        second=self.submit_vla('open-2')
        self.assertEqual(second['state'],'running',second.get('error'))

    def test_production_navigation_prepare_keeps_missing_fresh_stream_blocked(self):
        from types import MethodType
        self.driver.prepare_navigation=MethodType(ControlDriver.prepare_navigation,self.driver)
        with self.assertRaisesRegex(ControlError, 'upstream_fresh'):
            self.prepare_source_navigation()
        self.relay.tick()
        self.assertFalse(self.relay.enabled)

    def test_peer_relay_can_serialize_fresh_stream_evidence(self):
        status=self.transport.status
        with patch.object(self.transport,'status',side_effect=lambda:{**status(),'upstream_fresh':True}):
            self.assertTrue(self.relay.status()['upstream_fresh'])
