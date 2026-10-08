import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from shared.quiet_http_log import QuietPollFilter, install_poll_diagnostics


class QuietHttpLogTests(unittest.TestCase):
    def test_response_errors_include_source_reason_and_full_original_only_on_change(self):
        from flask import Flask, g, jsonify
        app = Flask(__name__)
        state = {'status': 503, 'error': 'Connection refused: ' + 'detail ' * 150}
        @app.get('/api/robot_status')
        def status():
            g.poll_diagnostic = {'target': 'http://192.0.2.5:8001/scene',
                                 'reason': '大脑访问下游观察服务失败'}
            return jsonify(error=state['error']), state['status']
        with patch.object(logging.getLogger('werkzeug'), 'filters', []), \
             patch('shared.quiet_http_log.logging.getLogger', wraps=logging.getLogger):
            install_poll_diagnostics(app)
            client = app.test_client()
            with patch.object(logging.getLogger('brain.http_monitor'), 'error') as error, \
                 patch.object(logging.getLogger('brain.http_monitor'), 'info') as info:
                response = client.get('/api/robot_status?ts=1', environ_overrides={'REMOTE_ADDR': '192.0.2.55'})
                self.assertEqual(response.json['error'], state['error'])
                line = error.call_args.args[0] % error.call_args.args[1:]
                for value in ('监测失败', '机器人现场状态', '/api/robot_status', 'HTTP 503',
                              '来源 IP=192.0.2.55', 'target=http://192.0.2.5:8001/scene',
                              '大脑访问下游观察服务失败', state['error']):
                    self.assertIn(value, line)
                self.assertNotIn('原始错误', line)
                client.get('/api/robot_status?ts=2')
                self.assertEqual(error.call_count, 1)
                state['error'] = 'Read timed out'
                client.get('/api/robot_status')
                self.assertEqual(error.call_count, 2)  # New cause, same HTTP status.
                for elapsed in (61, 3600, 86400):
                    with patch('time.monotonic', return_value=elapsed):
                        client.get('/api/robot_status')
                self.assertEqual(error.call_count, 2)
                state['status'] = 200
                client.get('/api/robot_status')
                client.get('/api/robot_status')
                self.assertEqual(info.call_count, 1)
                self.assertIn('监测恢复', info.call_args.args[0])
                state['status'] = 503
                client.get('/api/robot_status')
                self.assertEqual(error.call_count, 3)  # Same error after recovery is new.
            access = logging.LogRecord('werkzeug', logging.INFO, '', 0,
                '192.0.2.55 - - [time] "GET /api/robot_status HTTP/1.1" 503 -', (), None)
            self.assertFalse(logging.getLogger('werkzeug').filter(access))

    def test_real_views_explain_local_configuration_errors_without_claiming_remote_failure(self):
        from flask import Flask
        from brain.api.views import register_views
        app = Flask(__name__)
        with patch('brain.learning.demo.DemoLearning'), \
             patch.object(logging.getLogger('werkzeug'), 'filters', []):
            register_views(app, Mock())
            install_poll_diagnostics(app)
            client = app.test_client()
            with patch('brain.api.views.observation_url', side_effect=ValueError('observation 尚无真机适配')), \
                 patch('brain.api.views.requests.request') as remote, \
                 self.assertLogs('brain.http_monitor', level='WARNING') as logged:
                for path in ('/api/robot_status', '/api/quad_latest'):
                    self.assertEqual(client.get(path).status_code, 503)
                remote.assert_not_called()
            for line in logged.output:
                self.assertIn('尚未发出下游请求', line)
                self.assertIn('未解析到下游地址', line)
                self.assertIn('observation 尚无真机适配', line)
            with patch('execution.robot_api.config.load_robot_api_config', return_value=Mock(server_url='')), \
                 self.assertLogs('brain.http_monitor', level='WARNING') as logged:
                response = client.get('/api/belief')
                self.assertEqual(response.status_code, 503)
                self.assertIn('Invalid URL', response.json['error'])
            self.assertIn('目标地址为空或格式无效', logged.output[0])
            self.assertIn(response.json['error'], logged.output[0])

    def test_downstream_http_error_preserves_origin_target_and_nested_error(self):
        from flask import Flask
        from brain.api.views import register_views
        app = Flask(__name__)
        with patch('brain.learning.demo.DemoLearning'), \
             patch.object(logging.getLogger('werkzeug'), 'filters', []):
            register_views(app, Mock())
            install_poll_diagnostics(app)
            remote = Mock(status_code=502, content=b'{"error":{"code":"CAMERA_OFFLINE","detail":"raw detail"}}',
                          headers={'content-type': 'application/json'})
            with patch('brain.api.views.observation_url', return_value='http://192.0.2.5:8001'), \
                 patch('brain.api.views.requests.request', return_value=remote), \
                 self.assertLogs('brain.http_monitor', level='WARNING') as logged:
                response = app.test_client().get('/api/quad_latest')
            self.assertEqual(response.status_code, 502)
            for value in ('下游观察服务返回异常', 'target=http://192.0.2.5:8001/camera/latest',
                          'CAMERA_OFFLINE', 'raw detail'):
                self.assertIn(value, logged.output[0])

    def test_only_known_successful_polls_are_silent_and_failures_recover(self):
        quiet = QuietPollFilter()

        def record(path, code=200, method='GET'):
            return logging.LogRecord('werkzeug', logging.INFO, '', 0,
                '127.0.0.1 - - [time] "%s %s HTTP/1.1" %s -', (method, path, code), None)

        self.assertFalse(quiet.filter(record('/health')))
        self.assertFalse(quiet.filter(record('/api/task_status?ts=1')))
        failed = record('/health', 503)
        self.assertTrue(quiet.filter(failed))
        self.assertIn('监测失败', failed.getMessage())
        self.assertFalse(quiet.filter(record('/health', 503)))
        self.assertTrue(quiet.filter(record('/health', 500)))
        for elapsed in (61, 3600, 86400):
            with patch('time.monotonic', return_value=elapsed):
                self.assertFalse(quiet.filter(record('/health', 500)))
        recovered = record('/health')
        self.assertTrue(quiet.filter(recovered))
        self.assertIn('监测恢复', recovered.getMessage())
        self.assertFalse(quiet.filter(record('/health')))
        for code in (200, 400, 500):
            for _ in range(2):
                self.assertTrue(quiet.filter(record('/publish_task', code, 'POST')))
                self.assertTrue(quiet.filter(record('/unexpected-route', code)))
        exception = logging.LogRecord('werkzeug', logging.ERROR, '', 0, 'request crashed', (), None)
        self.assertTrue(quiet.filter(exception))

    def test_process_log_contains_print_info_business_and_full_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, FQPLANNER_LOG_ROOT=directory)
            env.pop('FQPLANNER_PROCESS_LOG', None)
            result = subprocess.run([sys.executable, '-c', '''
import logging
from shared.log_setup import attach_process_log
attach_process_log('master')
print('plain stdout')
logging.getLogger('brain').info('ordinary INFO')
from shared.brain_journal import emit
emit('TASK', event='task_opened', text='business ' + 'x' * 500 + ' END')
http = logging.getLogger('werkzeug')
http.info('127.0.0.1 - - [now] "GET /health HTTP/1.1" 200 -')
http.info('127.0.0.1 - - [now] "POST /publish_task HTTP/1.1" 200 -')
http.info('WARNING: This is a development server. Do not use it in a production deployment.')
logging.getLogger('httpx').info('HTTP Request: POST https://api.deepseek.com/chat/completions "HTTP/1.1 200 OK"')
try:
    raise ValueError('full error detail')
except ValueError:
    logging.getLogger('brain').exception('execution failed')
raise RuntimeError('uncaught error detail')
'''], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            logs = [p for p in Path(directory).glob('*/master/*.log')
                    if p.name not in {'brain.log', 'http-access.log'}]
            self.assertEqual(len(logs), 1)
            text = logs[0].read_text()
            for message in ('plain stdout', 'ordinary INFO', 'business ' + 'x' * 500 + ' END',
                            '大脑进程已启动', 'Traceback', 'ValueError: full error detail',
                            'RuntimeError: uncaught error detail',
                            'execution failed ValueError: full error detail'):
                self.assertIn(message, text)
            self.assertEqual(text.count('Traceback'), 1)
            for message in ('GET /health', 'POST /publish_task', 'development server',
                            'HTTP Request: POST https://api.deepseek.com'):
                self.assertNotIn(message, text)
            self.assertEqual(text.count('ValueError: full error detail'), 1)

    def test_panel_probe_success_is_silent_failure_only_on_change_and_recovery_logged(self):
        from ops import services
        with patch.dict(services._LAST_MONITOR, {}, clear=True), \
             patch('ops.services.append_monitor_log') as write:
            services._record_probe('test', 'healthy', ok=True)
            services._record_probe('test', 'healthy', ok=True)
            write.assert_not_called()
            services._record_probe('test', 'unreachable', ok=False)
            services._record_probe('test', 'unreachable', ok=False)
            self.assertEqual(write.call_count, 1)
            with patch('time.monotonic', return_value=86400):
                services._record_probe('test', 'unreachable', ok=False)
            self.assertEqual(write.call_count, 1)
            services._record_probe('test', 'timeout', ok=False)
            self.assertEqual(write.call_count, 2)
            services._record_probe('test', 'healthy', ok=True)
            self.assertIn('监测恢复', write.call_args.args[1])
            services._record_probe('test', 'healthy', ok=True)
            self.assertEqual(write.call_count, 3)
