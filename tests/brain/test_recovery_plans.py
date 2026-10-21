"""Bounded LLM recovery across SOP/generic tasks, with no physical services."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from brain.app import create_service
from brain.application import BrainApplication
from brain.packages.generic import steps_from_subtasks
from brain.recovery import validate_plan, available_actions, fresh_observation
from contracts.planning import Plan
from contracts.tasks import Rejected
from tests.brain.test_kernel_reception import FakeBody


FAILURE = {'terminal': 'failed', 'stopped': True, 'resources_released': True,
           'evidence': {'identity_ok': True, 'time_ok': True}}


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = {'brain': {'capture_timeline': False, 'reasoning': {'confirm_recovery_advice': True}},
                       'reflection': {'enabled': False},
                       'reception_real': {'kernel_runtime_dir': tmp.name, 'dream_inspection_enabled': False}}
        self.body = FakeBody()
        self.reads = 0
        self.body.observe_recovery = self.observe
        self.backend = 'reception_real'
        self.order = None
        self.extra = {}
        self.seen_contexts = []
        for name, fn in [('select_backend', lambda *a, **kw: self.backend),
                         ('target_identity', lambda c, b: {'backend': b})]:
            p = patch('brain.service.' + name, side_effect=fn)
            p.start(); self.addCleanup(p.stop)

    def observe(self):
        self.reads += 1
        return {'source': 'test_scene', 'scope': 'test_state_not_visual', 'world': {'fixture': True}}

    def model(self, messages):
        context = json.loads(messages[0]['content'].split('\n')[-1])
        self.seen_contexts.append(context)
        action = 'request_help'
        if 'revise_plan' in context['allowed_actions']:
            action = 'revise_plan'
        elif 'observe_scene' in context['allowed_actions'] and not context['recovery_planning']['observation_fresh']:
            action = 'observe_scene'
        result = {'action': action, 'summary': '先读取状态，再安排剩余动作', 'reason': '按原命令和新状态处理'}
        if action == 'revise_plan':
            result['plan'] = {'step_order': self.order or [s['step_id'] for s in context['recovery_planning']['remaining_steps']],
                              'observation_id': context['recovery_planning']['observation']['observation_id']}
            result['plan'].update(self.extra)
        return json.dumps(result)

    def start(self, *, generic=False, failure_step=None, app=False):
        self.backend = 'desk' if generic else 'reception_real'
        self.failure_step = failure_step or ('STEP_1' if generic else 'NAVIGATING_TO_TABLE2')
        self.body.outcomes[self.failure_step] = deepcopy(FAILURE)
        self.svc = create_service(self.config, model=self.model, port_factory=lambda _: self.body)
        if generic:
            self.svc.select_call = lambda *a: {'package': 'generic', 'source': 'fixture', 'sop': None}
            steps = steps_from_subtasks([{'subtask': text} for text in [
                '抓取 milk_1', '放置 milk_1 到 milk_area', '抓取 milk_2', '放置 milk_2 到 milk_area']])
            self.svc.plan_call = lambda *a: Plan(tuple(steps))
        if app:
            self.app = BrainApplication(self.svc).start()
            self.addCleanup(self.app.close)
            self.app.publish('开始接待（仅导航）', 'recovery-task')
            self.wait(lambda: self.app.status().get('recovery_advice'))
        else:
            self.svc.publish('整理这两个物体' if generic else '开始接待（仅导航）', 'recovery-task')
        return self.svc.status()['recovery_advice']

    def wait(self, predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(.01)
        self.fail('timeout')

    def confirm(self, advice=None, app=False):
        advice = advice or self.svc.status()['recovery_advice']
        target = self.app if app else self.svc
        return target.confirm_advice('recovery-task', advice['advice_id'], advice['record_revision'])

    def observe_and_plan(self, **kwargs):
        advice = self.start(**kwargs)
        self.assertEqual(advice['action'], 'observe_scene')
        before = list(self.body.submits)
        result = self.confirm(advice)
        self.assertTrue(result['observation_only'])
        self.assertEqual(self.body.submits, before)
        plan = self.svc.status()['recovery_advice']
        self.assertEqual(plan['action'], 'revise_plan')
        return plan

    def test_sop_observe_then_confirm_recovery_preserves_route_and_failures(self):
        plan = self.observe_and_plan()
        original = self.body.submits[0]
        before = deepcopy(self.svc.runtime.record['phase_specs'])
        self.body.outcomes.clear()
        result = self.confirm(plan)
        self.assertTrue(result['plan_revised'])
        status = self.svc.status()
        self.assertEqual(status['state'], 'succeeded')
        self.assertEqual(status['plan_revision'], 1)
        self.assertEqual(self.svc.runtime.record['phase_specs'], before)
        self.assertEqual(len(self.body.submits), 5)
        self.assertEqual(status['failure_history'][0]['command_id'], original)
        self.assertEqual(status['plan_revisions'][0]['original_command_id'], original)
        self.assertEqual(self.reads, 1)
        self.assertTrue(self.confirm(plan)['no_op'])
        self.assertEqual(len(self.body.submits), 5)

    def test_generic_can_reorder_independent_groups_without_losing_original_obligations(self):
        self.order = ['STEP_3', 'STEP_4', 'STEP_1', 'STEP_2']
        plan = self.observe_and_plan(generic=True)
        self.body.outcomes.clear()
        self.confirm(plan)
        record = self.svc.runtime.record
        self.assertEqual(record['state'], 'succeeded')
        self.assertEqual(record['phase_order'], self.order)
        self.assertEqual(len(record['steps']['STEP_1']['attempts']), 2)
        self.assertEqual(record['steps']['STEP_1']['attempts'][0]['verdict'], 'FAIL')
        self.assertEqual(record['steps']['STEP_1']['attempts'][1]['verdict'], 'PASS')
        prefixes = [cmd.split('-recoverytask')[0] for cmd in self.body.submits]
        self.assertEqual(prefixes, ['sim-1', 'sim-3', 'sim-4', 'sim-1', 'sim-2'])
        self.assertEqual(record['task_id'], 'recovery-task')

    def test_completed_prefix_is_preserved(self):
        plan = self.observe_and_plan(generic=True, failure_step='STEP_3')
        first = deepcopy(self.svc.runtime.record['steps']['STEP_1'])
        self.assertEqual(plan['plan']['step_order'], ['STEP_3', 'STEP_4'])
        self.body.outcomes.clear()
        self.confirm(plan)
        self.assertEqual(self.svc.runtime.record['steps']['STEP_1'], first)
        self.assertEqual(len(self.body.submits), 5)

    def test_bad_model_plan_is_not_executable(self):
        self.start(generic=True)
        for order, extra in [(['STEP_1'], {}), (['STEP_1'] * 4, {}),
                             (['STEP_2', 'STEP_1', 'STEP_3', 'STEP_4'], {}),
                             (['STEP_1', 'STEP_3', 'STEP_2', 'STEP_4'], {}),
                             (None, {'goal_xyt': [99,99,0]})]:
            self.order, self.extra = order, extra
            self.svc._advise(self.svc.runtime)  # Last advice is current; force a new evidence read below.
            from brain.reasoning import recovery_context
            context = recovery_context(self.svc.runtime.record)
            # Use a fresh approved read even after a previous invalid-model fallback.
            self.svc.runtime.record_advice(context, {'action':'observe_scene','summary':'read','reason':'new evidence'})
            self.confirm()
            self.assertEqual(self.svc.status()['recovery_advice']['source'], 'rules')
            self.assertEqual(len(self.body.submits), 1)

    def test_sop_cannot_reorder_legs(self):
        self.start()
        ids = [s['step_id'] for s in self.svc.runtime.record['phase_specs'][2:]]
        self.order = list(reversed(ids))
        self.confirm()
        self.assertEqual(self.svc.status()['recovery_advice']['source'], 'rules')
        self.assertEqual(len(self.body.submits), 1)

    def test_unknown_or_safety_failure_only_allows_observation_not_replanning(self):
        self.start()
        rt = self.svc.runtime
        record = deepcopy(rt.record)
        for changes in [{'command_unknown': True}, {'resources_cleared': False}, {'stopped_confirmed': False}]:
            snapshot = dict(record, **changes)
            self.assertNotIn('revise_plan', available_actions(snapshot))
        original = self.body.submits[0]
        self.body.commands[original]['evidence']['downstream'] = {'result': {'failure_scope': 'safety', 'next_command_allowed': False}}
        # A late safety restriction must be caught at confirmation, even if the model saw a normal failure.
        self.confirm()
        plan = self.svc.status()['recovery_advice']
        result = self.confirm(plan)
        self.assertFalse(result['accepted'])
        self.assertEqual(len(self.body.submits), 1)
        self.assertFalse(rt.record.get('plan_revisions'))

    def test_fresh_original_success_or_missing_stop_blocks_replay(self):
        plan = self.observe_and_plan()
        original = self.body.submits[0]
        self.body.commands[original]['terminal'] = 'succeeded'
        result = self.confirm(plan)
        self.assertFalse(result['accepted'])
        self.assertEqual(len(self.body.submits), 1)
        self.assertEqual(self.svc.status()['state'], 'recovery_required')

    def test_observation_expires_and_cannot_authorize_plan(self):
        plan = self.observe_and_plan()
        with patch('brain.recovery.fresh_observation', return_value=False):
            self.assertFalse(self.svc.status()['recovery_advice']['applicable'])
            with self.assertRaises(Rejected): self.confirm(plan)
        record = deepcopy(self.svc.runtime.record)
        record['recovery_observation']['observed_at'] = (datetime.now(timezone.utc)-timedelta(seconds=61)).isoformat()
        self.assertFalse(fresh_observation(record))
        self.assertEqual(len(self.body.submits), 1)

    def test_unknown_result_must_be_resolved_by_original_query_before_revision(self):
        self.start()
        rt = self.svc.runtime
        original = self.body.submits[0]
        with rt._exclusive():
            attempt = rt._latest(self.failure_step)
            attempt.update(verdict='UNKNOWN', finished=False)
            attempt['progress'].update(terminal='', timed_out=True)
            rt.record.update(command_unknown=True, stopped_confirmed=False, resources_cleared=False)
            rt._save('test_unknown_original')
        from brain.reasoning import recovery_context
        rt.record_advice(recovery_context(rt.record), {'action':'observe_scene','summary':'read','reason':'read'})
        self.confirm()
        self.assertNotIn('revise_plan',self.svc.status()['recovery_advice']['allowed_actions'])
        rt.record_advice(recovery_context(rt.record), {'action':'query_original','summary':'query','reason':'query'})
        self.confirm()
        plan=self.svc.status()['recovery_advice']
        self.assertEqual(plan['action'],'revise_plan')
        self.body.outcomes.clear()
        self.confirm(plan)
        self.assertEqual(self.svc.status()['state'],'succeeded')
        old=rt.record['steps'][self.failure_step]['attempts'][0]
        self.assertEqual(old['progress']['terminal'],'')
        self.assertEqual(old['recovery_original_check']['command_id'],original)
        self.assertEqual(old['recovery_original_check']['terminal'],'failed')
        self.assertEqual(len(self.body.submits),5)

    def test_simple_generic_scene_template_does_not_enable_episode_reset(self):
        from brain.recovery import bind_policy
        from brain.packages.registry import PackagePolicy
        policy=bind_policy(PackagePolicy(),'generic','simple_o7',self.body,self.config)
        self.assertTrue(policy['observe'])
        self.assertFalse(policy['revise'])

    def test_budget_and_old_task_policy_are_enforced(self):
        self.start()
        for _ in range(2):
            self.confirm()  # observe
            self.assertEqual(self.svc.status()['recovery_advice']['action'], 'revise_plan')
            self.confirm()  # same action fails again; next attempt needs new observation
        self.confirm()
        self.assertNotIn('revise_plan', self.svc.status()['recovery_advice']['allowed_actions'])
        self.assertEqual(len(self.body.submits), 3)
        record = deepcopy(self.svc.runtime.record); record.pop('recovery_policy')
        self.assertEqual(available_actions(record), [])

    def test_restart_after_claim_does_not_repeat_read_or_revision(self):
        plan = self.observe_and_plan()
        rt = self.svc.runtime
        rt.claim_advice(plan['advice_id'], plan['record_revision'])
        self.svc = create_service(self.config, model=self.model, port_factory=lambda _: self.body)
        result = self.confirm(plan)
        self.assertTrue(result['no_op'])
        self.assertEqual(result['confirmation']['status'], 'processing')
        self.assertEqual(len(self.body.submits), 1)
        self.assertEqual(self.reads, 1)

    def test_cancel_preempts_observation_and_discards_late_result(self):
        advice = self.start(app=True)
        entered, release = threading.Event(), threading.Event()
        def observe():
            entered.set(); release.wait(4)
            return self.observe()
        results = []
        with patch.object(self.body, 'observe_recovery', side_effect=observe):
            thread = threading.Thread(target=lambda: results.append(self.confirm(advice, app=True)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.app.control('cancel', task_id='recovery-task')
            finally:
                release.set(); thread.join(5)
        self.assertEqual(self.app.status()['state'], 'cancelled')
        self.assertFalse(self.svc.runtime.record.get('recovery_observation'))
        self.assertFalse(results[0]['accepted'])
        self.assertEqual(len(self.body.submits), 1)

    def test_two_confirmations_cannot_apply_the_same_revision_twice(self):
        self.start(app=True)
        self.confirm(app=True)
        self.wait(lambda: self.app.status()['recovery_advice']['action'] == 'revise_plan')
        plan = self.app.status()['recovery_advice']
        self.body.outcomes.clear()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.confirm(plan, app=True))) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        self.wait(lambda: self.app.status()['state'] == 'succeeded')
        self.assertEqual(sum(bool(r.get('no_op')) for r in results), 1)
        self.assertEqual(self.app.status()['plan_revision'], 1)
        self.assertEqual(len(self.body.submits), 5)

    def test_cancel_during_revision_query_discards_proposal(self):
        self.start(app=True)
        self.confirm(app=True)
        self.wait(lambda: self.app.status()['recovery_advice']['action'] == 'revise_plan')
        plan = self.app.status()['recovery_advice']
        entered, release = threading.Event(), threading.Event()
        original = self.body.query
        def query(*args):
            entered.set(); release.wait(4)
            return original(*args)
        results = []
        with patch.object(self.body, 'query', side_effect=query):
            thread = threading.Thread(target=lambda: results.append(self.confirm(plan, app=True)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.app.control('cancel', task_id='recovery-task')
            finally:
                release.set(); thread.join(5)
        self.assertFalse(results[0]['accepted'])
        self.assertEqual(self.app.status()['state'], 'cancelled')
        self.assertEqual(self.app.status()['plan_revision'], 0)
        self.assertEqual(len(self.body.submits), 1)

    def test_revision_survives_restart_without_automatic_motion(self):
        plan = self.observe_and_plan()
        # Simulate shutdown after the revision commit, before the drive is scheduled.
        with patch.object(self.svc, '_launch'):
            self.confirm(plan)
        stored = deepcopy(self.svc.runtime.record)
        svc = create_service(self.config, model=self.model, port_factory=lambda _: self.body)
        app = BrainApplication(svc).start()
        try:
            self.assertEqual(app.status()['plan_revision'], 1)
            self.assertEqual(app.status()['task_id'], stored['task_id'])
            self.assertEqual(len(self.body.submits), 1)
            self.assertTrue(app.confirm_advice('recovery-task',plan['advice_id'],plan['record_revision'])['no_op'])
        finally:
            app.close()

    def test_observation_failure_does_not_fake_fresh_evidence(self):
        self.start()
        with patch.object(self.body, 'observe_recovery', side_effect=RuntimeError('state offline')):
            result = self.confirm()
        self.assertFalse(result['accepted'])
        self.assertFalse(self.svc.runtime.record.get('recovery_observation'))
        self.assertEqual(len(self.body.submits), 1)

    def test_step_payloads_cannot_change_even_for_generic_reordering(self):
        self.order = ['STEP_3', 'STEP_4', 'STEP_1', 'STEP_2']
        plan = self.observe_and_plan(generic=True)
        rt = self.svc.runtime
        original = {s['step_id']:deepcopy(s) for s in rt.record['phase_specs']}
        with patch.object(self.svc, '_launch'):
            self.confirm(plan)
        self.assertEqual({s['step_id']:s for s in rt.record['phase_specs']}, original)
        self.assertEqual(rt.record['plan_revisions'][0]['before'], list(original.values()))


class RecoveryObservationAdapterTests(unittest.TestCase):
    def test_simple_explicit_same_episode_retry_does_not_override_safety_or_real_routes(self):
        from brain.recovery import refuses_retry
        raw = {'error': {'retryable': False}, 'result': {'simulation': {'retry_allowed': True}}}
        progress = {'evidence': {'downstream': raw}}
        self.assertFalse(refuses_retry(progress, 'reception_simple'))
        self.assertTrue(refuses_retry(progress, 'reception_real'))
        raw['result']['next_command_allowed'] = False
        self.assertTrue(refuses_retry(progress, 'reception_simple'))
        del raw['result']['next_command_allowed']
        raw['result']['failure_scope'] = 'safety'
        self.assertTrue(refuses_retry(progress, 'reception_simple'))
        del raw['result']['failure_scope']
        raw['result']['simulation']['retry_allowed'] = False
        self.assertTrue(refuses_retry(progress, 'reception_simple'))

    def test_body_reads_only_status_world_and_labels_non_visual_scope(self):
        from unittest.mock import Mock
        from brain.adapters.execution import BodyAdapter
        dream, vla = Mock(), Mock()
        dream.status.return_value = {'interface_online':True,'motion_ready':False,'commands':{'old':'not copied'}}
        dream.world.return_value = {'locations':[]}
        view = BodyAdapter(dream, vla).observe_recovery()
        self.assertEqual(view['scope'], 'navigation_status_and_world_not_visual')
        self.assertNotIn('commands',view['navigation'])
        vla.assert_not_called()
        dream.submit_navigation.assert_not_called()
        dream.submit_inspection.assert_not_called()
        self.assertFalse(view['navigation']['motion_ready'])

    def test_simple_observation_is_a_get_and_validates_capabilities(self):
        from brain.adapters.simple_o7 import SimpleO7Adapter
        port = SimpleO7Adapter('http://fixture',token='')
        with patch.object(port, '_call', return_value={'capabilities':['pick_hold_can']}) as call:
            self.assertEqual(port.observe_recovery()['source'],'simple_scene')
            call.assert_called_once_with('/v1/scene')
        with patch.object(port, '_call', return_value={'capabilities':['invented_skill']}):
            with self.assertRaises(Rejected): port.observe_recovery()


class RecoverySceneExecutionTests(unittest.TestCase):
    def test_model_reorders_using_new_scene_and_real_adapter_verifies_effects(self):
        from brain.adapters.execution import SimAdapter, parse_sim_action
        from brain.packages.generic import steps_from_subtasks
        with tempfile.TemporaryDirectory() as root:
            world = {obj:{'pos':[0,0,.75], 'grasped':False} for obj in ['milk_1','milk_2']}
            zones = {'milk_area':{'pos':[1,1], 'radius':.1}}
            executed, model_contexts = [], []
            def perform(text):
                executed.append(text)
                action = parse_sim_action(text)
                item = world[action[1]]
                if action[0] == 'grasp':
                    if action[1] == 'milk_1' and world['milk_2']['pos'][:2] != [1,1]:
                        return {'success':False, 'result':'milk_2 blocks access to milk_1'}
                    if any(i['grasped'] for i in world.values()):
                        return {'success':False, 'result':'hand occupied'}
                    item['grasped'] = True
                else:
                    if not item['grasped']: return {'success':False,'result':'not holding object'}
                    item.update(grasped=False,pos=[1,1,.75])
                return {'success':True,'result':'done'}
            port = SimAdapter(perform=perform,world=lambda:deepcopy(world),zones=lambda:deepcopy(zones))
            config = {'brain':{'reasoning':{'confirm_recovery_advice':True}},
                      'reception_real':{'kernel_runtime_dir':root},'reflection':{'enabled':False}}
            def model(messages):
                c=json.loads(messages[0]['content'].split('\n')[-1]);model_contexts.append(c)
                if 'revise_plan' in c['allowed_actions']:
                    observed=c['recovery_planning']['observation']
                    self.assertFalse(observed['data']['world']['milk_1']['grasped'])
                    return json.dumps({'action':'revise_plan','summary':'先完成挡住目标的 milk_2，再完成 milk_1',
                                       'reason':'失败回包与新场景共同表明先处理 milk_2，两个物体都仍需送达',
                                       'plan':{'step_order':['STEP_3','STEP_4','STEP_1','STEP_2'],
                                               'observation_id':observed['observation_id']}})
                return json.dumps({'action':'observe_scene','summary':'读取最新场景','reason':'需要确认两物体状态'})
            service = create_service(config,model=model,port_factory=lambda _:port)
            service.select_call=lambda *a:{'package':'generic','source':'fixture'}
            texts=['抓取 milk_1','放置 milk_1 到 milk_area','抓取 milk_2','放置 milk_2 到 milk_area']
            service.plan_call=lambda *a:Plan(tuple(steps_from_subtasks([{'subtask':x} for x in texts])))
            with patch('brain.service.select_backend',return_value='desk'), \
                 patch('brain.service.target_identity',return_value={'backend':'desk'}):
                service.publish('把两个物体都送到目标区','scene-recovery')
                for _ in range(2):
                    a=service.status()['recovery_advice']
                    service.confirm_advice('scene-recovery',a['advice_id'],a['record_revision'])
            self.assertEqual(service.status()['state'],'succeeded')
            self.assertEqual(executed,[texts[0],texts[2],texts[3],texts[0],texts[1]])
            self.assertTrue(all(not i['grasped'] and i['pos'][:2]==[1,1] for i in world.values()))
            self.assertEqual(service.status()['attempt_summary']['failure_count'],1)
            self.assertEqual(service.status()['attempt_summary']['retry_count'],1)
            self.assertEqual(len(model_contexts),2)
