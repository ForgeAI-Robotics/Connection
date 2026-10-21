"""The real SDK must initialize and route correctly with desktop proxy variables."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from brain.adapters.model import ModelClient
from shared.networks import normalize_proxy_environment


def config(endpoint, azure=False):
    return {'model_select': 'test-model', 'timeout_sec': 3, 'model_dict': {
        'cloud_model': 'test-model', 'cloud_type': 'azure' if azure else 'default',
        'cloud_server': endpoint, 'cloud_api_key': 'fixture',
        'azure_endpoint': endpoint, 'azure_deployment': 'test-deployment',
        'azure_api_version': '2024-02-01', 'azure_api_key': 'fixture'}}


class ModelProxyTests(unittest.TestCase):
    def test_alias_normalization_preserves_credentials_hosts_and_bypass(self):
        values = {'ALL_PROXY': 'socks://user:p%40ss@[::1]:7890/',
                  'all_proxy': 'SOCKS://localhost:7890',
                  'http_proxy': 'http://localhost:7890',
                  'HTTPS_PROXY': 'socks5h://localhost:7890',
                  'NO_PROXY': 'localhost,192.0.2.5'}
        with patch.dict(os.environ, values, clear=True):
            normalize_proxy_environment()
            expected = dict(values, ALL_PROXY='socks5://user:p%40ss@[::1]:7890/',
                            all_proxy='socks5://localhost:7890')
            self.assertEqual(dict(os.environ), expected)
            normalize_proxy_environment()
            self.assertEqual(dict(os.environ), expected)

    def test_both_sdk_clients_initialize_with_socks_alias(self):
        for azure in (False, True):
            with self.subTest(azure=azure), patch.dict(os.environ, {
                    'ALL_PROXY': 'socks://127.0.0.1:7890',
                    'HTTPS_PROXY': 'http://127.0.0.1:7890'}, clear=True):
                client, name = ModelClient({})._get_model_info_from_config(config('https://model.invalid', azure))
                self.addCleanup(client.close)
                self.assertEqual(name, 'test-model')
                self.assertEqual(client.max_retries, 0)
                self.assertEqual(client.timeout, 3)

    def test_unknown_proxy_scheme_still_fails_instead_of_silent_direct_fallback(self):
        with patch.dict(os.environ, {'ALL_PROXY': 'unknown://localhost:7890'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Unknown scheme'):
                ModelClient({})._get_model_info_from_config(config('https://model.invalid'))

    def test_requests_keep_explicit_http_proxy_and_local_no_proxy(self):
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                paths.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.dumps({'id': 'fixture', 'object': 'chat.completion', 'created': 0,
                                   'model': 'test-model', 'choices': [{'index': 0,
                                   'message': {'role': 'assistant', 'content': 'ok'},
                                   'finish_reason': 'stop'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            local = f'http://127.0.0.1:{server.server_port}'
            for endpoint, proxy, bypass in [('http://model.invalid/v1', local, ''),
                                            (local + '/v1', 'http://127.0.0.1:1', '127.0.0.1')]:
                with patch.dict(os.environ, {'ALL_PROXY': 'socks://127.0.0.1:1',
                                             'HTTP_PROXY': proxy, 'NO_PROXY': bypass}, clear=True):
                    model = ModelClient({'model': config(endpoint)})
                    try:
                        self.assertEqual(model([{'role': 'user', 'content': 'test'}]), 'ok')
                    finally:
                        if model.global_model: model.global_model.close()
            self.assertEqual(paths, ['http://model.invalid/v1/chat/completions', '/v1/chat/completions'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
