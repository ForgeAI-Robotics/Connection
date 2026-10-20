"""Local vision transport and fail-closed behavior, without GPU or robot access."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from brain.adapters import vlm
from brain.adapters.reception_verify import ReceptionVerifier, VerificationError
from shared.vision_options import generation_options, client_options


LOCAL = {
    'model': 'local-vision', 'api_base': 'http://127.0.0.1:18035/v1',
    'auth_required': False, 'timeout_sec': 17,
    'extra_body': {'chat_template_kwargs': {'enable_thinking': False}},
}


def reply(content):
    return io.BytesIO(json.dumps({'choices': [{'message': {'content': content}}]}).encode())


class LocalVisionTests(unittest.TestCase):
    def test_local_sdk_ignores_invalid_desktop_proxy_without_changing_cloud(self):
        with patch.dict(os.environ, {'ALL_PROXY': 'socks://127.0.0.1:7890'}):
            options = client_options('http://127.0.0.1:18035/v1', timeout=12)
            options['http_client'].close()
            remote = client_options('https://example.invalid/v1', timeout=12)
        self.assertEqual(options['timeout'], 12)
        self.assertNotIn('http_client', remote)

    def test_frame_sequence_keeps_local_credentials_and_generation_settings(self):
        from brain.learning import video
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(vlm, '_vlm_cfg', return_value=LOCAL), \
                patch.object(vlm, '_api_key', side_effect=AssertionError('cloud key consulted')), \
                patch.object(vlm, '_to_jpg_b64', return_value='fixture'), \
                patch.object(video, '_extract_frames', return_value=[('frame.jpg', 0)]), \
                patch('urllib.request.urlopen', return_value=reply('{"task_summary":"test","segments":[]}')) as send:
            result = video.video_to_actions('fixture.mp4')
        self.assertEqual(result['segments'], [])
        body = json.loads(send.call_args.args[0].data)
        self.assertFalse(body['chat_template_kwargs']['enable_thinking'])
        self.assertNotIn('max_tokens', body)  # Preserve the prior unconfigured video default.

    def test_verifier_local_needs_no_cloud_key_and_forwards_thinking(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch('urllib.request.urlopen', return_value=reply('{"target_visible":false}')) as send:
            result = ReceptionVerifier({'vlm': LOCAL})._analyze_image(
                'pick', 'item', 'table', b'image', 'image/jpeg')
        self.assertFalse(result['target_visible'])
        request = send.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, LOCAL['api_base'] + '/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer local')
        self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(body['max_tokens'], 400)
        self.assertNotIn('extra_body', body)
        self.assertEqual(send.call_args.kwargs['timeout'], 17)

    def test_cloud_verifier_still_requires_its_own_key(self):
        with patch.dict(os.environ, {}, clear=True), patch('urllib.request.urlopen') as send:
            with self.assertRaises(VerificationError):
                ReceptionVerifier({})._chat(
                    {'model': 'cloud', 'api_base': 'https://example.invalid/v1',
                     'api_key_env': 'MISSING_KEY'}, [], max_tokens=300)
            send.assert_not_called()

    def test_raw_extra_cannot_replace_images_identity_or_budget(self):
        for key in ('model', 'messages', 'stream', 'max_tokens'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                generation_options({'extra_body': {key: 'wrong'}}, max_tokens=400, raw=True)

    def test_environment_endpoint_override_does_not_inherit_local_noauth(self):
        with patch.object(vlm, '_vlm_cfg', return_value=LOCAL), \
                patch.dict(os.environ, {'VLM_API_BASE': 'https://example.invalid/v1',
                                       'VLM_MODEL': 'cloud'}):
            endpoint = vlm._endpoint_cfg()
        self.assertTrue(endpoint.get('auth_required', True))

    def test_desk_relation_images_and_local_options_reach_http(self):
        sop = {'trash': [], 'keep_in_place': [], 'standard_layout': [], 'restock': []}
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'current.jpg'
            image.write_bytes(b'fixture')
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(vlm, '_vlm_cfg', return_value=LOCAL), \
                    patch.object(vlm, '_api_key', side_effect=AssertionError('cloud key consulted')), \
                    patch('urllib.request.urlopen', return_value=reply('[{"object":"手机","action":"keep"}]')) as send:
                result, error = vlm.judge(folder, folder, sop, str(image))
        self.assertIsNone(error)
        self.assertEqual(result[0]['action'], 'keep')
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(sum(p['type'] == 'image_url' for p in body['messages'][0]['content']), 2)
        self.assertFalse(body['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(body['max_tokens'], 2000)

    def test_legacy_camera_never_reports_model_failure_as_normal(self):
        from execution.slaver.robot.module import camera
        for content in ('', '无法判断', '画面模糊\nunknown'):
            with self.subTest(content=content), patch.object(camera, '_vlm_cfg', LOCAL), \
                    patch('openai.OpenAI') as factory:
                factory.return_value.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
                state, _ = camera._call_vlm({'head': 'fixture'}, '执行结果诊断')
                self.assertEqual(state, 'unknown')
        with patch.object(camera, '_vlm_cfg', LOCAL), patch('openai.OpenAI', side_effect=RuntimeError('offline')):
            state, _ = camera._call_vlm({'head': 'fixture'}, '执行结果诊断')
            self.assertEqual(state, 'unknown')
        with patch.object(camera, 'backend_capture_image', return_value={'success': True, 'image': 'fixture'}), \
                patch.object(camera, '_call_vlm', return_value=('unknown', 'offline')):
            result = json.loads(camera.capture_scene('诊断', 'head'))
        self.assertEqual(result[1]['_status'], 'failure')

    def test_vision_noauth_does_not_change_text_adjudicator_request(self):
        cloud = {'model': 'deepseek-chat', 'api_base': 'https://example.invalid',
                 'api_key_env': 'TEST_TEXT_KEY'}
        with patch.dict(os.environ, {'TEST_TEXT_KEY': 'test-text-secret'}), \
                patch('urllib.request.urlopen', return_value=reply('{}')) as send:
            ReceptionVerifier({'vlm': LOCAL, 'llm': cloud})._adjudicate(
                'pick', 'item', 'table', {}, {}, {'target_visible': False})
        request = send.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-text-secret')
        self.assertEqual(body['model'], 'deepseek-chat')
        self.assertEqual(body['max_tokens'], 300)
        self.assertNotIn('chat_template_kwargs', body)


if __name__ == '__main__':
    unittest.main()
