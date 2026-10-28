"""NAV gateway authority contract tests. Temporary ledgers, no robot network IO."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

from brain.adapters.body_control import ControlDriver, ControlledBodyAdapter
from brain.adapters.http_client import HttpContractError
from brain.kernel.body_control import BodyControl
from brain.storage.tasks import KernelStore
from contracts.body_control import VERSION, REVISION, PERMIT_MODE, GATEWAY_BINDING, ControlError, stamp
from contracts.tasks import CONTRACT_VERSION


def configuration(root):
    return {'reception_real': {'kernel_enabled': True, 'kernel_runtime_dir': str(root),
        'dream_base_url': 'http://nav.invalid', 'vla_base_url': 'http://vla.invalid',
        'body_control': dict(enabled=True, permit_mode=PERMIT_MODE, robot_serial='test-robot', control_resource='g1:test-robot:body',
            action_endpoint='tcp://127.0.0.1:5556', gateway_base_url='https://gateway.invalid',
            gateway_ca_file='/unused/test-ca.pem', gateway_timeout_sec=.02,
            operation_timeout_sec=.15, poll_interval_sec=.002, renew_interval_sec=.08)}}


def nav_request(command='nav-1'):
    return dict(skill='navigate', task_id='test-task', body=dict(contract_version=CONTRACT_VERSION,
        task_id='test-task', command_id=command, target_id='fridge', route_phase='', leg_index=1,
        frame_id='map', goal_xyt=[0, 0, 0], motion_mode='forward_path', require_final_orientation=True))


class Wire:
    """Independent peer state: stage, tombstones, exact identities and expiry enforced."""
    def __init__(self):
        self.calls, self.requests, self.commands = [], {}, {}
        self.permit = self.last = None
        self.anchors, self.retired = {}, set()
        self.instance = 'gateway-1'
        self.controller = '00000000-0000-4000-8000-000000000001:123:10'
        self.session = 0
        self.fail_fence = self.stale = self.block_prepare = False
        self.drop = self.corrupt = None
        self.entered, self.unblock = threading.Event(), threading.Event()
        self.block_operation = None
        self.isolated = True
        self.active_session = None
        self.lock = threading.RLock()
        self.nav = dict(interface_online=True, active_command_id='', output_fenced=True, hold_verified=True)

    def client(self, service):
        return SimpleNamespace(request_json=lambda method, path, payload=None, **kw:
                               (self.call(service, method, path, payload), 200, {}))

    def status(self, observation):
        now=time.monotonic()
        challenge=observation or 'no-anchor'
        self.anchors.setdefault(challenge, now)
        native=dict(controller_instance_id=self.controller, hold_reference_id=None, o6_instance_id=None, hand_participation='not_required', old_hand_task_pending=False,
            receiver_isolated=self.isolated, body_stopped=True, body_hold_verified=True, o6_hold_verified=None, o6_output_fenced=None,
            observed_monotonic=now-(2 if self.stale else 0), active_session=self.active_session)
        native['sample']=dict(low_state_monotonic_ns=int(now*1e9),safety_override=False,policy_output_selected=True)
        p=deepcopy(self.permit)
        return dict(contract_version=VERSION,coordination_revision=REVISION,gateway_instance_id=self.instance,
            permit=p,last_binding=deepcopy(self.last),native=native,observation_error=None,
            hand_policy='body_only',supported_hand_modes=['not_required'],exit_status=self.exit_status(),
            permit_mode=PERMIT_MODE, renew_required=False, observation_id=observation, features=['brain-takeover-v1'],
            freshness=self.freshness())

    def freshness(self):
        now=time.monotonic()
        return dict(generated_at=stamp(), generated_monotonic_ns=int(now*1e9),
                    gateway_instance_id=self.instance, sample_age_ms=2000 if self.stale else 0,
                    sample_fresh=not self.stale, body_sample_age_ms=0, body_sample_fresh=True)

    def hold(self, raw):
        p=self.permit or self.last
        return dict(applicable=True,identity={k:raw[k] for k in ('control_session_id','task_id','command_id','command_seq')},
                    command_stopped=True,motion_held=True,body_hold_verified=True,
                    control_session_retained=self.permit is not None,session_released=False,permission_state='installed',
                    **{k:p[k] for k in ('publisher_instance_id','input_session_id','controller_instance_id')},
                    freshness=self.freshness())

    def exit_status(self):
        if self.last is None:return None
        return dict(identity={k:deepcopy(self.last[k]) for k in GATEWAY_BINDING},
            exit_confirmed=self.isolated and self.permit is None and not self.fail_fence,
            body_forwarding_fenced=self.permit is None,receiver_callbacks_sealed=self.isolated,
            receiver_decoded_caches_cleared=self.isolated and not self.fail_fence,
            input_session_retired=self.isolated and not self.fail_fence,body_stopped=True,body_hold_verified=True,
            hand_participation='not_required',o6_output_fenced=None,o6_hold_verified=None,hold_reference_id=None)

    def call(self, service, method, path, payload=None):
        clean=urlsplit(path).path
        self.calls.append((service,method,path,deepcopy(payload)))
        op=(payload or {}).get('operation') or (payload or {}).get('action') or (clean.rsplit('/',1)[-1] if method=='POST' else None)
        if method=='POST' and op==self.block_operation:
            self.entered.set()
            if not self.unblock.wait(3): raise RuntimeError('test timeout')
        with self.lock:
            if method=='GET':
                if clean.endswith('/status'):
                    if service=='gateway':return self.status(parse_qs(urlsplit(path).query).get('observation_id',[''])[0])
                    return dict(self.nav,updated_at=stamp())
                if '/commands/' in clean:
                    raw=deepcopy(self.commands[clean.rsplit('/',1)[1]])
                    if raw.get('state') in ('succeeded','failed','cancelled'): raw['current_hold']=self.hold(raw)
                    return raw
                key=(service,clean.rsplit('/',1)[1])
                if key not in self.requests:raise HttpContractError('missing original',status_code=404,payload={'error':{'code':'UNKNOWN_REQUEST'}})
                r=deepcopy(self.requests[key])
                if service=='nav' and r.get('action')=='prepare' and self.permit and not self.block_prepare:
                    r.update(state='completed',current_local_state='prepared',effect_status='confirmed',accepted=True,current_prepare_valid=True)
                    r['evidence'].update(mode_confirmed=True,hold_verified=True,navigation_not_started=True,
                        fresh_preparation_output=True,hand_participation='not_required',hold_scope='body')
                    self.active_session=r['evidence']['publisher']['input_session_id'];self.isolated=False
                    self.requests[key]=deepcopy(r)
                if service=='nav':r['freshness']=self.freshness()
                return r
            if service=='nav' and clean.endswith('/cancel'):
                command=self.commands.get(payload['command_id'])
                if command:
                    command.update(state='cancelled',completed_at=stamp(),result={'navigation_stopped':True})
                r=dict(request_id=payload['request_id'],state='completed',accepted=True,identity=deepcopy(payload))
                self.requests[(service,payload['request_id'])]=r
                return deepcopy(r)
            key=(service,payload['request_id'])
            if key in self.requests:
                return deepcopy(self.requests[key])
            if service=='nav':
                r=dict(contract_version=VERSION,coordination_revision=REVISION,request_id=payload['request_id'],
                    action=op,accepted=True,execution_authority=False,historical=False,updated_at=stamp(),
                    effect_status='unknown',permit_mode=PERMIT_MODE,control_session_id=payload['control_session_id'],
                    identity=deepcopy(payload),nav_instance_id='nav-process',query_nav_instance_id='nav-process',
                    freshness=self.freshness())
                if op=='prepare':
                    self.session+=1
                    r.update(state='pending',current_local_state='preparing',execution_stage='session_registered',
                        evidence={'publisher':dict(publisher_instance_id='pub-'+str(self.session),input_session_id=f'{self.session:032x}')})
                    r['registration']=dict(r['evidence']['publisher'],controller_instance_id=self.controller)
                else:
                    self.nav.update(active_command_id='',output_fenced=True)
                    r.update(state='completed',current_local_state='yielded',effect_status='confirmed',
                        evidence=dict(navigation_stopped=True,output_fenced=True,hold_verified=True))
            else:
                r=dict(contract_version=VERSION,coordination_revision=REVISION,gateway_instance_id=self.instance,
                    request_id=payload['request_id'],operation=op,identity=deepcopy(payload),state='completed',
                    accepted=True,effect_status='confirmed',evidence={},updated_at=stamp())
                lease=payload['control_authorization']['lease_id']
                if op=='revoke':
                    self.retired.add(lease)
                    if self.permit and self.permit['control_authorization']['lease_id']==lease:
                        self.last,self.permit=self.permit,None
                    r['evidence']={'permit_revoked':True,'exit_confirmed':False}
                elif op=='fence':
                    self.isolated=True;self.active_session=None
                    r['evidence']=dict(body_forwarding_fenced=True,receiver_callbacks_sealed=True,
                        receiver_decoded_caches_cleared=not self.fail_fence,input_session_retired=not self.fail_fence,
                        native=dict(state='completed',controller_instance_id=self.controller,
                            identity={k:deepcopy(payload[k]) for k in ('control_session_id','control_authorization','publisher_instance_id','input_session_id')}))
                else:
                    assert payload['hand_mode']=='not_required' and payload['hold_reference_id'] is None
                    if op=='grant' and self.stale:
                        r.update(state='unknown',accepted=False,effect_status='unknown',blockers=['NATIVE_OBSERVATION_STALE'])
                        self.requests[key]=deepcopy(r)
                        return r
                    self.status('expiry-check')
                    if lease in self.retired:
                        r.update(state='rejected',accepted=False,effect_status='not_started',blockers=['TARGET_RETIRED'])
                    else:
                        if op!='grant':
                            assert self.permit and payload['permit_seq']>self.permit['permit_seq']
                            assert payload['phase']==('execute' if op=='allow_execute' else self.permit['phase'])
                        assert 'freshness_ref' not in payload and 'lease_duration_ms' not in payload
                        self.permit={k:deepcopy(payload[k]) for k in (*GATEWAY_BINDING,'phase','permit_seq','hand_mode','hold_reference_id')}
                        self.permit.update(permit_mode=PERMIT_MODE,valid_until_monotonic=None)
                        self.last=deepcopy(self.permit)
                        r['evidence']=dict(permit_installed=True,permit=deepcopy(self.permit),exit_confirmed=False)
                if op==self.corrupt:r['identity']['task_id']='foreign'
            self.requests[key]=deepcopy(r)
            if op==self.drop:
                self.drop=None
                raise HttpContractError('lost POST response')
            return r


class Fixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,{'BRAIN_CONTROL_TOKEN':'nav-test-secret','BRAIN_GATEWAY_TOKEN':'gateway-test-secret'})
        self.env.start();self.addCleanup(self.env.stop)
        self.root=Path(self.temp.name);self.config=configuration(self.root)
        self.wire=Wire()
        self.driver=ControlDriver(self.config,clients={k:self.wire.client(k) for k in ('nav','gateway')})
        self.pool=ThreadPoolExecutor(4);self.addCleanup(self.pool.shutdown)
        self.authority=BodyControl(self.config,self.root,self.driver,self.pool)
        self.addCleanup(self.authority.close)
        self.store=KernelStore(self.root)
    def schedule(self, request):
        old=self.store.load_state() or {'steps':{}}
        old.update(task_id=request['body']['task_id'],state='running',execution_backend='reception_real',
            execution_target={'dream':self.config['reception_real']['dream_base_url'],'vla':self.config['reception_real']['vla_base_url']},
            open_command_id=request['body']['command_id'],dispatch_closed=False,control_request='')
        old['steps'][request['body']['command_id']]={'attempts':[{'command_id':request['body']['command_id'],'request':deepcopy(request)}]}
        self.store.save_state(old)
    def prepare(self, command='nav-1'):
        r=nav_request(command);self.schedule(r)
        ref=self.authority.prepare_navigation(r)
        return r,ref
    def posts(self, operation):
        return [c for c in self.wire.calls if c[1]=='POST' and (c[3].get('operation') or c[3].get('action') or urlsplit(c[2]).path.rsplit('/',1)[-1])==operation]
    def row(self):
        return next(reversed(self.authority.store.read()['leases'].values()))


class AuthorityTests(Fixture,unittest.TestCase):
    def test_prepare_grant_execute_then_goal_and_no_relay_or_vla(self):
        request,ref=self.prepare()
        self.authority.record_submission(request,dict(request['body'],control_authorization=ref))
        self.assertEqual([x[3].get('operation') or x[3].get('action') or urlsplit(x[2]).path.rsplit('/',1)[-1] for x in self.wire.calls if x[1]=='POST'],
                         ['prepare','grant','allow_execute'])
        self.assertEqual({x[0] for x in self.wire.calls},{'nav','gateway'})
        self.assertEqual(len(self.posts('prepare')),1)
    def test_lost_grant_reply_queries_original_without_second_post(self):
        self.wire.drop='grant';self.prepare()
        self.assertEqual(len(self.posts('grant')),1)
        self.assertTrue(any(c[0]=='gateway' and c[1]=='GET' and '/requests/' in c[2] for c in self.wire.calls))
    def test_wrong_identity_never_authorizes_goals(self):
        self.wire.corrupt='grant'
        with self.assertRaises(ControlError):self.prepare()
        self.assertFalse(self.posts('allow_execute'))
    def test_queries_are_read_only_and_do_not_refresh_permission(self):
        _,ref=self.prepare();self.authority._stop.set()
        before=self.authority.store.path.read_bytes();calls=len(self.wire.calls)
        self.authority.query(ref['lease_id'],observation_id='read-1')
        self.authority.recovery_status()
        self.assertEqual(before,self.authority.store.path.read_bytes());self.assertEqual(calls,len(self.wire.calls))
    def test_control_session_has_no_periodic_renew_or_deadline(self):
        request,_=self.prepare();time.sleep(.2)
        self.assertFalse(self.posts('renew'))
        self.assertIsNone(self.wire.permit['valid_until_monotonic'])
        self.assertFalse(hasattr(self.authority,'_renew_thread'))
        self.authority.record_submission(request,self.authority.business_body(request))
    def test_revoked_session_prevents_business_submission(self):
        request,_=self.prepare();self.authority._mark_revoked(self.row()['lease_id'])
        with self.assertRaises(ControlError):self.authority.record_submission(request,request['body'])
    def test_missing_native_or_stale_sample_never_grants(self):
        self.wire.stale=True
        with self.assertRaises(ControlError):self.prepare()
        self.assertFalse(self.posts('allow_execute'));self.assertEqual(len(self.posts('grant')),1)
    def test_restart_invalidates_permission_preserves_original_request(self):
        _,ref=self.prepare();before=deepcopy(self.authority.store.read()['requests'])
        self.authority.close()
        self.authority=BodyControl(self.config,self.root,self.driver,self.pool);self.addCleanup(self.authority.close)
        self.authority.start()
        self.assertFalse(self.authority.query(ref['lease_id'],observation_id='restarted')['authorization_current'])
        self.assertEqual(before,self.authority.store.read()['requests'])
        self.assertEqual(len(self.posts('grant')),1)
    def test_single_writer(self):
        self.authority.start();other=BodyControl(self.config,self.root,self.driver,self.pool)
        self.addCleanup(other.close)
        with self.assertRaises(BlockingIOError):other.start()
    def test_unscheduled_request_cannot_grant(self):
        with self.assertRaises(ControlError):self.authority.register(nav_request())
        self.assertFalse(self.wire.calls)
    def test_legacy_ledger_refused_without_reset(self):
        self.authority.start();self.authority.close()
        state=self.authority.store.read();state['schema']=1;self.authority.store.write(state)
        before=self.authority.store.path.read_bytes()
        other=BodyControl(self.config,self.root,self.driver,self.pool);self.addCleanup(other.close)
        with self.assertRaisesRegex(ControlError,'迁移'):other.start()
        self.assertEqual(before,self.authority.store.path.read_bytes())
    def test_vla_and_carrying_tasks_are_rejected(self):
        for change in ({'skill':'open_fridge'},{'required_holding':'cola_can_1'}):
            with self.subTest(change=change),self.assertRaises(ControlError):
                self.authority.register(dict(nav_request(),**change))
    def test_no_secrets_in_journal(self):
        self.prepare();text=self.authority.store.path.read_text()
        self.assertNotIn('nav-test-secret',text);self.assertNotIn('gateway-test-secret',text)
    def test_gateway_contract_and_native_identity_checked(self):
        self.prepare();row=self.row();status=self.wire.status('check')
        status['gateway_instance_id']='restarted'
        with self.assertRaises(ControlError):self.driver.verify_current_permit(row,status,'execute')
    def test_business_submission_is_exact_and_not_replayed(self):
        request,ref=self.prepare();body=dict(request['body'],control_authorization=ref)
        self.authority.record_submission(request,body)
        with self.assertRaises(ControlError):self.authority.record_submission(request,body)
        self.assertEqual(self.row()['business_submission']['body'],body)

    def test_restart_retires_only_proven_unsent_registration(self):
        request=nav_request();self.schedule(request)
        row=self.authority.register(request)
        self.authority.close()
        self.authority=BodyControl(self.config,self.root,self.driver,self.pool);self.addCleanup(self.authority.close)
        self.authority.start()
        result=self.authority.store.read()['leases'][row['lease_id']]
        self.assertEqual(result['state'],'released')
        self.assertTrue(result['evidence']['not_acquired'])
        self.assertFalse(result['evidence']['physical_readiness_verified'])
        self.assertFalse(self.wire.calls)
