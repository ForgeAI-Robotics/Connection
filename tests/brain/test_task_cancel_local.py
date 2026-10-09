"""Task abandonment must never require or send a remote stop."""
import tempfile
import threading
import unittest
from unittest.mock import Mock

from brain.app import create_runtime
from brain.adapters.dream_client import DreamClient
from brain.adapters.vla_client import VlaClient
from brain.adapters.http_client import HttpContractError
from tests.brain.test_kernel_reception import _runtime, FakeBody


class LocalCancelTests(unittest.TestCase):
    def test_restart_honors_previous_cancel_intent_without_remote_calls(self):
        with tempfile.TemporaryDirectory() as root:
            rt = _runtime(root)
            with rt._exclusive():
                rt._reload_locked()
                rt.record.update(state='recovery_required', control_request='cancel',
                                 command_unknown=True, open_command_id='unknown-command',
                                 stopped_confirmed=False, resources_cleared=False)
                rt._save('test_legacy_cancel')
            port = FakeBody()
            port.cancel = Mock(side_effect=AssertionError('no remote cancellation'))
            port.query = Mock(side_effect=AssertionError('no remote query'))
            restored = create_runtime({'reception_real': {'kernel_runtime_dir': root}}, port)
            self.assertEqual(restored.state, 'cancelled')
            self.assertFalse(restored.public_status()['blocks_new_motion'])
            self.assertEqual(restored.record['task_cancellation']['abandoned_command_id'], 'unknown-command')
            self.assertFalse(restored.record['stopped_confirmed'])
            port.cancel.assert_not_called()
            port.query.assert_not_called()

    def test_abandoning_poll_waits_wakes_without_another_query_or_cancel(self):
        for cls, method, query in [(DreamClient, 'wait_command', 'command'), (VlaClient, 'wait_task', 'task')]:
            with self.subTest(client=cls.__name__):
                client = cls('http://127.0.0.1:1')
                entered = threading.Event()
                def observed(*args):
                    entered.set()
                    return {'state': 'navigating', 'command_id': 'old'}
                query_mock = Mock(side_effect=observed)
                setattr(client, query, query_mock)
                errors = []
                def wait():
                    try:
                        getattr(client, method)('old', timeout_sec=20, poll_interval_sec=10)
                    except HttpContractError as exc:
                        errors.append(str(exc))
                thread = threading.Thread(target=wait)
                thread.start()
                try:
                    self.assertTrue(entered.wait(1))
                    client.abandon_waits()
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(query_mock.call_count, 1)
                    self.assertEqual(len(errors), 1)
                    self.assertIn('本轮大脑任务已取消', errors[0])
                finally:
                    client.abandon_waits()
                    thread.join(1)
