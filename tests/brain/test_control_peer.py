"""Actual HTTPS client, trusted certificate, isolated credentials and no redirects."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from brain.adapters.http_client import HttpClient, HttpContractError


class GatewayTLSClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();root=Path(cls.temp.name)
        cls.cert=str(root/'server.crt');key=str(root/'server.key')
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1',
            '-keyout',key,'-out',cls.cert],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('Authorization')!='Bearer gateway-test-secret':
                    self.send_response(401);self.end_headers();return
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','https://192.0.2.99/status');self.end_headers();return
                self.send_response(200);self.end_headers();self.wfile.write(b'{"ready":true}')
            def log_message(self,*args):pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cls.cert,key)
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url='https://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def setUp(self):
        self.env=patch.dict(os.environ,{'BRAIN_GATEWAY_TOKEN':'gateway-test-secret','NAV_CONTROL_TOKEN':'nav-test-secret'})
        self.env.start();self.addCleanup(self.env.stop)
    def client(self,*,trusted=True):
        c=HttpClient(self.url,timeout_sec=.3,ca_file=self.cert if trusted else None)
        c.auth_token_env='BRAIN_GATEWAY_TOKEN';return c
    def test_trusted_certificate_and_gateway_role_work(self):
        self.assertTrue(self.client().request_json('GET','/status')[0]['ready'])
    def test_untrusted_certificate_is_rejected(self):
        with self.assertRaises(HttpContractError):self.client(trusted=False).request_json('GET','/status')
    def test_nav_credential_cannot_replace_gateway_credential(self):
        c=self.client();c.auth_token_env='NAV_CONTROL_TOKEN'
        with self.assertRaises(HttpContractError) as result:c.request_json('GET','/status')
        self.assertEqual(result.exception.status_code,401)
    def test_redirects_and_cross_origin_never_receive_credentials(self):
        c=self.client()
        with self.assertRaises(HttpContractError) as result:c.request_json('GET','/redirect')
        self.assertEqual(result.exception.status_code,302)
        with self.assertRaisesRegex(HttpContractError,'其他服务'):c.request_json('GET','https://192.0.2.99/status')
