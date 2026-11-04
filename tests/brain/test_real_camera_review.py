"""Regression checks from cross-agent review; synthetic images, no network."""
import base64
import hashlib
import os
import unittest
from unittest.mock import Mock, patch

from brain.adapters.http_client import HttpContractError
from brain.adapters.vla_camera import VlaCameraClient
from tests.brain.test_real_camera import ROUTE, picture


class HeadCameraReviewTests(unittest.TestCase):
    def test_empty_or_null_auth_config_never_sends_anonymous_request(self):
        for empty in ('', None):
            with self.subTest(token_env=empty), patch.dict(os.environ, {}, clear=True), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('anonymous request')) as network:
                client = VlaCameraClient(ROUTE['url'], config={
                    'reception_real': {'body_control': {'vla_token_env': '', 'token_env': empty}}})
                with self.assertRaises(HttpContractError):
                    client.latest()
                network.assert_not_called()

    def test_matching_hash_does_not_make_truncated_jpeg_valid(self):
        frame, raw = picture()
        truncated = raw[:-20]
        frame['image_base64'] = base64.b64encode(truncated).decode('ascii')
        frame['sha256'] = hashlib.sha256(truncated).hexdigest()
        http = Mock()
        http.request_json.return_value = frame, 200, {}
        with self.assertRaises(HttpContractError):
            VlaCameraClient(ROUTE['url'], http=http).latest()


if __name__ == '__main__':
    unittest.main()
