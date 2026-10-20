"""Configured inference budgets reach the model; defaults remain compatible."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from brain.adapters.model import ModelClient
from brain.planning import PlanningService
from contracts.tasks import Rejected


def response():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='描述'))], usage=None)


class ModelGenerationTests(unittest.TestCase):
    def call_model(self, settings):
        model = ModelClient({'model': settings})
        model.global_model = Mock(base_url='http://127.0.0.1:18035/v1')
        model.model_name = 'Qwen3.5-9B'
        model.global_model.chat.completions.create.return_value = response()
        self.assertEqual(model([{'role': 'user', 'content': 'test'}]), '描述')
        return model.global_model.chat.completions.create.call_args.kwargs

    def test_existing_default_generation_is_unchanged(self):
        args = self.call_model({})
        self.assertEqual((args['temperature'], args['top_p'], args['max_tokens'], args['seed']),
                         (0.2, 0.9, 2048, 42))
        self.assertNotIn('extra_body', args)

    def test_thinking_budget_and_sampling_are_not_silently_capped(self):
        settings = dict(max_tokens=32768, temperature=1.0, top_p=.95, seed=7,
                        presence_penalty=1.5,
                        extra_body={'top_k': 20, 'chat_template_kwargs': {'enable_thinking': True}})
        args = self.call_model(settings)
        for key, value in settings.items():
            self.assertEqual(args[key], value)

    def test_camera_forwards_long_generation_and_transport_timeout(self):
        from brain.adapters import camera
        cfg = dict(max_tokens=32768, temperature=1., top_p=.95, presence_penalty=1.5,
                   seed=7, timeout_sec=1800,
                   extra_body={'top_k': 20, 'chat_template_kwargs': {'enable_thinking': True}})
        with patch.object(camera, '_vlm_cfg', return_value=cfg), \
             patch.object(camera, '_endpoint_cfg', return_value={'key_names': (), 'api_base': 'http://localhost/v1', 'model': 'test'}), \
             patch.object(camera, '_api_key', return_value='local'), \
             patch('execution.robot_api.client.capture_image', return_value={'success': True, 'image': 'abc'}), \
             patch('openai.OpenAI') as client:
            client.return_value.chat.completions.create.return_value = response()
            self.assertIn('描述', camera.capture_scene('桌面', camera_name='head_cam'))
            self.assertEqual(client.call_args.kwargs['timeout'], 1800)
            args = client.return_value.chat.completions.create.call_args.kwargs
            for key, value in cfg.items():
                if key != 'timeout_sec': self.assertEqual(args[key], value)

    def test_observation_deadline_is_configured_and_validated(self):
        runtime = SimpleNamespace(package_name='look')
        for settings, expected in [({}, 30), ({'observation_timeout_sec': 1800}, 1800)]:
            service = PlanningService({'brain': settings}, None, None, None)
            plan = service.plan(runtime, {})
            self.assertEqual(plan.steps[0].deadline_sec, expected)
        for value in (0, -1, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(Rejected):
                PlanningService({'brain': {'observation_timeout_sec': value}}, None, None, None).plan(runtime, {})
