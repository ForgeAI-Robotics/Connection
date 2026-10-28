"""Opt-in brain + real VLA HTTP/worker + gateway/O6 code, synthetic hardware."""
import os
from pathlib import Path
import sys
import time
import ssl
import subprocess
import tempfile
import threading
from copy import deepcopy
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
import unittest

from tests.brain import test_vla_gateway_peer as vla_peer


class VlaGatewayJointPeerTests(vla_peer.VlaGatewayPeerTests):
    @classmethod
    def setUpClass(cls):
        source=os.environ.get('CONNECTION_GATEWAY_SOURCE')
        if not source or not os.environ.get('CONNECTION_VLA_SOURCE'):
            raise unittest.SkipTest('Set both CONNECTION_GATEWAY_SOURCE and CONNECTION_VLA_SOURCE for joint isolated tests')
        super().setUpClass()
        cls.gateway_paths=[str(Path(source).resolve()/p) for p in ('', 'tests', 'tests/vla_gateway')]
        sys.path[:0]=cls.gateway_paths
        import test_managed_gateway
        cls.gateway_fixture=test_managed_gateway
        cls.tls_directory=tempfile.TemporaryDirectory(prefix='brain-vla-test-tls-')
        cls.addClassCleanup(cls.tls_directory.cleanup)
        cls.cert=Path(cls.tls_directory.name)/'cert.pem'
        cls.key=Path(cls.tls_directory.name)/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',str(cls.key),'-out',str(cls.cert),'-subj','/CN=localhost',
            '-addext','subjectAltName=IP:127.0.0.1'],check=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        for p in cls.gateway_paths:sys.path.remove(p)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        from g1_gateway.core import Gateway
        from g1_brain_vla_bridge.gateway_client import GatewayClient
        from g1_brain_vla_bridge.tests.test_gateway_o6 import GatewayO6Tests
        case=GatewayO6Tests();case.setUp();self.addCleanup(case.doCleanups)
        case.now=time.monotonic()
        controller='boot:1:2'
        case.native=lambda:dict(controller_instance_id=controller,phase=case.phase,safety_override=False,
            input_session_id=case.session,reference_retained=case.phase=='retained',receiver_accepting=case.phase=='active')
        case.s.native=case.native
        native=self.gateway_fixture.ManagedNative(case)
        native.sample['controller_instance_id']=controller
        native_call=native.call
        def call(body):
            reply=native_call(body)
            evidence=reply['result']['evidence']
            if body['operation']=='prepare':evidence['input_generation']=1
            if body['operation']=='activate':evidence['planner_engine_disabled']=True
            if body['operation']=='retain':evidence['initial_source_binding_unknown']=False
            return reply
        native.call=call
        for _ in range(5):
            case.now=time.monotonic()
            for side in ('left','right'):case.s.observe(side,case.s.targets[side],case.now)
            case.s.renew()
            time.sleep(.2)
        original=native.snapshot
        last_feedback=[case.now]
        def snapshot():
            case.now=time.monotonic()
            if case.now-last_feedback[0]>=.05:
                for side in ('left','right'):case.s.observe(side,case.s.targets[side],case.now)
                last_feedback[0]=case.now
            value=original()
            value['observed_monotonic']=case.now
            value['body_hold_verified']=True
            return value
        native.snapshot=snapshot
        cfg=dict(robot_serial=case.s.robot_serial,control_resource='g1:'+case.s.robot_serial+':body',
            permit_mode='control_session',hand_policy='per_command',
            nav_endpoint='tcp://127.0.0.1:5556',vla_endpoint='tcp://127.0.0.1:0')
        self.gateway=Gateway(cfg,str(self.authority.root/'real-gateway.sqlite'),native)
        self.addCleanup(self.gateway.close)
        self.addCleanup(self.authority.close)
        self.gateway.observe()
        self.gateway_calls=[]
        def http(role,method,path,body=None):
            self.gateway.observe()
            self.gateway_calls.append((role,method,path,body))
            route=urlsplit(path).path
            if method=='POST':return self.gateway.submit(body,role,route.rsplit('/',1)[-1])
            if '/requests/' in route:return self.gateway.query(route.rsplit('/',1)[-1],role)
            return self.gateway.status(parse_qs(urlsplit(path).query).get('observation_id',[None])[0])
        self.driver.clients['gateway']=SimpleNamespace(request_json=lambda m,p,b=None,**kw:
            (http('brain',m,p,b),200,{}))
        client=GatewayClient.__new__(GatewayClient);client.config={}
        client.http=lambda m,p,b=None:(http('vla',m,'/v1/body-input/'+p,b),0)
        self.peer.driver.gateway=client
        # Only native/body/encoder IO is synthetic. Keep actual gateway, source
        # operations, GatewayClient validators, O6 permissions and receipts.
        hardware=self.peer.hw
        def observe(**kw):
            self.gateway.observe()
            return dict(native=dict(available=True,controller_instance_id=controller,socket_path='/no/native',executable='/no/native'),
                        o6=dict(status=case.s.status()))
        hardware.observe=observe
        hardware.sample=lambda view:dict(phase=case.phase,reference_retained=case.phase=='retained',
            receiver_accepting=case.phase=='active',retained_reference_id='ref',
            static_external_token_window=True,safety_override=False)
        hardware.reference=lambda view:(hardware.sample(view),[.1]*64)
        hardware.hold=lambda *_:dict(hold_verified=case.phase=='retained',hands_stopped=case.s.fenced,
                                     controller_instance_id=controller)
        hardware.env['SONIC_ACTION_PORT']='0'
        from g1_brain_vla_bridge.session_wire import tag_frame
        self.input_sequence=0
        def send(packet):
            self.input_sequence+=1
            packet=tag_frame(packet,self.peer.lifecycle.context['input_session_id'],self.input_sequence)
            forwarded=self.gateway.forward(packet,'vla',lambda _:None)
            if forwarded:case.s.accept(packet,dict(case.s.targets))
            self.gateway.observe()
        self.peer.pub.send=send
        self.hands=case
        launch=self.peer.driver.call
        def launch_with_feedback(args,**kw):
            result=launch(args,**kw)
            if 'start-detached' in args:
                deadline=time.monotonic()+.65
                while time.monotonic()<deadline:
                    self.gateway.observe()
                    time.sleep(.02)
            return result
        self.peer.driver.call=launch_with_feedback
        self._enable_gateway_tls()
        self.native_step_errors=[]
        step=self.gateway._step
        def observed_step(request,operation):
            try:return step(request,operation)
            except Exception as exc:
                import traceback
                self.native_step_errors.append((operation,type(exc).__name__,str(exc),traceback.format_exc()))
                raise
        self.gateway._step=observed_step

    def _enable_gateway_tls(self):
        from g1_gateway.service import handler
        from brain.adapters.http_client import HttpClient
        from g1_brain_vla_bridge.gateway_client import GatewayClient
        from shared.service_credentials import service_token
        tokens={'brain':service_token('BRAIN_GATEWAY_TOKEN'), 'vla':'isolated-vla-role-'+'v'*32,
                'navigation':'isolated-nav-role-'+'n'*32}
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.gateway,tokens))
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(self.cert,self.key)
        server.socket=ctx.wrap_socket(server.socket,server_side=True)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        stop=threading.Event()
        def observe():
            while not stop.is_set():
                self.gateway.observe();stop.wait(.02)
        observer=threading.Thread(target=observe,daemon=True);observer.start()
        def shutdown():
            server.shutdown();server.server_close();worker.join(2)
            stop.set();observer.join(2)
        self.addCleanup(shutdown)
        # Authority exit must finish while its transport and observer still exist.
        self.addCleanup(self.authority.close)
        url='https://127.0.0.1:'+str(server.server_port)
        brain=HttpClient(url,ca_file=str(self.cert),timeout_sec=2)
        brain.auth_token_env='BRAIN_GATEWAY_TOKEN'
        request_json=brain.request_json
        def request(method,path,payload=None,**kw):
            self.gateway_calls.append(('brain',method,path,deepcopy(payload)))
            return request_json(method,path,payload,**kw)
        brain.request_json=request
        self.driver.clients['gateway']=brain
        token=self.authority.root/'vla-test.token';token.write_text(tokens['vla']);token.chmod(0o600)
        client=GatewayClient(dict(base_url=url,ca_file=str(self.cert),token_file=str(token)))
        original=client.http
        def http(method,path,body=None):
            self.gateway_calls.append(('vla',method,'/v1/body-input/'+path,deepcopy(body)))
            return original(method,path,body)
        client.http=http;self.peer.driver.gateway=client
        # The inherited read-only query assertion now counts real wire writes.
        self.wire.calls=self.gateway_calls
        self.vla_role_token=tokens['vla']

    def delayed_hand_status(self, seconds, *, changed_permit=False):
        """Delay only status projection, after real O6 installation completed."""
        original=self.gateway.status
        entered=threading.Event();deadline=[None];self.delayed_reads=0
        def status(*args,**kwargs):
            value=original(*args,**kwargs)
            if self.hands.s.permission is not None and (value.get('permit') or {}).get('phase')=='prepare':
                if deadline[0] is None:deadline[0]=time.monotonic()+seconds
                if time.monotonic()<deadline[0]:
                    self.delayed_reads+=1;entered.set()
                    if changed_permit:
                        value['permit']['control_session_id']='foreign-observation'
                    else:
                        value['o6'].update(permission=None,control_session_id=None,input_session_id=None,hand_mode=None)
            return value
        self.gateway.status=status
        return entered

    def assert_no_business_execution(self):
        self.assertEqual(self.peer.resume_count,0)
        self.assertFalse(any(c[1]=='POST' and (c[3] or {}).get('operation')=='allow_execute'
                             for c in self.gateway_calls))
        self.assertFalse(self.authority.submission_attempted('test-task','open-1'))

    def test_delayed_status_waits_without_replaying_prepare(self):
        self.delayed_hand_status(.25)
        result=self.port.submit('open-1',self.request)
        self.assertTrue(result['accepted'],result.get('error'))
        self.peer.wait(lambda:self.peer.runtime.task('open-1').state=='running')
        self.assertGreater(self.delayed_reads,1)
        self.assertEqual(len([r for r in self.gateway.native.effects if r['operation']=='prepare']),1)
        self.assertEqual(len([r for r in self.gateway.native.hands.calls if r['action']=='install']),1)
        self.assertEqual(self.peer.resume_count,1)
        self.assertTrue(self.port.cancel('open-1',self.request)['completed'])

    def test_cancel_interrupts_delayed_status_before_execute(self):
        entered=self.delayed_hand_status(10)
        pending=self.authority.executor.submit(self.port.submit,'open-1',self.request)
        self.assertTrue(entered.wait(3))
        task=self.store.load_state();task.update(state='cancelled',dispatch_closed=True);self.store.save_state(task)
        self.authority.cancel_task('test-task')
        result=pending.result(5)
        self.assertFalse(result['accepted'])
        self.assert_no_business_execution()
        self.peer.wait(lambda:self.peer.runtime.task('open-1').state in ('cancelled','failed'))

    def test_changed_permission_stops_instead_of_waiting_three_seconds(self):
        entered=self.delayed_hand_status(10,changed_permit=True)
        started=time.monotonic();result=self.port.submit('open-1',self.request)
        self.assertTrue(entered.is_set());self.assertFalse(result['accepted'])
        self.assertLess(time.monotonic()-started,3)
        self.assert_no_business_execution()

    def test_missing_status_exhausts_bounded_wait_without_replay(self):
        self.delayed_hand_status(10)
        started=time.monotonic();result=self.port.submit('open-1',self.request)
        self.assertFalse(result['accepted'])
        self.assertGreaterEqual(time.monotonic()-started,3)
        self.assertLess(time.monotonic()-started,5)
        self.assert_no_business_execution()
        self.assertEqual(len([r for r in self.gateway.native.effects if r['operation']=='prepare']),1)

    def test_lost_prepare_reply_queries_same_vla_request(self):
        from brain.adapters.http_client import HttpContractError
        client=self.driver.clients['vla'];original=client.request_json;posts=[]
        def request(method,path,payload=None,**kw):
            value=original(method,path,payload,**kw)
            if method=='POST' and path.endswith('/handoff/prepare'):
                posts.append(payload['request_id'])
                raise HttpContractError('synthetic reply loss after real acceptance')
            return value
        client.request_json=request
        result=self.port.submit('open-1',self.request)
        self.assertTrue(result['accepted'],result.get('error'))
        self.peer.wait(lambda:self.peer.runtime.task('open-1').state=='running')
        self.assertEqual(len(posts),1);self.assertEqual(self.peer.launches,1)
        self.assertEqual(self.peer.resume_count,1)
        self.assertTrue(self.port.cancel('open-1',self.request)['completed'])

    def test_vla_credential_cannot_grant_over_tls(self):
        from brain.adapters.http_client import HttpContractError
        request=self.gateway_fixture.q(self.gateway,controller_instance_id='boot:1:2')
        with self.assertRaises(HttpContractError) as error:
            self.driver.clients['gateway'].request_json('POST','/v1/body-input/permits',request,
                headers={'Authorization':'Bearer '+self.vla_role_token})
        self.assertEqual(error.exception.status_code,403)
        self.assertIsNone(self.gateway.permit)

    def test_current_fence_wire_is_accepted_by_actual_gateway(self):
        result=self.port.submit('open-1',self.request)
        self.assertTrue(result['accepted'],(result.get('error'),dict(self.gateway.counts),
            {k:self.hands.s.status().get(k) for k in ('preparation_received','error','hand_mode','receiver_output_fenced')}))
        self.peer.wait(lambda:self.peer.runtime.task('open-1').state=='running')
        self.assertTrue(self.port.cancel('open-1',self.request)['completed'])
        row=self.authority.command_row('test-task','open-1')
        requests=self.authority.store.read()['requests'].values()
        fence=next(op['children']['gateway-fence'] for op in requests if 'gateway-fence' in op['children'])
        self.assertEqual(fence['state'],'confirmed',fence)
        self.assertEqual(fence['wire']['payload']['o6_instance_id'],row['gateway_binding']['o6_instance_id'])

    def test_cancel_after_grant_before_hand_install_uses_actual_gateway_proof(self):
        g=self.gateway;fixture=self.gateway_fixture
        grant=fixture.q(g,controller_instance_id='boot:1:2')
        fixture.complete(g,grant)
        self.assertIsNone(self.hands.s.permission)
        revoke=dict(grant,operation='revoke',permit_seq=2,request_id='cancel-before-hands')
        fixture.complete(g,revoke)
        try:
            fixture.wait(g,lambda:g.status()['exit_status'].get('exit_confirmed') is True)
        except AssertionError:
            self.fail(str({'exit':g.status()['exit_status'], 'effects':g.native.effects,'errors':self.native_step_errors,
                           'steps':g.db.execute('SELECT body,receipt FROM steps').fetchall()}))
        status=g.status()
        self.assertTrue(status['exit_status']['o6_never_bound'])
        proof=self.driver.verify_exit(dict(target='vla',gateway_binding=grant),{},status)
        self.assertTrue(proof['exit_confirmed'])
        self.assertIsNone(self.hands.s.permission)
