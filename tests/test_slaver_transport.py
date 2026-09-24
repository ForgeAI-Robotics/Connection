import json
import queue
import unittest
from unittest.mock import Mock, patch
from connection.brain.adapters.slaver import SlaverTransport


class SlaverTransportTests(unittest.TestCase):
    def test_subscription_precedes_send_and_only_matching_complete_receipt_releases_busy(self):
        messages = queue.Queue()
        pubsub = Mock()
        pubsub.subscribe.side_effect = lambda channel: messages.put({'type': 'subscribe', 'channel': channel})
        def receive(**kwargs):
            try: return messages.get(timeout=.01)
            except queue.Empty: return None
        pubsub.get_message.side_effect = receive
        collaborator = Mock()
        collaborator._get_conn.return_value.pubsub.return_value = pubsub
        collaborator.read_all_agents_name.return_value = ['FQrobot']
        with patch('agent.collaboration.Collaborator.from_config', return_value=collaborator) as factory:
            transport = SlaverTransport({'collaborator': {'clear': True}})
            self.assertIs(factory.call_args.args[0]['clear'], False)
        try:
            slot = transport._begin_inflight('FQrobot', 'command-original')
            pubsub.subscribe.assert_called_once_with('FQrobot_to_FQPlanner')
            for message in [dict(task_id='stale', subtask_result='ok'), dict(task_id='command-original', subtask_result='')]:
                transport._handle_result(json.dumps({'robot_name': 'FQrobot', 'subtask_handle': True, 'status': 'success', **message}))
            self.assertFalse(slot['got_result'])
            collaborator.update_agent_busy.assert_not_called()
            transport._handle_result(json.dumps({'robot_name': 'FQrobot', 'task_id': 'command-original',
                'subtask_handle': True, 'subtask_result': 'observed', 'status': 'success'}))
            self.assertTrue(slot['got_result'])
            collaborator.update_agent_busy.assert_called_once_with('FQrobot', False)
        finally:
            transport.close()
        self.assertFalse(transport._thread.is_alive())
        pubsub.close.assert_called_once()
