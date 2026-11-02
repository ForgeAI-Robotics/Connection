"""Opt-in diagnostic response bounds leave existing transport defaults unchanged."""
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from brain.adapters.http_client import HttpClient, HttpContractError


class Response(BytesIO):
    status = 200
    headers = {'Content-Type': 'application/json'}

    def __init__(self, content):
        super().__init__(content)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class HttpResponseBoundsTests(unittest.TestCase):
    def test_opt_in_reads_only_limit_plus_one_and_rejects_oversize(self):
        response = Response(b'x' * 100000)
        with patch('urllib.request.urlopen', return_value=response):
            with self.assertRaises(HttpContractError):
                HttpClient('http://offline.invalid').request_bytes('GET', '/report', max_response_bytes=64)
        self.assertEqual(response.read_sizes, [65])

    def test_default_read_is_unchanged(self):
        response = Response(b'normal')
        with patch('urllib.request.urlopen', return_value=response):
            raw, status, _ = HttpClient('http://offline.invalid').request_bytes('GET', '/normal')
        self.assertEqual(raw, b'normal')
        self.assertEqual(status, 200)
        self.assertEqual(response.read_sizes, [-1])

    def test_http_error_read_is_bounded_too(self):
        response = Response(b'x' * 100000)
        error = HTTPError('http://offline.invalid/report', 503, 'Unavailable', {}, response)
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(HttpContractError):
                HttpClient('http://offline.invalid').request_bytes('GET', '/report', max_response_bytes=32)
        self.assertEqual(response.read_sizes, [33])

    def test_opt_in_error_message_excludes_untrusted_response_body(self):
        response = Response(b'{"secret":"test-only-credential"}')
        error = HTTPError('http://offline.invalid/report', 403, 'Forbidden', {}, response)
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(HttpContractError) as raised:
                HttpClient('http://offline.invalid', error_body_in_message=False).request_bytes(
                    'GET', '/report', max_response_bytes=1024)
        self.assertNotIn('test-only-credential', str(raised.exception))
        self.assertEqual(raised.exception.status_code, 403)

    def test_authenticated_transport_refuses_cross_origin_before_open(self):
        client = HttpClient('https://gateway.invalid', error_body_in_message=False)
        client.auth_token_env = 'TEST_DIAGNOSTIC_TOKEN'
        with patch('brain.adapters.http_client.service_token', return_value='test-only-credential'), \
                patch('urllib.request.build_opener') as build:
            with self.assertRaises(HttpContractError) as raised:
                client.request_bytes('GET', 'https://other.invalid/report', max_response_bytes=65536)
        build.assert_not_called()
        self.assertNotIn('test-only-credential', str(raised.exception))

    def test_authenticated_transport_installs_no_redirect_handler(self):
        client = HttpClient('https://gateway.invalid', error_body_in_message=False)
        client.auth_token_env = 'TEST_DIAGNOSTIC_TOKEN'
        response = Response(b'{}')
        with patch('brain.adapters.http_client.service_token', return_value='test-only-credential'), \
                patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value = response
            client.request_bytes('GET', '/report', max_response_bytes=65536)
        no_redirect = build.call_args.args[1]
        self.assertIsNone(no_redirect.redirect_request(None, None, 302, 'Moved', {}, 'https://other.invalid'))


if __name__ == '__main__':
    unittest.main()
