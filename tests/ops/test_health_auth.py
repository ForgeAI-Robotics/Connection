"""Authenticated health probes must not forward secrets through redirects."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import unittest
from unittest.mock import patch

from ops.services import _http_get, _health_remote


class HealthAuthTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                outer.requests.append((self.path, self.headers.get('Authorization')))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/sink')
                    self.end_headers()
                    return
                self.send_response(200 if self.headers.get('Authorization') == 'Bearer offline-nav-test' else 401)
                self.end_headers()
                self.wfile.write(b'{"status":"ok","interface_online":true}')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        env = patch.dict(os.environ, {'TEST_NAV_TOKEN': 'offline-nav-test', 'TEST_NAV_TOKEN_FILE': ''})
        env.start(); self.addCleanup(env.stop)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def test_health_uses_bearer_and_does_not_expose_it_in_result(self):
        self.assertFalse(_http_get(self.url + '/health')[0])
        result = _http_get(self.url + '/health', token_env='TEST_NAV_TOKEN')
        self.assertTrue(result[0], result)
        self.assertEqual(self.requests[-1], ('/health', 'Bearer offline-nav-test'))
        self.assertNotIn('offline-nav-test', str(result))

    def test_authenticated_probe_never_follows_redirect(self):
        result = _http_get(self.url + '/redirect', token_env='TEST_NAV_TOKEN')
        self.assertFalse(result[0])
        self.assertIn('302', result[1])
        self.assertEqual([path for path, _ in self.requests], ['/redirect'])

    def test_invalid_file_does_not_fall_back_to_environment_or_send_request(self):
        with patch.dict(os.environ, {'TEST_NAV_TOKEN_FILE': '/no-such-private-file'}):
            result = _http_get(self.url + '/health', token_env='TEST_NAV_TOKEN')
        self.assertFalse(result[0])
        self.assertEqual(self.requests, [])
        self.assertNotIn('offline-nav-test', str(result))

    def test_nav_catalog_uses_configured_credential_source_and_logs_no_token(self):
        cfg = {'reception_real': {'body_control': {'nav_token_env': 'TEST_NAV_TOKEN'}}}
        with patch('shared.config.load_config', return_value=cfg), patch('ops.services._record_probe') as record:
            result = _health_remote('dream', self.url)(None)
        self.assertTrue(result['ok'])
        self.assertNotIn('offline-nav-test', str(record.call_args))

    def test_default_credential_mapping_matches_brain_control_driver(self):
        with patch('shared.config.load_config', return_value={}), \
             patch('ops.services._http_get', return_value=(True, 'HTTP 200', 1, '{}')) as get, \
             patch('ops.services._record_probe'):
            _health_remote('dream', self.url)(None)
        get.assert_called_once_with(self.url + '/health', token_env='BRAIN_CONTROL_TOKEN')


if __name__ == '__main__':
    unittest.main()
