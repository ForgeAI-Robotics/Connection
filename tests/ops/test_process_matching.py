"""Process cleanup must never mistake tmux/SSH launch text for a service."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ops.services import _matches_service_process, iter_matching_pids, Service
from ops import tmuxctl
from ops.control import _stop


class ProcessMatchingTests(unittest.TestCase):
    def test_module_entrypoint_and_role_are_exact(self):
        self.assertTrue(_matches_service_process(['/venv/bin/python', '-u', '-m', 'brain'], '-m brain'))
        self.assertTrue(_matches_service_process(['python3.10', '-W', 'ignore', '-m', 'brain'], '-m brain'))
        self.assertTrue(_matches_service_process(['python', '-m', 'execution.reception_sim', 'nav'], '-m execution.reception_sim nav'))
        for argv in [
            ['python', '-m', 'brain_other'],
            ['python', '-m', 'entries.web', '-m', 'brain'],
            ['python', '-c', 'print("-m brain")'],
            ['python', 'inspect.py', '-m', 'brain'],
            ['tmux', 'new-session', '/bin/bash', '-lc', "exec python -m brain"],
            ['ssh', 'host', 'python -m brain'],
            ['bash', '-lc', 'exec python -m brain'],
        ]:
            with self.subTest(argv=argv):
                self.assertFalse(_matches_service_process(argv, '-m brain'))
        self.assertFalse(_matches_service_process(['python', '-m', 'execution.reception_sim', 'vla'], '-m execution.reception_sim nav'))

    def test_script_and_redis_do_not_match_arguments_of_other_programs(self):
        script = 'simulation/backends/desk/main.py'
        self.assertTrue(_matches_service_process(['python3', '/workspace/' + script], script))
        self.assertTrue(_matches_service_process(['python3', '--', script], script))
        self.assertFalse(_matches_service_process(['python3', 'audit.py', script], script))
        self.assertFalse(_matches_service_process(['python3', script + '.backup'], script))
        self.assertTrue(_matches_service_process(['/usr/bin/redis-server', '*:6379'], 'redis-server'))
        self.assertTrue(_matches_service_process(['redis-server 127.0.0.1:6379'], 'redis-server'))
        self.assertFalse(_matches_service_process(['bash', '-lc', 'redis-server'], 'redis-server'))

    def test_proc_scan_excludes_launcher_and_its_command_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid, argv in [(11, ['python3', '-m', 'brain']),
                              (12, ['tmux', 'new-session', 'python3 -m brain']),
                              (13, ['python3', '-m', 'entries.web'])]:
                (root / str(pid)).mkdir()
                (root / str(pid) / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')
            with patch('ops.services.Path', side_effect=lambda value: root if value == '/proc' else Path(value)):
                self.assertEqual(iter_matching_pids('-m brain'), [11])

    def test_restart_one_session_keeps_other_entry_and_tmux_server_alive(self):
        if not tmuxctl.tmux_available():
            self.skipTest('tmux unavailable')
        socket = f'panel-isolation-{os.getpid()}'
        original_run = tmuxctl._run
        def isolated(args, **kwargs):
            return original_run([args[0], '-L', socket, *args[1:]], **kwargs)
        with tempfile.TemporaryDirectory() as tmp, patch.object(tmuxctl, '_run', side_effect=isolated):
            for name in ['panel_brain_fixture', 'panel_entry_fixture']:
                Path(tmp, name + '.py').write_text('import time\ntime.sleep(120)\n')
            brain = Service(id='fixture_brain', name='brain', layer='brain', controllable=True,
                            window='fixture_brain', match='-m panel_brain_fixture')
            entry = Service(id='fixture_entry', name='entry', layer='brain', controllable=True,
                            window='fixture_entry', match='-m panel_entry_fixture')
            def command(module):
                return f'export PYTHONPATH={shlex.quote(tmp)}; exec {shlex.quote(sys.executable)} -m {module}'
            try:
                tmuxctl.start_session(brain, command('panel_brain_fixture'))
                tmuxctl.wait_session(brain)
                tmuxctl.start_session(entry, command('panel_entry_fixture'))
                tmuxctl.wait_session(entry)
                entry_pid = tmuxctl.pane_pid(entry.window)
                _stop(brain)
                tmuxctl.start_session(brain, command('panel_brain_fixture'))
                tmuxctl.wait_session(brain)
                self.assertEqual(tmuxctl.pane_pid(entry.window), entry_pid)
                self.assertTrue(tmuxctl.session_alive(entry.window))
                self.assertTrue(tmuxctl.session_alive(brain.window))
            finally:
                subprocess.run(['tmux', '-L', socket, 'kill-server'], capture_output=True)
