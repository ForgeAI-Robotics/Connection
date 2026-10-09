import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from shared.networks import entry_url, apply_to_brain_config, apply_proxy_bypass


class NetworkRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        self.path = self.root / 'config/networks.yaml'
        self.data = {
            'active': 'first',
            'roles': {'brain': {'http_port': 5000, 'ops_port': 5678},
                      'nav': {'http_port': 8001}, 'control': {'http_port': 8091}},
            'wifis': {
                'first': {'brain': '192.0.2.55', 'control': '192.0.2.55',
                          'nav': '192.0.2.5', 'robot': '192.0.2.5'},
                'second': {'brain': '198.51.100.55', 'control': '198.51.100.55',
                           'nav': '198.51.100.5', 'robot': '198.51.100.5'},
            },
        }
        self.write()
        self.env = patch.dict(os.environ, {'CONNECTION_WORKSPACE': str(self.root)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self):
        self.path.write_text(yaml.safe_dump(self.data))

    def test_proxy_bypass_covers_site_ips_and_remote_simulation_host(self):
        apply_proxy_bypass()
        self.assertEqual(os.environ['NO_PROXY'].split(','), ['192.0.2.55', '192.0.2.5'])
        (self.root / 'config/robot_api.yaml').write_text(yaml.safe_dump(
            {'backends': {'simple_o7': {'url': 'http://203.0.113.69:18770'}}}))
        apply_proxy_bypass()
        self.assertEqual(os.environ['no_proxy'].split(','), ['192.0.2.55', '192.0.2.5', '203.0.113.69'])

    def test_wifi_switch_moves_all_entry_defaults_and_downstream_targets(self):
        for name, prefix in [('first', '192.0.2'), ('second', '198.51.100')]:
            self.data['active'] = name
            self.write()
            for env_name in ['MASTER_URL', 'LARK_BRAIN_URL']:
                self.assertEqual(entry_url(env_name), f'http://{prefix}.55:5000')
            self.assertEqual(entry_url('CONNECTION_OPS_URL', 'ops_port'), f'http://{prefix}.55:5678')
            config = apply_to_brain_config({'reception_real': {'dream_base_url': 'http://stale:1'}})
            self.assertEqual(config['reception_real']['dream_base_url'], f'http://{prefix}.5:8001')
            self.assertEqual(config['reception_real']['vla_base_url'], f'http://{prefix}.55:8091')

    def test_blank_env_uses_config_and_standalone_override_is_explicit(self):
        os.environ['MASTER_URL'] = '  '
        self.assertEqual(entry_url('MASTER_URL'), 'http://192.0.2.55:5000')
        os.environ['MASTER_URL'] = ' https://brain.example.test/ '
        self.path.unlink()
        self.assertEqual(entry_url('MASTER_URL'), 'https://brain.example.test')

    def test_bad_config_or_bad_override_never_falls_back_to_localhost(self):
        self.data['active'] = 'missing'
        self.write()
        with self.assertRaisesRegex(ValueError, '未知 WiFi'):
            entry_url('MASTER_URL')
        os.environ['MASTER_URL'] = 'old-host:5000'
        with self.assertRaisesRegex(ValueError, 'MASTER_URL'):
            entry_url('MASTER_URL')

    def test_web_voice_and_feishu_consume_the_address_table(self):
        from entries.web.app import create_app as web
        from entries.voice.app import create_app as voice
        from entries.feishu.config import load_settings
        with patch('entries.web.app.BrainClient') as client:
            web()
            self.assertEqual([call.args[0] for call in client.call_args_list],
                             ['http://192.0.2.55:5000', 'http://192.0.2.55:5678'])
        with patch('entries.voice.app.BrainClient') as client:
            voice()
            self.assertEqual(client.call_args.args[0], 'http://192.0.2.55:5000')
        with patch('entries.feishu.config.load_dotenv'):
            self.assertEqual(load_settings(require_credentials=False).brain_url, 'http://192.0.2.55:5000')
