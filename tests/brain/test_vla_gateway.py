"""Brain VLA admission/exit with independent synthetic peers; no robot IO."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from brain.adapters.body_control import ControlledBodyAdapter
from brain.adapters.http_client import HttpContractError
from brain.adapters.vla_control import EXTENSION, worksite
from contracts.body_control import VERSION, REVISION, PERMIT_MODE, GATEWAY_BINDING, ControlError, stamp
from tests.brain.test_body_control import Fixture, Wire, nav_request


def vla_request(command='open-1'):
    body = dict(contract_version='fq/reception-lan/v1', task_id='test-task', command_id=command,
                operation='open_fridge', object_id='fridge_1', target_area='fridge')
    now = datetime.now(timezone.utc)
    body['worksite_evidence'] = dict(schema='fq/vla-worksite-evidence/v1', kind='operator_ready',
        **{k:body[k] for k in ('task_id','command_id','operation','object_id','target_area')},
        reference='isolated-operator-observation', pose_reference='isolated-pose',
        robot_serial='test-robot', controller_instance_id=Wire().controller,
        ready=True, pose_verified=True, observed_at=now.isoformat(), valid_until=(now+timedelta(seconds=60)).isoformat())
    return dict(skill='open_fridge', task_id='test-task', body=body)


class VlaWire(Wire):
    def __init__(self):
        super().__init__()
        self.business = {}
        self.fail_hands = False
        self.fail_vla = False

    def status(self, observation):
        s = super().status(observation)
        s.update(hand_policy='per_command', supported_sources=['navigation','vla'],
                 supported_hand_modes=['not_required','hold','execute'],
                 features=['brain-takeover-v1', EXTENSION])
        p = self.permit or self.last or {}
        s['o6'] = dict(receiver_instance_id='o6-1', hold_reference_id='hand-1',
            control_session_id=p.get('control_session_id'), input_session_id=p.get('input_session_id'),
            hand_mode=p.get('hand_mode'), hold_verified=True, preparation_received=True,
            receiver_output_fenced=self.permit is None or p.get('phase') == 'prepare',
            permission={k:p.get(k) for k in ('control_authorization','controller_instance_id','gateway_instance_id','phase')},
            measured={k:dict(fresh=not self.fail_hands, age_sec=0) for k in ('left','right')})
        if p.get('source_id') == 'vla':
            s['native'].update(hand_participation='required', safety_override=False)
        return s

    def exit_status(self):
        s = super().exit_status()
        if s and self.last.get('source_id') == 'vla':
            s.update(hand_participation='required', o6_output_fenced=True,
                     o6_hold_verified=not self.fail_hands, hold_reference_id='hand-1')
        return s

    def call(self, service, method, path, payload=None):
        clean = urlsplit(path).path
        if service == 'vla':
            if self.fail_vla:
                raise AssertionError('NAV must not contact VLA')
            self.calls.append((service,method,path,deepcopy(payload)))
            if method == 'POST' and clean.endswith('/prepare'):
                body = payload['business_request']; c = body['control']
                self.assert_business(body, payload)
                self.session += 1
                b = dict(robot_serial='test-robot',control_resource='g1:test-robot:body',source_id='vla',
                    action_endpoint='tcp://vla.invalid:5556',control_session_id=c['control_session_id'],
                    control_authorization=c['control_authorization'],gateway_instance_id=self.instance,
                    controller_instance_id=self.controller,publisher_instance_id='vla-pub',
                    input_session_id=f'{self.session:032x}',o6_instance_id='o6-1',hold_reference_id='hand-1')
                r = dict(contract_version=VERSION,coordination_revision=REVISION,permit_mode=PERMIT_MODE,
                    extension=EXTENSION,request_id=payload['request_id'],request=deepcopy(payload),
                    task_id=body['task_id'],command_id=body['command_id'],action='prepare',
                    identity=dict(b,task_id=body['task_id'],command_id=body['command_id']),registration=b,
                    state='pending',accepted=False,historical=False,vla_instance_id='vla-1',query_vla_instance_id='vla-1',
                    checked_at=stamp(),updated_at=stamp(),current_prepare_valid=False,current_prepare_blockers=[])
                self.requests[(service,payload['request_id'])] = r
                self.business[body['command_id']] = deepcopy(body)
                if self.drop == 'vla-prepare':
                    self.drop = None
                    raise HttpContractError('lost original prepare reply')
                return deepcopy(r)
            if method == 'POST' and clean.endswith('/yield'):
                previous = next(r for (s,_),r in self.requests.items() if s == 'vla' and
                    r['registration']['control_session_id'] == payload['control_session_id'])
                r = deepcopy(previous)
                r.update(request=deepcopy(payload),request_id=payload['request_id'],action='yield',
                         state='completed',accepted=True,checked_at=stamp(),updated_at=stamp(),
                         evidence=dict(policy_stopped=True,publisher_stopped=True,output_fenced=True,hold_verified=True))
                self.requests[(service,payload['request_id'])] = r
                return deepcopy(r)
            if method == 'GET':
                r = deepcopy(self.requests[(service,clean.rsplit('/',1)[-1])])
                if r['action'] == 'prepare' and self.permit:
                    self.active_session=r['registration']['input_session_id']; self.isolated=False
                    r.update(state='completed',accepted=True,current_prepare_valid=True,
                             current_observation=self.status('vla-observation'),checked_at=stamp())
                return r
            raise AssertionError(path)
        if (service == 'gateway' and method == 'POST' and payload.get('source_id') == 'vla'
                and payload.get('operation') in {'grant','allow_execute'}):
            self.calls.append((service,method,path,deepcopy(payload)))
            self.permit=deepcopy(payload)
            self.permit.update(valid_until_monotonic=None)
            self.last=deepcopy(self.permit)
            r=dict(contract_version=VERSION,coordination_revision=REVISION,gateway_instance_id=self.instance,
                request_id=payload['request_id'],operation=payload['operation'],identity=deepcopy(payload),
                state='completed',accepted=True,effect_status='confirmed',updated_at=stamp(),
                evidence=dict(permit_installed=True,permit=deepcopy(self.permit)))
            self.requests[(service,payload['request_id'])]=r
            return deepcopy(r)
        return super().call(service,method,path,payload)

    @staticmethod
    def assert_business(body, payload):
        assert body['control']['prepare_request_id'] == payload['request_id']
        assert body['control']['command_seq'] == 1
        assert body['worksite_evidence']['control_session_id'] == body['control']['control_session_id']


class VlaGatewayTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.wire = VlaWire()
        self.driver.clients={k:self.wire.client(k) for k in ('nav','vla','gateway')}

    def prepare_vla(self, request=None):
        request=request or vla_request()
        self.schedule(request)
        self.authority.prepare_command(request)
        return request

    def test_vla_prepare_execute_exit_without_nav(self):
        r=self.prepare_vla()
        body=self.authority.business_body(r)
        self.assertEqual(body,self.wire.business['open-1'])
        self.assertEqual(body['control']['command_seq'],1)
        self.authority.record_submission(r,body)
        self.assertEqual(self.posts('grant')[0][3]['hand_mode'],'hold')
        self.assertEqual(self.posts('allow_execute')[0][3]['hand_mode'],'execute')
        self.assertEqual(self.posts('grant')[0][3]['action_endpoint'],'tcp://vla.invalid:5556')
        self.assertTrue(self.authority.release_control('test-task','open-1')['exit_confirmed'])
        self.assertEqual({c[0] for c in self.wire.calls},{'vla','gateway'})
        self.assertFalse(self.posts('renew'))

    def test_nav_on_extended_gateway_does_not_need_vla_or_hands(self):
        self.wire.fail_vla=True;self.wire.fail_hands=True
        self.prepare()
        self.assertEqual({c[0] for c in self.wire.calls},{'nav','gateway'})
        self.assertEqual(self.posts('grant')[0][3]['hand_mode'],'not_required')

    def test_stale_or_wrong_hands_never_get_execute(self):
        self.wire.fail_hands=True
        with self.assertRaises(ControlError):self.prepare_vla()
        self.assertFalse(self.posts('allow_execute'))

    def test_missing_extension_blocks_only_vla(self):
        original=self.wire.status
        def status(o):
            s=original(o);s['features'].remove(EXTENSION);s['hand_policy']='body_only';return s
        self.wire.status=status
        with self.assertRaises(ControlError):self.prepare_vla()
        self.assertFalse(self.posts('prepare'))

    def test_lost_prepare_only_queries_original(self):
        self.wire.drop='vla-prepare'
        self.prepare_vla()
        self.assertEqual(len(self.posts('prepare')),1)

    def test_old_or_invalid_worksite_never_sends_prepare(self):
        r=vla_request();r['body']['worksite_evidence']['valid_until']='2000-01-01T00:00:00+00:00'
        with self.assertRaises(ControlError):self.prepare_vla(r)
        self.assertFalse(self.posts('prepare'))

    def test_repeated_business_submission_never_reposts(self):
        r=self.prepare_vla();body=self.authority.business_body(r)
        self.authority.record_submission(r,body)
        with self.assertRaises(ControlError):self.authority.record_submission(r,body)

    def test_new_vla_command_uses_new_session(self):
        self.prepare_vla();old=self.row()
        self.authority.release_control('test-task','open-1')
        self.prepare_vla(vla_request('open-2'));new=self.row()
        self.assertNotEqual(old['control_session_id'],new['control_session_id'])
        self.assertEqual(new['command_seq'],1)
        self.assertGreater(new['ownership_epoch'],old['ownership_epoch'])
        self.authority.release_control('test-task','open-1')
        self.assertEqual(self.wire.permit['control_session_id'],new['control_session_id'])

    def test_prepare_cancel_late_registration_cannot_grant(self):
        entered, release = threading.Event(),threading.Event()
        original=self.wire.call
        def delayed(s,m,p,b=None):
            value=original(s,m,p,b)
            if s=='vla' and m=='POST' and p.endswith('/prepare'):
                entered.set();release.wait(2)
            return value
        self.wire.call=delayed
        r=vla_request();self.schedule(r)
        pending=self.pool.submit(self.authority.prepare_command,r)
        self.assertTrue(entered.wait(1))
        task=self.store.load_state();task.update(state='cancelled',dispatch_closed=True);self.store.save_state(task)
        with patch.object(self.authority,'_schedule_exit',return_value=None):
            self.authority.cancel_task('test-task')
            release.set()
            with self.assertRaises(ControlError):pending.result(2)
        self.assertFalse(self.posts('grant'))

    def test_gateway_management_read_failure_does_not_revoke(self):
        self.prepare_vla();before=len(self.posts('revoke'))
        with patch.object(self.driver,'status',side_effect=HttpContractError('offline')):
            with self.assertRaises(HttpContractError):self.driver.status('gateway')
        self.assertEqual(len(self.posts('revoke')),before)
        self.assertFalse(self.row().get('revoked',False))

    def test_hand_exit_missing_is_not_released(self):
        self.prepare_vla();self.wire.fail_hands=True
        with self.assertRaises(ControlError):self.authority.release_control('test-task','open-1')
        self.assertFalse(self.authority.released('test-task','open-1'))

    def test_plan_metadata_and_gate_do_not_contact_either_peer(self):
        peer=SimpleNamespace(status=lambda:self.fail('all-source readiness'),capabilities=lambda:self.fail('VLA preflight'))
        port=ControlledBodyAdapter(peer,peer,self.authority)
        self.assertTrue(port.gate_open)
        self.assertTrue(port.capabilities()['readiness_checked_at_dispatch'])
        self.assertFalse(self.wire.calls)

    def test_supplied_evidence_not_retimed_or_rebound(self):
        r=vla_request();p=r['body']['worksite_evidence'];before=deepcopy(p)
        result=worksite(p,r['body'],'test-robot',self.wire.controller,'session')
        self.assertEqual(result['observed_at'],p['observed_at']);self.assertEqual(p,before)
        with self.assertRaises(ControlError):worksite(dict(p,control_session_id='other'),r['body'],'test-robot',self.wire.controller,'session')

    def test_navigation_business_evidence_is_checked_and_forwarded_once(self):
        r=vla_request();r['body'].pop('worksite_evidence')
        r['body']['navigation_proof']=dict(dream_command_id='arrived',target_id='fridge',state='succeeded')
        raw=dict(contract_version='fq/reception-lan/v1',task_id='test-task',command_id='arrived',
                 state='succeeded',target_id='fridge',completed_at=stamp(),
                 result=dict(success=True,reached=True,navigation_stopped=True))
        original=self.wire.call
        def call(s,m,p,b=None):
            if s=='nav' and m=='GET':
                self.wire.calls.append((s,m,p,b))
                if '/commands/' in p:return deepcopy(raw)
                return dict(robot_serial='test-robot',controller_instance_id=self.wire.controller,observed_at=stamp(),
                    proof_context=dict(valid=True,pose_within_tolerance=True,dream_command_id='arrived',
                                       target_id='fridge',map_id='map',localization_epoch='loc',navigation_epoch='nav'))
            return original(s,m,p,b)
        self.wire.call=call
        self.prepare_vla(r)
        body=self.authority.business_body(r)
        self.assertNotIn('navigation_proof',body)
        self.assertEqual(body['worksite_evidence']['kind'],'navigation_arrival')
        self.assertEqual(body['worksite_evidence']['navigation_command_id'],'arrived')
        self.assertEqual(len([c for c in self.wire.calls if c[0]=='nav']),2)
        self.assertIn('observation',self.row()['worksite_observation'])

    def test_wrong_or_restarted_vla_receipts_never_allow_execute(self):
        original=self.wire.call
        def call(s,m,p,b=None):
            value=original(s,m,p,b)
            if s=='vla' and m=='GET':value['query_vla_instance_id']='restarted'
            return value
        self.wire.call=call
        with self.assertRaises(ControlError):self.prepare_vla()
        self.assertFalse(self.posts('allow_execute'))

    def test_o6_wrong_permission_and_roundtrip_age_rejected(self):
        self.prepare_vla();row=self.row()
        for mutate in (lambda s:s['o6']['permission'].update(ownership_epoch=999,control_authorization={}),
                       lambda s:s['o6'].update(receiver_instance_id='other'),
                       lambda s:s.update(_request_elapsed_ms=101)):
            s=self.wire.status('check');mutate(s)
            with self.subTest(mutate=mutate),self.assertRaises(ControlError):
                self.driver.verify_current_permit(row,s,'execute',require_hold=True)

    def test_nav_vla_nav_uses_original_exit_and_no_cross_source_status(self):
        r,_=self.prepare();row=self.row()
        raw=self.authority.business_body(r)
        raw.update(state='succeeded',result=dict(navigation_stopped=True),current_hold=self.wire.hold(raw))
        self.authority.settle_navigation('test-task','nav-1',raw)
        self.prepare_vla();self.assertTrue(self.authority.released('test-task','nav-1'))
        self.assertNotEqual(row['control_session_id'],self.row()['control_session_id'])
        self.authority.release_control('test-task','open-1')
        self.prepare('nav-2')
        self.assertEqual(self.wire.permit['source_id'],'navigation')
        self.assertEqual(len(self.posts('grant')),3)

    def test_restart_never_restores_or_replays_vla_permission(self):
        from brain.kernel.body_control import BodyControl
        self.prepare_vla();before=len([c for c in self.wire.calls if c[1]=='POST'])
        self.authority.close()
        restarted=BodyControl(self.config,self.root,self.driver,self.pool);self.addCleanup(restarted.close)
        restarted.start()
        self.assertTrue(restarted.recovery_status()['blocked'])
        self.assertEqual(len([c for c in self.wire.calls if c[1]=='POST']),before)

    def test_vla_endpoint_change_cannot_retarget_unreleased_session(self):
        from brain.kernel.body_control import BodyControl
        self.prepare_vla();self.authority.close()
        config=deepcopy(self.config);config['reception_real']['vla_base_url']='http://other.invalid'
        restarted=BodyControl(config,self.root,self.driver,self.pool);self.addCleanup(restarted.close)
        with self.assertRaises(ControlError):restarted.start()

    def test_uninstalled_hands_need_explicit_current_gateway_exit_proof(self):
        self.prepare_vla();row=self.row()
        self.authority.release_control('test-task','open-1')
        s=self.wire.status('before-install')
        s['o6'].update(permission=None,control_session_id=None,input_session_id=None)
        with self.assertRaises(ControlError):self.driver.verify_exit(row,{},s)
        s['features'].append('vla-prepare-installs-o6-v1')
        s['exit_status'].update(o6_never_bound=True,o6_instance_id=row['gateway_binding']['o6_instance_id'])
        self.assertTrue(self.driver.verify_exit(row,{},s)['exit_confirmed'])
        for change in (lambda v:v['exit_status'].update(exit_confirmed=False),
                       lambda v:v['exit_status'].update(o6_instance_id='wrong'),
                       lambda v:v['o6'].update(receiver_output_fenced=False),
                       lambda v:v['o6'].update(permission=False),
                       lambda v:v['o6']['measured']['left'].update(fresh=False),
                       lambda v:v['o6'].update(permission={'control_authorization':row['gateway_binding']['control_authorization']}),
                       lambda v:v['features'].remove('vla-prepare-installs-o6-v1')):
            bad=deepcopy(s);change(bad)
            with self.subTest(change=change),self.assertRaises(ControlError):self.driver.verify_exit(row,{},bad)

    def test_vla_fence_keeps_original_hand_identity(self):
        self.prepare_vla();row=self.row()
        self.authority.release_control('test-task','open-1')
        wire=self.posts('fence')[0][3]
        for key in ('o6_instance_id','hold_reference_id'):
            self.assertEqual(wire[key],row['gateway_binding'][key])
