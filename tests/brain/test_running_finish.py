"""One human confirmation per direct VLA action; no physical services."""
from copy import deepcopy
import os
from pathlib import Path
from unittest.mock import patch
import unittest

from brain.app import create_service
from brain.adapters.source_gate import SourceGateBodyAdapter
from brain.adapters.simulated_navigation import DirectSimulatedNavigationAdapter
from brain.adapters.http_client import HttpContractError
from brain.packages.reception_skill import BASELINE_PATH
from contracts.tasks import Rejected
from contracts.vla_direct import effect_window
from tests.brain.test_source_gate_vla import DirectVla, vla_request
from tests.brain import test_source_gate as source_gate_fixture
from tests.brain.test_native_vla import evidence, now


class RunningVla(DirectVla):
    def __init__(self):
        super().__init__()
        self.finish_posts = 0
        self.pending = {}
        self.on_wait = None
        self.completion_state = 'succeeded'
        self.lose_reply = False

    def submit_task(self, body):
        result = super().submit_task(body)
        self.commands[body['command_id']]['execution_ready_at'] = now()
        return result

    def finish(self, task_id, command_id, observed, authorization):
        if authorization != 'Bearer test-only':
            raise HttpContractError('unauthorized', status_code=403)
        self.finish_posts += 1
        row = self.commands[command_id]
        self.pending[command_id] = (task_id, deepcopy(observed), authorization)
        row.update(finish_requested=True, completion=dict(status='accepted', requested_from=row['state'],
            accepted_at=now(), evidence=deepcopy(observed), evidence_window_start=row['execution_ready_at']))
        if self.lose_reply:
            raise HttpContractError('finish reply timed out')
        return self.task(command_id)

    def wait_task(self, command_id, **kwargs):
        if kwargs.get('stop_states') or command_id not in self.pending:
            return self.task(command_id)
        if self.on_wait:
            callback, self.on_wait = self.on_wait, None
            callback()
        row = self.commands[command_id]
        if row['state'] == 'cancelled':
            return self.task(command_id)
        task_id, observed, authorization = self.pending.pop(command_id)
        self.pause(task_id, command_id)
        super().finish(task_id, command_id, observed, authorization)
        row['completion'].update(status='completed', pause_completed_at=row['verification_ready_at'], completed_at=now())
        if self.completion_state == 'failed':
            row.update(state='failed', error={'code':'OBJECT_RETENTION_UNCONFIRMED','message':'holding lost'})
            row['result']['success'] = False
            row['completion']['status'] = 'failed'
        elif self.completion_state == 'conflict':
            row['completion']['evidence']['reference'] = 'foreign-observation'
        return self.task(command_id)


