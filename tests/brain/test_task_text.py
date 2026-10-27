import copy
import unittest
from unittest.mock import patch

from shared.task_text import format_process_event, reason_text
from ops.brain_view import current_alert


class TaskTextTests(unittest.TestCase):
    def test_new_error_names_do_not_gate_structured_evidence(self):
        from shared.task_text import failure_text
        metrics = {'goal_distance': .12, 'final_yaw_error': -.5,
                   'turn_clearance': {'allowed': False, 'nearest_m': .18, 'required_m': .21}}
        for message, code in [('never_seen_before', 'NAV_NEW_CODE'), ('新设备返回的新原因', None),
                              ('arbitrary cause', 'VLA_NEW_CODE'), (None, 'OTHER_NEW_CODE')]:
            for details in (metrics, {'terminal': metrics}):
                with self.subTest(message=message, code=code, details=details):
                    original = copy.deepcopy(details)
                    text = failure_text(message, code, details=details)
                    for value in ['距目标 12.0 cm', '最终朝向误差 28.6°', '最近障碍 18.0 cm', '禁止转向']:
                        self.assertIn(value, text)
                    self.assertIn(message or code, text)
                    self.assertEqual(details, original)

    def test_terminal_failures_show_snapshot_metrics_and_embedded_clearance(self):
        from shared.task_text import failure_text
        reason = ('terminal correction has a live nearby obstacle; holding stand; '
                  'nearest_m=0.201, angle_deg=79.0, near_returns=1, required_m=0.210')
        details = {'terminal': {'goal_distance': .1551687, 'final_yaw_error': .6915553,
                               'turn_clearance': {'allowed': False, 'required_m': .21, 'reason': reason}}}
        text = failure_text('arrival_clearance_wait_timeout', details=details)
        for value in ['末端净空等待超时', '距目标 15.5 cm', '最终朝向误差 39.6°',
                      '禁止转向', '最近障碍 20.1 cm', '要求净空 21.0 cm', reason]:
            self.assertIn(value, text)
        details['terminal'].update(goal_distance=.953909, final_yaw_error=1.2907092,
                                   turn_clearance={'allowed': True, 'nearest_m': .337726, 'required_m': .21})
        text = failure_text('lateral_stop_not_settled', details=details)
        for value in ['横移停止未稳定', '距目标 95.4 cm', '最终朝向误差 74.0°',
                      '采样快照', '允许转向', '最近障碍 33.8 cm']:
            self.assertIn(value, text)
        self.assertNotIn('已到达', text)

    def test_invalid_clearance_values_are_not_invented_or_replaced(self):
        from shared.task_text import failure_text
        for clearance in [None, [], {}, {'nearest_m': False, 'required_m': float('nan')},
                          {'nearest_m': -1, 'required_m': float('inf')},
                          {'nearest_m': None, 'reason': 'nearest_m=0.201, required_m=invalid'}]:
            with self.subTest(clearance=clearance):
                text = failure_text('arrival_clearance_wait_timeout',
                                    details={'terminal': {'turn_clearance': clearance}})
                self.assertIn('末端净空等待超时', text)
                self.assertNotIn('最近障碍', text)
                self.assertNotIn('要求净空', text)
                self.assertNotIn('禁止转向', text)
        self.assertIn('横移停止未稳定', failure_text('lateral_stop_not_settled'))

    def test_missing_or_invalid_yaw_metrics_do_not_invent_zero_or_crash(self):
        from shared.task_text import failure_text
        for terminal in [None, 'invalid', {}, {'goal_distance': -1, 'final_yaw_error': float('nan')},
                         {'goal_distance': True, 'final_yaw_error': '0.2'},
                         {'goal_distance': float('inf'), 'final_yaw_error': 1e308}]:
            with self.subTest(terminal=terminal):
                text = failure_text('arrival_yaw_timeout', details={'terminal': terminal})
                self.assertIn('最终朝向调整超时', text)
                self.assertNotIn('距目标', text)
                self.assertNotIn('最终朝向误差', text)
        text = failure_text('arrival_yaw_timeout', details={'terminal': {'goal_distance': 0}})
        self.assertIn('距目标 0.0 cm', text)
        self.assertNotIn('最终朝向误差', text)

    def test_saved_downstream_error_is_not_an_advice_generation_failure(self):
        record = {'kind': 'TASK', 'event': 'recovery_advised', 'state': 'recovery_required',
                  'recovery_advice': {'action': 'query_original'},
                  'diagnostic': {'error': 'HTTP 409 INVALID_LEG_ORDER'}}
        self.assertEqual(format_process_event(record), '')
        record['recovery_advice']['error'] = 'model unavailable'
        self.assertIn('生成处理建议时出错', format_process_event(record))

    def record(self, **fields):
        return dict(time='2026-09-30T15:58:44.208+08:00', kind='TASK', event='gate',
                    task_id='ca5700b038bd4fe880003fc831c35888', step_id='NAVIGATING_TO_TABLE2',
                    state='waiting_human', blocked_reason='real_execution_disabled', **fields)

    def test_gate_explains_reason_without_claiming_command_was_sent(self):
        record = self.record(environment_revision='long-environment-revision', revision=1278)
        line = format_process_event(record)
        for text in ['导航到 2 号桌', '真机动作许可未开启', '等待人工处理',
                     'real_execution_disabled', 'NAVIGATING_TO_TABLE2', 'ca5700b0']:
            self.assertIn(text, line)
        self.assertNotIn('long-environment-revision', line)
        self.assertNotIn('1278', line)
        self.assertNotIn('已下发', line)

    def test_unknown_events_and_reasons_remain_available(self):
        unknown = format_process_event({'kind': 'TASK', 'event': 'new_event', 'time': '2026-09-30T15:58:44.208+08:00'})
        self.assertIn('new_event', unknown)
        self.assertIn(' - master - INFO - ', unknown)
        self.assertEqual(reason_text('new_gate: raw detail'), 'new_gate: raw detail')
        record = self.record(error='Traceback\n' + 'x' * 800 + '\nRuntimeError: full')
        self.assertIn(record['error'], format_process_event(record))

    def test_task_open_records_full_identity_once(self):
        record = self.record(environment_revision='full-environment-revision')
        record.update(event='task_opened', state='running', blocked_reason='', step_id=None,
                      task_desc='开始接待')
        line = format_process_event(record)
        self.assertIn('已接收任务：开始接待', line)
        self.assertIn(record['task_id'], line)
        self.assertNotIn(record['environment_revision'], line)

    def test_event_projection_includes_reason_and_advice_without_mutation(self):
        from brain.observability.events import emit
        record = {'task_id': 't', 'phase': 'NAVIGATING_TO_TABLE2', 'state': 'waiting_human',
                  'blocked_reason': 'real_execution_disabled', 'recovery_advice': {'action': 'request_help'}}
        original = copy.deepcopy(record)
        with patch('brain.observability.events.journal_emit') as journal:
            emit('recovery_advised', record)
        self.assertEqual(journal.call_args.kwargs['blocked_reason'], 'real_execution_disabled')
        self.assertEqual(journal.call_args.kwargs['advice_action'], 'request_help')
        self.assertEqual(record, original)
        line = format_process_event(dict(kind='TASK', **journal.call_args.kwargs))
        self.assertEqual(line, '')  # Advice is archived, not another visible report.
        self.assertEqual(journal.call_args.kwargs['recovery_advice'], record['recovery_advice'])

    def test_skip_and_pass_lines_name_the_step_they_are_about(self):
        import tempfile
        from brain.observability.events import emit
        from tests.brain.test_kernel_reception import FakeBody, _runtime
        port = FakeBody()
        port.outcomes['VLA_PICKING'] = dict(terminal='failed', stopped=True, resources_released=True)
        port.handoff_confirmed['to_nav'] = False  # bridged by the skip
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root, port)
            runtime.drive()
            lines = {}
            def sink(event, record):
                with patch('brain.observability.events.journal_emit') as journal:
                    emit(event, record)
                lines.setdefault(event, format_process_event(dict(kind='TASK', **journal.call_args.kwargs)))
            runtime.event_sink = sink
            runtime.skip_current(step_id='VLA_PICKING')
            runtime.drive()
        skipped = lines['step_manually_skipped'].splitlines()[0]
        self.assertIn('人工跳过，未确认成功：抓取饮料（VLA_PICKING）', skipped)
        self.assertIn('下一步：导航到中转点 2', skipped)
        self.assertIn('cmd=vla-pick-', skipped)
        handoff = lines['handoff_observed'].splitlines()[0]
        self.assertIn('导航到中转点 2', handoff)
        self.assertIn('因人工跳过由大脑补齐', handoff)
        self.assertIn('跨越：抓取饮料', handoff)
        passed = lines['step_passed'].splitlines()[0]
        self.assertIn('核验通过：导航到中转点 2（NAVIGATING_TO_RELAY2）', passed)

    def test_current_alert_translates_but_retains_unknown_command_warning(self):
        task = {'task_id': 't', 'phase': 'NAVIGATING_TO_TABLE2', 'state': 'waiting_human',
                'blocked_reason': 'real_execution_disabled', 'command_unknown': True}
        alert = current_alert({'ready': True, 'state': 'healthy'}, task, {})
        self.assertEqual(alert['level'], 'ERROR')
        self.assertIn('导航到 2 号桌：真机动作许可未开启', alert['message'])
        self.assertNotIn('尚未下发', alert['message'])

    def test_waiting_task_is_one_bilingual_error_without_analysis_dump(self):
        record = self.record(environment_revision='long-version',
            diagnostic={'open_command_id': None, 'command_unknown': False},
            recovery_advice={'action': 'wait', 'reason': 'Long analysis text'})
        line = format_process_event(record)
        for value in (' - master - ERROR - ', '真机动作许可未开启', 'real_execution_disabled',
                      'state=waiting_human', 'NAVIGATING_TO_TABLE2'):
            self.assertIn(value, line)
        for value in ('原始诊断', 'recovery_advice', 'open_command_id', 'Long analysis text'):
            self.assertNotIn(value, line)
        self.assertEqual(len(line.splitlines()), 1)

    def test_inbound_request_has_chinese_and_original_event_and_source(self):
        line = format_process_event({'kind': 'INBOUND', 'event': 'publish',
                                     'source': 'web', 'text': '开始接待', 'task_id': 't'})
        for value in (' - master - INFO - ', '收到任务指令：开始接待', 'publish', 'source=web'):
            self.assertIn(value, line)

    def test_failed_attempt_projects_complete_original_error(self):
        from brain.observability.events import emit
        error = 'ConnectionError: ' + 'downstream details ' * 100 + '\nOriginal traceback end'
        record = {'task_id': 't', 'phase': 'VLA_PICKING', 'state': 'recovery_required',
                  'steps': {'VLA_PICKING': {'attempts': [{'attempt_id': 'a1', 'command_id': 'c1',
                      'evidence_ref': 'a1.json', 'progress': {'error': error}}]}}}
        with patch('brain.observability.events.journal_emit') as journal:
            emit('unconfirmed', record)
        line = format_process_event(dict(kind='TASK', **journal.call_args.kwargs))
        self.assertIn('执行结果尚未确认', line)
        self.assertIn('error=' + error, line)
        self.assertIn('attempt_id=a1', line)
        self.assertNotIn('evidence_ref', line)

    def test_normal_task_stays_compact(self):
        record = self.record()
        record.update(event='plan_ready', state='running', blocked_reason='', step_count=11)
        line = format_process_event(record)
        self.assertIn('共 11 步', line)
        self.assertNotIn('原始诊断', line)
        self.assertEqual(len(line.splitlines()), 1)
        record['plan_steps'] = [
            {'step_id': 'NAVIGATING_TO_TABLE2', 'kind': 'navigate', 'key': 'table2', 'requires_gate': True},
            {'step_id': 'VLA_PICKING', 'kind': 'pick', 'target_area': 'table_2'},
        ]
        record['step_count'] = 2
        detailed = format_process_event(record)
        self.assertIn('1.导航到 2 号桌（NAVIGATING_TO_TABLE2，导航，目标 table2，需真机许可）', detailed)
        self.assertIn('2.抓取饮料（VLA_PICKING，抓取，区域 table_2）', detailed)
        self.assertEqual(len(detailed.splitlines()), 1)
        self.assertNotIn('phase_specs', detailed)
