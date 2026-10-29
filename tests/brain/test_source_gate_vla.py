"""direct-v1 VLA + existing Runtime; no physical devices or remote services."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

from brain.adapters.source_gate import SourceGateBodyAdapter, SourceGateNavigationAdapter
from brain.adapters.simulated_navigation import DirectSimulatedNavigationAdapter
from brain.adapters.simulated_manipulation import HybridReceptionAdapter
from brain.adapters.execution import interpret_observation
from brain.adapters.ports import build_port
from brain.adapters.http_client import HttpContractError
from brain.app import create_service
from brain.packages.reception_skill import BASELINE_PATH
from contracts.vla_direct import FIELDS
from contracts.tasks import Rejected
from tests.brain import test_source_gate as source_gate_fixture
from tests.brain.test_native_vla import FakeVla, evidence, now
from tests.brain.test_body_control import nav_request


class DirectVla(FakeVla):
    def __init__(self):
        super().__init__()
        self.current_queries = 0

    def capabilities(self):
        raise AssertionError('No remote VLA readiness checks from brain')

    def submit_task(self, body):
        assert set(body) == set(FIELDS), body
        super().submit_task(body)
        self.commands[body['command_id']]['execution_mode'] = 'direct'
        return dict(accepted=True, command_id=body['command_id'], task_id=body['task_id'])

    def finish(self, task_id, command_id, observed, authorization):
        super().finish(task_id, command_id, observed, authorization)
        row = self.commands[command_id]
        self._stop(row)
        if row['operation'] == 'grasp_cola':
            row['result']['holding_evidence'] = dict(deepcopy(observed), task_id=task_id, command_id=command_id,
                receiver_instance_id='o6-1', valid=True)
        return self.task(command_id)

    @staticmethod
    def _stop(row):
        row['result'].pop('navigation_port_ready', None)
        row['result'].update(execution_mode='direct', body_stopped=True, hands_stopped=True, hold_verified=True)
        row['exit_state'] = dict(policy_stopped=True, publisher_stopped=True, output_fenced=True,
                                body_stopped=True, hands_stopped=True, hold_verified=True, action_started=True)

    def cancel(self, task_id, command_id):
        super().cancel(task_id, command_id)
        self._stop(self.commands[command_id])
        return self.task(command_id)

    def control_status(self):
        self.current_queries += 1
        last = next(reversed(self.commands.values()), {})
        return dict(execution_mode='direct', current_holding=self.holding,
                    holding_evidence=deepcopy((last.get('result') or {}).get('holding_evidence')))


def vla_request(command='open-1', operation='open_fridge'):
    r = nav_request(command)
    r['skill'] = operation
    r['body'] = dict(contract_version='fq/reception-lan/v1', task_id='test-task', command_id=command,
                     operation=operation, object_id='fridge_1' if operation=='open_fridge' else 'cola_can_1',
                     target_area='fridge', navigation_proof={'dream_command_id':'nav-1','target_id':'fridge','state':'succeeded'})
    return r


class DirectVlaTests(unittest.TestCase):
    # Reuse only the offline fixture, not the inherited NAV test methods.
    setUp = source_gate_fixture.SourceGateTests.setUp
    tearDown = source_gate_fixture.SourceGateTests.tearDown
    schedule = source_gate_fixture.SourceGateTests.schedule

    def setup_vla(self):
        self.vla = DirectVla()
        self.port = SourceGateBodyAdapter(self.nav, self.vla, self.control)

    def start(self, r):
        self.schedule(r, 'reception_real')
        self.assertTrue(self.port.submit(r['body']['command_id'], r)['accepted'])

    def finish(self, r):
        cid = r['body']['command_id']
        self.port.effect_control('pause', cid, r)
        return self.port.effect_control('finish', cid, r, evidence=evidence(r['skill']), authorization='Bearer test-only')

    def test_business_only_submission_selects_once_and_does_not_check_dependencies(self):
        self.setup_vla()
        for r in (vla_request(), vla_request('grasp-1', 'grasp_cola')):
            self.start(r)
            self.assertTrue(self.finish(r)['evidence']['supports'])
        self.assertEqual([x['source'] for x in self.gate.puts], ['vla'])
        self.assertEqual(self.vla.current_queries, 0)
        self.assertEqual(len(self.vla.posts), 2)
        self.assertEqual(set(self.vla.posts[0]), set(FIELDS))

    def test_direct_stop_fields_reject_missing_false_conflicting_and_old_receipts(self):
        self.setup_vla(); r=vla_request(); self.start(r); self.finish(r)
        raw=self.vla.task('open-1')
        self.assertTrue(self.port._view('open-1',raw,r)['stopped'])
        for field in ('body_stopped','hands_stopped','hold_verified','policy_stopped'):
            changed=deepcopy(raw);changed['result'][field]=False
            self.assertFalse(self.port._view('open-1',changed,r)['stopped'],field)
        changed=deepcopy(raw);changed['exit_state']['publisher_stopped']=None
        self.assertFalse(self.port._view('open-1',changed,r)['stopped'])
        changed=deepcopy(raw);changed.pop('execution_mode')
        self.assertFalse(self.port._view('open-1',changed,r)['evidence']['identity_ok'])
        # The general/legacy parser is not silently relaxed for direct results.
        self.assertFalse(interpret_observation('open-1',raw,r)['stopped'])

    def test_preparation_failure_clears_only_command_slot_not_physical_stop(self):
        self.setup_vla(); r=vla_request(); self.start(r)
        row=self.vla.commands['open-1']
        row.update(state='failed',completed_at=now(),result=dict(success=False,execution_mode='direct',
            object_id='fridge_1',action_started=False,policy_stopped=True,body_stopped=None,hold_verified=None),
            exit_state=dict(action_started=False,policy_stopped=True,publisher_stopped=True,output_fenced=True))
        viewed=self.port.query('open-1',r)
        self.assertFalse(viewed['started']);self.assertTrue(viewed['resources_released'])
        self.assertIsNone(viewed['raw']['result']['body_stopped'])
        self.assertFalse(viewed['evidence']['supports'])

    def test_cancel_calls_vla_and_records_its_stop_separately_from_gate(self):
        self.setup_vla();r=vla_request();self.start(r)
        record=self.store.load_state();record.update(state='cancelled',dispatch_closed=True);self.store.save_state(record)
        self.vla.http=type('HTTP',(),{})()
        with patch('brain.kernel.source_gate.VlaClient',return_value=self.vla):
            self.control.cancel_task('test-task');self.control._futures['test-task'].result(2)
        self.assertEqual(self.vla.cancels,['open-1']);self.assertEqual(self.nav.cancels,[])
        state=self.control._read()['tasks']['test-task']
        self.assertTrue(state['vla_stopped']);self.assertTrue(state['gate_closed'])
        self.assertEqual([x['source'] for x in self.gate.puts],['vla','none'])

    def test_cancelled_task_cannot_send_late_effect_confirmation(self):
        self.setup_vla();r=vla_request();self.start(r)
        record=self.store.load_state();record.update(state='cancelled',dispatch_closed=True);self.store.save_state(record)
        with self.assertRaises(Exception):self.port.effect_control('pause','open-1',r)
        self.assertEqual(self.vla.pause_calls,0)

    def test_vla_post_timeout_only_queries_original(self):
        self.setup_vla();r=vla_request();self.schedule(r,'reception_real')
        original=self.vla.submit_task
        def lost(body):original(body);raise HttpContractError('lost accepted response')
        self.vla.submit_task=lost
        self.assertTrue(self.port.submit('open-1',r)['unclear'])
        self.port.query('open-1',r);self.port.submit('open-1',r)
        self.assertEqual(len(self.vla.posts),1)
        self.assertEqual(len(self.gate.puts),1)

    def test_all_three_ports_have_their_intended_execution_sides(self):
        first=build_port(self.config,'reception_real',body_control=self.control).port
        second=build_port(self.config,'reception_real_sim_manipulation',body_control=self.control).port
        third=build_port(self.config,'reception_sim_navigation_real',body_control=self.control).port
        self.assertIs(type(first),SourceGateBodyAdapter)
        self.assertIsInstance(second,HybridReceptionAdapter)
        self.assertIs(type(second.navigation),SourceGateNavigationAdapter)
        self.assertIsInstance(third,DirectSimulatedNavigationAdapter)
        self.assertIsNone(third.dream)
        self.assertEqual(self.gate.puts,[])

    def run_flow(self, *, simulated_nav=False):
        self.setup_vla()
        if simulated_nav:self.port=DirectSimulatedNavigationAdapter(self.vla,self.control)
        self.config.update(brain={'reception_backend':'real','capture_timeline':False,
                           'reasoning':{'recovery_advice':False}},reflection={'enabled':False})
        self.config['reception_real']['skill_file']=str(BASELINE_PATH.with_name('reception_single_can_v2.yaml'))
        service=create_service(self.config,model=lambda _:self.fail('No model'),port_factory=lambda _:self.port)
        service.body_control=self.control
        env={k:str(self.root/name) for k,name in [('FQ_EXECUTION_STATE','execution.json'),
             ('FQ_EXECUTION_LOCK','execution.lock'),('FQ_EXECUTION_BLOCK','execution.blocked')]}
        with patch.dict(os.environ,env):
            service.publish('开始接待（导航模拟＋操控真机）' if simulated_nav else '开始接待（导航真机＋操控真机）',
                            'test-task',options={'reflect':False,'worksite_confirmed':True})
            rt=service.runtime
            for operation in ('open_fridge','grasp_cola'):
                self.assertEqual(rt.state,'waiting_human',rt.public_status())
                cid=rt.record['open_command_id']
                service.vla_effect('test-task',cid,'pause')
                service.vla_effect('test-task',cid,'finish',evidence=evidence(operation),authorization='Bearer test-only')
                self.assertEqual(rt.state,'waiting_human')  # No auto-next on finish.
                rt.continue_current();rt.drive()
            self.assertEqual(rt.state,'succeeded',rt.public_status())
        return service

    def test_full_real_workflow_nav_open_grasp_nav_and_no_repeated_vla_switch(self):
        self.run_flow()
        self.assertEqual(len(self.nav.posts),5);self.assertEqual(len(self.vla.posts),2)
        self.assertEqual([x['source'] for x in self.gate.puts],['vla','navigation'])

    def test_manipulation_only_button_never_contacts_real_nav(self):
        self.run_flow(simulated_nav=True)
        self.assertEqual(self.nav.posts,[]);self.assertEqual(len(self.vla.posts),2)
        self.assertEqual([x['source'] for x in self.gate.puts],['vla'])

    def test_holding_not_inferred_from_old_grasp_success(self):
        self.setup_vla();r=vla_request('grasp-1','grasp_cola');self.start(r);self.finish(r)
        nav=nav_request('carry');nav['required_holding']='cola_can_1';self.schedule(nav,'reception_real')
        self.vla.commands['grasp-1']['result']['holding_evidence']['valid']=False
        self.assertFalse(self.port.submit('carry',nav)['accepted']);self.assertEqual(self.nav.posts,[])

    def test_explicit_service_disabled_is_rejected_but_generic_503_stays_unknown(self):
        self.setup_vla()
        for index, code in enumerate(('REAL_ACTIONS_DISABLED', 'SERVICE_UNAVAILABLE')):
            r=vla_request('disabled-'+str(index));self.schedule(r,'reception_real')
            def reject(body, code=code):
                raise HttpContractError(code,status_code=503,payload={'success':False,'error':{
                    'code':code,'message':code,'details':{'action_started':False,'action_state_uncertain':False}}})
            self.vla.submit_task=reject
            value=self.port.submit(r['body']['command_id'],r)
            self.assertEqual(value['unclear'],code!='REAL_ACTIONS_DISABLED')

    def test_gate_closed_never_certifies_vla_body_stop(self):
        self.setup_vla();r=vla_request();self.start(r)
        original=self.vla.cancel
        def unconfirmed(task,cid):
            original(task,cid)
            self.vla.commands[cid]['result']['body_stopped']=None
            self.vla.commands[cid]['exit_state']['body_stopped']=None
            return self.vla.task(cid)
        self.vla.cancel=unconfirmed;self.vla.http=type('HTTP',(),{})()
        record=self.store.load_state();record.update(state='cancelled',dispatch_closed=True);self.store.save_state(record)
        with patch('brain.kernel.source_gate.VlaClient',return_value=self.vla):
            self.control.cancel_task('test-task');self.control._futures['test-task'].result(2)
        state=self.control._read()['tasks']['test-task']
        self.assertTrue(state['gate_closed']);self.assertFalse(state['vla_stopped']);self.assertFalse(state['completed'])
        self.assertEqual(self.vla.cancels,['open-1'])


if __name__ == '__main__':
    unittest.main()
