"""Verify relocation against pre-migration artifacts, not regenerated expectations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

FIXTURES = Path(__file__).parent / "contracts"


class PackageCompatibilityTests(unittest.TestCase):
    def test_frozen_reception_wire_bodies(self):
        from brain.packages import reception as r
        from contracts.tasks import make_command_id
        specs = {step.step_id: step for step in r.PHASES}
        for sample in json.loads((FIXTURES / "reception_wire.json").read_text()):
            step = specs[sample["step_id"]]
            command = make_command_id(step.prefix, "baseline0001", 1)
            if step.kind == "navigate":
                body = r.navigation_body(step, "baseline0001", command)
            elif step.kind == "inspect":
                body = r.inspection_body("baseline0001", command, "nav-proof-baseline0001")
            else:
                body = r.manipulation_body(step, "baseline0001", command, "nav-proof-baseline0001")
            self.assertEqual(body, sample["body"])

    def test_pre_migration_ledger_keeps_identity_and_history(self):
        from brain.app import create_runtime
        from brain.adapters.execution import LookAdapter
        raw = (FIXTURES / "open_look_ledger.json").read_text()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "current_task.json"
            path.write_text(raw)
            runtime = create_runtime({"reception_real": {"kernel_runtime_dir": root}}, LookAdapter())
            self.assertEqual(runtime.record["task_id"], "baseline0001")
            self.assertEqual(runtime.record["steps"], json.loads(raw)["steps"])
            self.assertEqual(path.read_text(), raw)

    def test_imports_from_unrelated_cwd_do_not_start_resources(self):
        code = '''
import os, socket, threading
from unittest.mock import patch
with patch.object(socket.socket, 'connect', side_effect=AssertionError('network')), \\
     patch.object(threading.Thread, 'start', side_effect=AssertionError('thread')), \\
     patch.object(os, 'chdir', side_effect=AssertionError('cwd')):
    import brain.app
    import brain.kernel.runtime
    import brain.adapters.execution
    import brain.learning.reflection
'''
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

class ConfigurationTests(unittest.TestCase):
    def test_environment_placeholder_survives_yaml_special_characters(self):
        from shared.config import load_config
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.yaml'
            path.write_text('model: {cloud_api_key: "${TEST_CONNECTION_KEY}"}\n')
            with patch.dict(os.environ, {'TEST_CONNECTION_KEY': 'test: value # literal'}):
                self.assertEqual(load_config(path)['model']['cloud_api_key'], 'test: value # literal')
