import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SlaverConfigTests(unittest.TestCase):
    def check_workspace_config(self, override=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('CONNECTION_TEST_CONFIG_KEY=workspace-value\n')
            (root / 'slaver.yaml').write_text('key: ${CONNECTION_TEST_CONFIG_KEY}\n')
            outside = root / 'outside'
            outside.mkdir()
            env = dict(os.environ, CONNECTION_WORKSPACE=str(root))
            env.pop('CONNECTION_TEST_CONFIG_KEY', None)
            if override is not None:
                env['CONNECTION_TEST_CONFIG_KEY'] = override
            result = subprocess.run(
                [sys.executable, '-c',
                 'from execution.slaver.tools.utils import Config; '
                 'import sys; print(Config.load_config(sys.argv[1])["key"])',
                 str(root / 'slaver.yaml')],
                cwd=outside, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), override or 'workspace-value')

    def test_loads_workspace_dotenv_outside_checkout(self):
        self.check_workspace_config()

    def test_existing_environment_takes_precedence(self):
        self.check_workspace_config('explicit-value')
