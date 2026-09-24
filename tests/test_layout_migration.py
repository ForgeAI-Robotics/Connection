import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("layout_migration", ROOT / "scripts/migrate_layout_data.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class LayoutMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.target, self.backup = root / "old", root / "new", root / "backup"
        self.ledger = self.source / "master/sop/runtime/kernel/current_task.json"
        self.ledger.parent.mkdir(parents=True)

    def run_migration(self):
        return migration.migrate(self.source, self.target, self.backup, apply=True)

    def test_finished_ledger_history_and_evidence_are_byte_identical(self):
        record = {"task_id": "same-task", "state": "succeeded", "resources_cleared": True,
                  "steps": {"pick": {"attempts": [{"attempt_id": "a1", "command_id": "vla-pick-original"},
                                                {"attempt_id": "a2", "command_id": "vla-pick-original-r1"}]}}}
        raw = json.dumps(record, indent=2).encode()
        self.ledger.write_bytes(raw)
        evidence = self.ledger.parent / "evidence/a1.json"
        evidence.parent.mkdir()
        evidence.write_bytes(b'{"supports": false}\n')
        self.run_migration()
        self.assertEqual((self.target / "data/tasks/current_task.json").read_bytes(), raw)
        self.assertEqual((self.target / "data/tasks/evidence/a1.json").read_bytes(), evidence.read_bytes())
        self.assertEqual((self.backup / self.ledger.relative_to(self.source)).read_bytes(), raw)

    def test_unresolved_state_rejects_before_writing_anything(self):
        for state in ("paused", "waiting_human", "cancelling", "recovery_required", "RUNNING", "RECOVERY_REQUIRED"):
            self.ledger.write_text(json.dumps({"task_id": "original", "state": state}))
            with self.assertRaisesRegex(ValueError, "Unresolved task"):
                self.run_migration()
            self.assertFalse(self.target.exists())
            self.assertFalse(self.backup.exists())

    def test_terminal_label_cannot_hide_an_unresolved_command(self):
        self.ledger.write_text(json.dumps({"task_id": "original", "state": "failed", "open_command_id": "nav-original"}))
        with self.assertRaisesRegex(ValueError, "Unresolved command"):
            self.run_migration()

    def test_different_destination_is_not_overwritten(self):
        self.ledger.write_text(json.dumps({"task_id": "original", "state": "succeeded"}))
        destination = self.target / "data/tasks/current_task.json"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing data")
        with self.assertRaisesRegex(ValueError, "Destination differs"):
            self.run_migration()
        self.assertEqual(destination.read_bytes(), b"existing data")
        self.assertFalse(self.backup.exists())

    def test_config_changes_paths_without_enabling_motion_or_changing_endpoints(self):
        config = {"reception_real": {"kernel_runtime_dir": "./sop/runtime/kernel", "kernel_enabled": False,
                                    "dream_base_url": "http://192.0.2.10:8001", "vla_base_url": "http://192.0.2.11:8091"},
                  "model": {"key": "fixture-only"}, "scene": {"path": "./scene/profile.yaml"}}
        path = self.source / "master/config.yaml"
        path.write_text(yaml.safe_dump(config))
        self.run_migration()
        updated = yaml.safe_load((self.target / "config/brain.yaml").read_text())
        self.assertIs(updated["reception_real"]["kernel_enabled"], False)
        for key in ("dream_base_url", "vla_base_url"):
            self.assertEqual(updated["reception_real"][key], config["reception_real"][key])
        self.assertEqual(updated["model"], config["model"])
        self.assertEqual(updated["reception_real"]["kernel_runtime_dir"], "data/tasks")
        self.assertEqual(updated["scene"]["path"], "config/scene/profile.yaml")


class FinalLayoutTests(unittest.TestCase):
    def test_retired_source_trees_are_absent(self):
        for relative in ("src/connection", "src/compat", "master", "deploy", "web", "integrations", "agent", "common", "slaver", "robot_api", "serve_dream"):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_production_imports_do_not_depend_on_retired_modules(self):
        import ast
        retired = {"connection", "master", "deploy", "web", "integrations", "agent", "common", "slaver", "robot_api", "serve_dream", "compat"}
        for path in (ROOT / "src").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    self.assertNotIn(name.split(".")[0], retired, str(path))