class RunningFinishTests(unittest.TestCase):
    setUp = source_gate_fixture.SourceGateTests.setUp
    tearDown = source_gate_fixture.SourceGateTests.tearDown

    def service(self, simulated=False, vla=None, *, with_close=False):
        self.vla = vla or RunningVla()
        self.port = (DirectSimulatedNavigationAdapter(self.vla, self.control) if simulated
                     else SourceGateBodyAdapter(self.nav, self.vla, self.control))
        self.config.update(brain={'reception_backend':'real','capture_timeline':False,
                                 'reasoning':{'recovery_advice':False}}, reflection={'enabled':False})
        self.config['reception_real']['skill_file'] = str(BASELINE_PATH.with_name('reception_single_can_v2.yaml'))
        if not with_close:
            # Keep the original finish/recovery cases on a frozen historical v2
            # workflow. test_close_fridge exercises the new v3 end-to-end flow.
            import yaml
            definition = yaml.safe_load(Path(self.config['reception_real']['skill_file']).read_text())
            definition['real_workflow']['sop']['version'] = '2'
            definition['real_workflow']['steps'] = [s for s in definition['real_workflow']['steps']
                                                   if s['action'] != 'close_fridge']
            path = self.root/'historical_reception_v2.yaml'
            path.write_text(yaml.safe_dump(definition, allow_unicode=True))
            self.config['reception_real']['skill_file'] = str(path)
        service = create_service(self.config, model=lambda _:self.fail('No model'), port_factory=lambda _:self.port)
        service.body_control = self.control
        env = {k:str(self.root/name) for k,name in [('FQ_EXECUTION_STATE','execution.json'),
               ('FQ_EXECUTION_LOCK','execution.lock'),('FQ_EXECUTION_BLOCK','execution.blocked')]}
        scope=patch.dict(os.environ, env);scope.start();self.addCleanup(scope.stop)
        service.publish('开始接待（导航模拟＋操控真机）' if simulated else '开始接待（导航真机＋操控真机）',
                        'test-task', options={'reflect':False, 'worksite_confirmed':True})
        return service

    def finish(self, service, operation='open_fridge'):
        cid = service.runtime.record['open_command_id']
        return service.vla_effect('test-task', cid, 'finish', evidence=evidence(operation),
                                  authorization='Bearer test-only', auto_advance=True)

    def test_two_confirmations_advance_full_real_workflow(self):
        service = self.service()
        self.assertEqual(service.runtime.state, 'waiting_human')
        self.assertEqual(self.vla.pause_calls, 0)
        self.finish(service)
        self.assertEqual(service.runtime.state, 'waiting_human')
        self.assertEqual(self.vla.posts[-1]['operation'], 'grasp_cola')
        self.assertEqual(self.vla.pause_calls, 1)
        self.finish(service, 'grasp_cola')
        self.assertEqual(service.runtime.state, 'succeeded', service.runtime.public_status())
        self.assertEqual(self.vla.finish_posts, 2)
        self.assertEqual(self.vla.pause_calls, 2)
        self.assertEqual(len(self.nav.posts), 5)
        self.assertEqual([p['source'] for p in self.gate.puts], ['vla','navigation'])
        self.assertNotIn('Bearer test-only', str(service.runtime.record))

    def test_simulated_navigation_still_has_only_two_real_confirmations(self):
        service = self.service(simulated=True)
        self.finish(service);self.finish(service, 'grasp_cola')
        self.assertEqual(service.runtime.state, 'succeeded')
        self.assertEqual(self.nav.posts, [])
        self.assertEqual([p['source'] for p in self.gate.puts], ['vla'])

    def test_lost_finish_reply_queries_and_advances_without_reposting(self):
        service = self.service()
        self.vla.lose_reply = True
        with self.assertRaises(Rejected):self.finish(service)
        self.assertEqual(self.vla.finish_posts, 1)
        self.assertEqual(self.vla.posts[-1]['operation'], 'grasp_cola')
        self.assertEqual(service.runtime.state, 'waiting_human')

    def test_failure_and_conflicting_confirmation_never_advance(self):
        for state in ('failed','conflict'):
            with self.subTest(state=state):
                # Independent fixture/ledger per scenario.
                case=RunningFinishTests();case.setUp()
                try:
                    service=case.service();case.vla.completion_state=state;case.finish(service)
                    self.assertEqual(service.runtime.state,'recovery_required')
                    self.assertEqual(len(case.vla.posts),1)
                    self.assertEqual(len(case.nav.posts),1)
                finally:
                    case.doCleanups();case.tearDown()

    def test_cancel_during_completion_prevents_next_command(self):
        service=self.service()
        self.vla.on_wait=lambda:service.runtime.request_cancel()
        self.finish(service)
        self.assertEqual(service.runtime.state,'cancelled')
        self.assertEqual(len(self.vla.posts),1)

    def test_restart_only_queries_durable_finish_intent(self):
        service=self.service()
        # Leave the accepted intent pending, as at a process crash.
        service._launch=lambda _:None
        self.finish(service)
        record=service.runtime.record
        self.assertTrue(service.runtime.pending_vla_completion())
        previous=service.runtime
        service.runtime=None  # Reconstruct from persisted state, not the original Python object.
        restored=service.attach()
        self.assertIsNot(restored,previous)
        restored.await_vla_completion();restored.drive()
        self.assertEqual(restored.state,'waiting_human')
        self.assertEqual(self.vla.posts[-1]['operation'],'grasp_cola')
        self.assertEqual(self.vla.finish_posts,1)
        self.assertEqual(record['task_id'],restored.record['task_id'])

    def test_pause_during_completion_does_not_auto_resume(self):
        service=self.service()
        self.vla.on_wait=lambda:service.runtime.request_pause()
        self.finish(service)
        self.assertEqual(service.runtime.state,'paused')
        self.assertEqual(len(self.vla.posts),1)

    def test_completion_query_timeout_does_not_repost_or_advance(self):
        service=self.service()
        def timed_out(*args, **kwargs):raise HttpContractError('original query timed out')
        self.vla.wait_task=timed_out
        self.finish(service)
        self.assertEqual(service.runtime.state,'recovery_required')
        self.assertEqual(self.vla.finish_posts,1)
        self.assertEqual(len(self.vla.posts),1)
        self.assertIn('timed out',service.runtime.record['blocked_reason'])

    def test_effect_http_accepts_only_boolean_auto_advance(self):
        from brain.api.app import create_app
        from unittest.mock import Mock
        application=Mock();application.vla_effect.return_value={'accepted':True}
        client=create_app(application,facade=Mock()).test_client()
        body=dict(task_id='task',command_id='command',evidence=evidence(),auto_advance=True)
        response=client.post('/api/task_vla/finish',json=body,headers={'Authorization':'Bearer test-only'})
        self.assertEqual(response.status_code,200)
        application.vla_effect.assert_called_once_with('task','command','finish',evidence=body['evidence'],
                                                       authorization='Bearer test-only',auto_advance=True)
        self.assertNotIn('test-only',response.get_data(as_text=True))
        self.assertEqual(client.post('/api/task_vla/finish',json=dict(body,auto_advance='true')).status_code,400)

    def test_duplicate_and_conflicting_clicks_preserve_first_evidence(self):
        service=self.service();service._launch=lambda _:None
        cid=service.runtime.record['open_command_id'];observed=evidence()
        for _ in range(2):
            service.vla_effect('test-task',cid,'finish',evidence=observed,
                               authorization='Bearer test-only',auto_advance=True)
        self.assertEqual(self.vla.finish_posts,1)
        with self.assertRaises(Rejected):
            service.vla_effect('test-task',cid,'finish',evidence=dict(observed,reference='other'),
                               authorization='Bearer test-only',auto_advance=True)
        self.assertEqual(service.runtime.public_status()['vla_confirmation']['finish_intent']['evidence'],observed)

    def test_observation_window_requires_matching_original_evidence(self):
        observed=evidence()
        raw=dict(execution_ready_at='2026-10-29T00:00:00+00:00',state='succeeded',
                 completion=dict(requested_from='running',status='completed',evidence=observed,
                                 evidence_window_start='2026-10-29T00:00:00+00:00'))
        self.assertEqual(effect_window(raw,observed),raw['execution_ready_at'])
        with self.assertRaises(ValueError):effect_window(raw,dict(observed,reference='other'))
