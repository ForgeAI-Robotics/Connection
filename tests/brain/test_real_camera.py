"""Head-camera reads use authenticated real evidence, never robot commands."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from brain.adapters import camera, vla_camera
from brain.adapters.execution import parse_capture_scene
from brain.adapters.http_client import HttpContractError
from shared.execution_profile import resolve


def picture():
    output = io.BytesIO()
    Image.new('RGB', (64, 48), 'blue').save(output, format='JPEG')
    raw = output.getvalue()
    stamp = (datetime.now(timezone.utc) - timedelta(milliseconds=10)).isoformat()
    frame = dict(schema=vla_camera.FRAME_SCHEMA, camera_key='ego_view', frame_id='instance-1/frame-2',
        captured_at=stamp, received_at=stamp, age_ms=10., received_age_ms=10.,
        width=64, height=48, content_type='image/jpeg', sha256=hashlib.sha256(raw).hexdigest(),
        image_base64=base64.b64encode(raw).decode())
    return frame, raw


ROUTE = dict(mode='real', backend='vla_camera', available=True, url='http://vla.invalid:8091')


class HeadCameraTests(unittest.TestCase):
    def read(self, frame):
        http = Mock()
        http.request_json.return_value = frame, 200, {}
        result = vla_camera.VlaCameraClient(ROUTE['url'], http=http).latest()
        self.assertEqual(http.auth_token_env, 'BRAIN_CONTROL_TOKEN')
        self.assertEqual(http.request_json.call_args.args,
                         ('GET', '/v1/camera/frames/latest?camera=ego_view'))
        self.assertEqual(http.request_json.call_count, 1)
        return result

    def test_frame_integrity_and_metadata_no_image_in_evidence(self):
        frame, raw = picture()
        metadata, actual = self.read(frame)
        self.assertEqual(actual, raw)
        self.assertEqual(metadata['captured_at'], frame['captured_at'])
        self.assertNotIn('image_base64', metadata)

    def test_stale_unknown_wrong_view_and_corrupt_frames_fail_closed(self):
        mutations = [dict(schema='old'), dict(camera_key='left_wrist'), dict(frame_id=''),
            dict(captured_at=None), dict(captured_at='2020-01-01T00:00:00+00:00'),
            dict(captured_at=datetime.now().isoformat()), dict(received_age_ms=501),
            dict(age_ms=float('nan')), dict(age_ms=True), dict(age_ms=-1),
            dict(content_type='text/html'), dict(sha256='0'*64), dict(image_base64='!'),
            dict(width=65), dict(width=True), dict(image_base64='A'*(3*1024*1024))]
        for change in mutations:
            with self.subTest(change=list(change)):
                frame, _ = picture(); frame.update(change)
                with self.assertRaises(HttpContractError): self.read(frame)

    def test_future_capture_and_slow_transport_cannot_be_fresh(self):
        frame, _ = picture()
        frame['captured_at'] = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        with self.assertRaises(HttpContractError): self.read(frame)
        frame, _ = picture()
        with patch('brain.adapters.vla_camera.time.monotonic', side_effect=[0, 1]), self.assertRaises(HttpContractError):
            self.read(frame)

    def test_missing_auth_does_not_send_anonymous_or_use_simulation(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch('urllib.request.urlopen') as net, self.assertRaises(HttpContractError):
            vla_camera.VlaCameraClient(ROUTE['url']).latest()
        net.assert_not_called()

    def test_old_applied_unavailable_profile_is_not_silently_upgraded(self):
        route = dict(ROUTE, available=False, backend='unavailable', reason='old unapplied route')
        with patch.object(vla_camera, 'applied_profile', return_value={'routes': {'observation': route}}), \
                patch('execution.robot_api.client.capture_image') as sim, patch('openai.OpenAI') as model:
            with self.assertRaisesRegex(ValueError, 'old unapplied'): camera.capture_scene('看看前面')
        sim.assert_not_called(); model.assert_not_called()

    def test_real_capture_passes_one_head_image_and_provenance_to_runtime(self):
        frame, raw = picture(); metadata = {k: v for k, v in frame.items() if k != 'image_base64'}
        with patch.object(vla_camera, 'real_camera_route', return_value=ROUTE), \
                patch.object(vla_camera, 'capture_head', return_value=(metadata, raw)) as capture, \
                patch('execution.robot_api.client.capture_image') as sim, \
                patch.object(camera, '_endpoint_cfg', return_value=dict(api_base='https://example.invalid/v1',
                    model='cloud-vision', key_names=('TEST_KEY',))), \
                patch.object(camera, '_api_key', return_value='fixture-key'), \
                patch.object(camera, '_vlm_cfg', return_value={'extra_body': {'enable_thinking': False}}), \
                patch('openai.OpenAI') as model:
            model.return_value.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='前方有一张桌子'))])
            result = camera.capture_scene('桌上有什么')
            send = model.return_value.chat.completions.create.call_args.kwargs
        sim.assert_not_called(); capture.assert_called_once()
        self.assertEqual(sum(x['type']=='image_url' for x in send['messages'][0]['content']), 1)
        evidence = parse_capture_scene(result)
        self.assertEqual(evidence['camera_frame']['frame_id'], frame['frame_id'])
        self.assertEqual(datetime.fromisoformat(evidence['observation']['observed_at']),
                         datetime.fromisoformat(frame['captured_at']).replace(microsecond=(datetime.fromisoformat(frame['captured_at']).microsecond//1000)*1000))
        self.assertNotIn('image_base64', json.dumps(evidence))

    def test_capture_failure_never_calls_model_and_never_falls_back(self):
        with patch.object(vla_camera, 'real_camera_route', return_value=ROUTE), \
                patch.object(vla_camera, 'capture_head', side_effect=HttpContractError('no frame')), \
                patch('execution.robot_api.client.capture_image') as sim, patch('openai.OpenAI') as model:
            with self.assertRaisesRegex(HttpContractError, 'no frame'): camera.capture_scene('看看前面')
        sim.assert_not_called(); model.assert_not_called()

    def test_long_inference_does_not_refresh_observation_age(self):
        frame, _ = picture()
        frame['captured_at'] = (datetime.now(timezone.utc)-timedelta(seconds=6)).isoformat()
        text = json.dumps(['视野描述（ego_view）：一个杯子', {'_status':'success','camera_frame':frame}])
        self.assertIsNone(parse_capture_scene(text, ttl_sec=5))
        self.assertIsNotNone(parse_capture_scene(text, ttl_sec=10))

    def test_new_real_profile_uses_current_control_endpoint(self):
        with patch('shared.networks.lan') as network:
            network.return_value.http.return_value = 'http://192.0.2.7:8091'
            route = resolve({'mode':'real'}, {})['routes']['observation']
        self.assertEqual(route['backend'], 'vla_camera')
        self.assertTrue(route['available'])
        self.assertEqual(route['url'], 'http://192.0.2.7:8091')

    def test_web_image_proxy_retains_frame_identity_and_no_motion_calls(self):
        from brain.api.app import create_app
        application = Mock(); application.service.config = {}
        app = create_app(application)
        metadata, image = picture(); metadata.pop('image_base64')
        with patch.object(vla_camera, 'real_camera_route', return_value=ROUTE), \
                patch.object(vla_camera, 'capture_head', return_value=(metadata,image)) as capture, \
                patch('brain.api.views.requests.request') as generic:
            response = app.test_client().get('/api/quad_latest')
            state = app.test_client().get('/api/robot_status')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data,image)
        self.assertEqual(response.headers['X-Frame-Id'],metadata['frame_id'])
        self.assertEqual(state.status_code,503)
        capture.assert_called_once(); generic.assert_not_called()


if __name__ == '__main__': unittest.main()
