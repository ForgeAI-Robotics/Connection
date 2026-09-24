import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import yaml
from ops.execution import Switcher


class Services:
    def __init__(self):
        self.up = {'master', 'deploy', 'feishu', 'desk'}
        self.calls = []
    def running(self, name): return name in self.up
    def status(self): return {'active': False}
    def start(self, name): self.calls.append(('start', name)); self.up.add(name)
    def stop(self, name): self.calls.append(('stop', name)); self.up.discard(name)
    def healthy(self, names, **kwargs): pass
    def targets_healthy(self, routes): pass


class OpsTests(unittest.TestCase):
    def test_desk_switch_needs_no_redis_and_preserves_both_entries(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'config').mkdir()
            (root / 'config/brain.yaml').write_text('brain: {scheduler: runtime}\nreception_real: {kernel_enabled: false}\n')
            (root / 'config/robot_api.yaml').write_text(yaml.safe_dump({'backends': {
                'desk': {'enabled': True, 'url': 'http://localhost:1234', 'provide_state': True, 'accept_action': True}}}))
            services = Services()
            Switcher(root, services).apply({'mode': 'simulation', 'simulation_backend': 'desk'})
            self.assertEqual(services.calls, [('stop', 'master'), ('start', 'master')])
            self.assertNotIn('redis', services.up)

    def test_cli_process_manager_imports_no_ui(self):
        result = subprocess.run([sys.executable, '-c', '''
import sys
from ops.execution import LocalServices
LocalServices()
assert 'ops.app' not in sys.modules
assert 'flask' not in sys.modules
assert 'ops.app' not in sys.modules
'''], cwd='/tmp', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
