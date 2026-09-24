"""Real localhost processes, isolated data, mock body only. No external robot calls."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import requests
import yaml
from clients.brain import BrainClient


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ProcessIntegrationTests(unittest.TestCase):
    def test_web_exit_keeps_brain_task_and_direct_feishu_client_alive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config.yaml'
            config.write_text(yaml.safe_dump({'brain': {'capture_timeline': False}, 'reflection': {'enabled': False},
                'reception_real': {'kernel_runtime_dir': str(root / 'ledger'), 'kernel_enabled': False}}))
            brain_port, web_port = free_port(), free_port()
            env = dict(os.environ, CONNECTION_WORKSPACE=folder, FQ_EXECUTION_STATE=str(root / 'none.json'),
                       FQ_EXECUTION_LOCK=str(root / 'execution.lock'), FQ_EXECUTION_BLOCK=str(root / 'blocked'),
                       RECEPTION_MODE='mock', FQPLANNER_LOG_ROOT=str(root / 'logs'))
            log = (root / 'process.log').open('w+')
            processes = []
            def start(module, *args):
                process = subprocess.Popen([sys.executable, '-m', module, *args], cwd='/tmp', env=env, stdout=log, stderr=log)
                processes.append(process)
                return process
            def ready(url):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        if requests.get(url, timeout=.5).status_code == 200:
                            return
                    except requests.RequestException:
                        pass
                    time.sleep(.05)
                log.flush(); log.seek(0)
                self.fail(log.read())
            try:
                brain_url = f'http://127.0.0.1:{brain_port}'
                web_url = f'http://127.0.0.1:{web_port}'
                start('brain', '--config', str(config), '--port', str(brain_port), '--host', '127.0.0.1')
                ready(brain_url + '/health')
                web = start('entries.web', '--brain-url', brain_url, '--port', str(web_port), '--host', '127.0.0.1')
                ready(web_url + '/')
                response = requests.post(web_url + '/api/reception/run', json={'headcount': 2}, timeout=10)
                self.assertTrue(response.json()['accepted'], response.text)
                task_id = response.json()['task_id']
                web.terminate(); web.wait(5)
                client = BrainClient(brain_url)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    status = asyncio.run(client.get_status()).raw
                    if status.get('state') == 'succeeded': break
                    time.sleep(.05)
                self.assertEqual(status['state'], 'succeeded', status)
                self.assertEqual(status['task_id'], task_id)
                response = asyncio.run(client.publish_task('开始接待', 'direct-feishu-client'))
                self.assertTrue(response['accepted'])
                self.assertEqual(response['task_id'], 'direct-feishu-client')
                record = json.loads((root / 'ledger/current_task.json').read_text())
                self.assertEqual(record['entry'], 'feishu')
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try: process.wait(12)
                        except subprocess.TimeoutExpired: process.kill(); process.wait(3)
                log.close()

    def test_bad_ledger_never_becomes_an_empty_task(self):
        from brain.storage.tasks import KernelStore, load_existing
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'current_task.json'
            for raw in ('[]', '{}', '{broken'):
                path.write_text(raw)
                with self.assertRaises(ValueError): load_existing(folder)
                with self.assertRaises(ValueError): KernelStore(folder).save_state({'task_id': 'replacement', 'state': 'running'})
                self.assertEqual(path.read_text(), raw)
