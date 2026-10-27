import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brain.adapters.http_client import HttpClient, HttpContractError
from shared.service_credentials import service_token


class ServiceCredentialTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'credential'
        self.path.write_text('private-test-token');self.path.chmod(0o600)
        env=patch.dict(os.environ,{'TEST_SERVICE_TOKEN':'environment-test-token',
                                 'TEST_SERVICE_TOKEN_FILE':str(self.path)})
        env.start();self.addCleanup(env.stop)

    def test_file_precedes_env_and_is_reread(self):
        self.assertEqual(service_token('TEST_SERVICE_TOKEN'),'private-test-token')
        self.path.write_text('rotated-test-token\n')
        self.assertEqual(service_token('TEST_SERVICE_TOKEN'),'rotated-test-token')

    def test_bad_file_does_not_fall_back_to_env(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):service_token('TEST_SERVICE_TOKEN')
        self.path.unlink()
        with self.assertRaises(ValueError):service_token('TEST_SERVICE_TOKEN')

    def test_symlink_empty_and_malformed_are_rejected_without_secret(self):
        target=self.path.with_name('target');target.write_text('private-test-token');target.chmod(0o600)
        self.path.unlink();self.path.symlink_to(target)
        with self.assertRaises(ValueError):service_token('TEST_SERVICE_TOKEN')
        self.path.unlink();self.path.touch(mode=0o600)
        for token in ('','secret\r\ninjected','x'*4097):
            self.path.write_text(token)
            with self.assertRaises(ValueError) as caught:service_token('TEST_SERVICE_TOKEN')
            self.assertNotIn('secret',str(caught.exception))

    def test_env_compatibility_and_no_implicit_cross_service_fallback(self):
        with patch.dict(os.environ,{'TEST_SERVICE_TOKEN_FILE':''}):
            self.assertEqual(service_token('TEST_SERVICE_TOKEN'),'environment-test-token')
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(service_token('UNCONFIGURED_NAV_TOKEN'),'')

    def test_http_rejects_bad_file_before_network(self):
        self.path.chmod(0o644)
        client=HttpClient('http://127.0.0.1:1');client.auth_token_env='TEST_SERVICE_TOKEN'
        with self.assertRaises(HttpContractError):client.request_json('GET','/status')
