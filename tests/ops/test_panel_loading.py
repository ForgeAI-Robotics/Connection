import unittest
from unittest.mock import patch

from ops.app import create_app
from ops.control import _service_status, service_description
from ops.services import by_id


class PanelLoadingTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        for worker in self.app.extensions['workers']:
            self.addCleanup(worker.shutdown)

    def test_initial_page_has_card_data_without_health_or_process_checks(self):
        with patch('ops.app._service_status', side_effect=AssertionError('must not probe')), \
             patch('ops.tmuxctl.tmux_available', side_effect=AssertionError('must not run tmux')):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('id="initial-services"', text)
        for name in ('master', 'deploy', 'feishu', 'voice'):
            self.assertIn(f'"id": "{name}"', text)
        self.assertIn('"state": "checking"', text)

    def test_brain_layer_does_not_wait_for_or_probe_unrelated_services(self):
        def status(service, *, include_pids):
            self.assertEqual(service.layer, 'brain')
            self.assertFalse(include_pids)
            return service_description(service)
        with patch('ops.app._service_status', side_effect=status) as probe, \
             patch('ops.tmuxctl.tmux_available', return_value=True):
            response = self.client.get('/api/status?layer=brain')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(probe.call_count, 4)
        result = response.get_json()
        self.assertEqual([s['id'] for s in result['brain']], ['master', 'deploy', 'feishu', 'voice'])
        self.assertEqual(result['robot'], [])
        self.assertEqual(self.client.get('/api/status?layer=missing').status_code, 400)

    def test_list_status_skips_process_tree_but_detailed_status_retains_it(self):
        with patch('ops.control._brain_state', return_value='tmux'), \
             patch('ops.control._feishu_health', return_value={'ok': True}), \
             patch('ops.tmuxctl.tmux_pids_for', return_value={123, 456}) as tree:
            light = _service_status(by_id('feishu'), include_pids=False)
            tree.assert_not_called()
            full = _service_status(by_id('feishu'))
        self.assertEqual(light['state'], 'tmux')
        self.assertEqual(light['pids'], [])
        self.assertEqual(full['pids'], [123, 456])
