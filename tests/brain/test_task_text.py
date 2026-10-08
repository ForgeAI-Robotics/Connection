import copy
import unittest
from unittest.mock import patch

from shared.task_text import format_process_event, reason_text
from ops.brain_view import current_alert


class TaskTextTests(unittest.TestCase):
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
