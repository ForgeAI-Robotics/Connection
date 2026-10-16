"""Fresh-checkout configuration must preserve machine-owned files on rerun."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from shared.networks import load_lan


ROOT = Path(__file__).resolve().parents[1]


class ConfigTemplateTests(unittest.TestCase):
    def test_bootstrap_creates_network_config_and_preserves_local_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/bootstrap_local.py', root / 'scripts/bootstrap_local.py')
            shutil.copytree(ROOT / 'config/examples', root / 'config/examples')
            command = [sys.executable, str(root / 'scripts/bootstrap_local.py')]
            subprocess.run(command, cwd='/tmp', check=True, capture_output=True)
            path = root / 'config/networks.yaml'
            self.assertEqual(load_lan(path).http('brain'), 'http://192.0.2.10:5000')
            local = yaml.safe_load(path.read_text())
            local['roles']['brain']['ssh_user'] = 'operator'
            local['wifis']['example_office']['brain'] = '127.0.0.2'
            path.write_text('# keep operator comments\n' + yaml.safe_dump(local))
            (root / '.env').write_text('CLOUD_API_KEY=local-test-value\n')
            state = root / 'data/tasks/current_task.json'
            state.parent.mkdir(parents=True)
            state.write_text('{"state":"paused","command_unknown":true}')
            files = [*root.glob('config/*.yaml'), root / '.env', state]
            before = {p: p.read_bytes() for p in files}
            subprocess.run(command, cwd='/tmp', check=True, capture_output=True)
            self.assertEqual({p: p.read_bytes() for p in files}, before)
            self.assertEqual(load_lan(path).ssh_target('brain'), 'operator@127.0.0.2')

    def test_public_yaml_has_no_duplicate_keys(self):
        class UniqueLoader(yaml.SafeLoader):
            def construct_mapping(self, node, deep=False):
                keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
                if len(keys) != len(set(keys)):
                    raise ValueError(f'duplicate key at {node.start_mark}')
                return super().construct_mapping(node, deep=deep)

        for path in sorted((ROOT / 'config/examples').glob('*.yaml')):
            with self.subTest(path=path.name), path.open() as source:
                yaml.load(source, Loader=UniqueLoader)
