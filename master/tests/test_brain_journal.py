import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import log_setup
from brain_journal import (
    BrainJournal,
    OutboundCall,
    fingerprint,
    format_human,
    reset_journal_for_tests,
)


class BrainJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ[log_setup.LOG_ROOT_ENV] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, log_setup.LOG_ROOT_ENV, None)
        self.journal = BrainJournal(service="master")
        reset_journal_for_tests(self.journal)
        self.addCleanup(reset_journal_for_tests)

    def _brain_text(self):
        path = self.journal.paths()["brain"]
        return path.read_text(encoding="utf-8")

    def test_human_and_jsonl_are_written(self):
        self.journal.emit(
            "INBOUND",
            event="publish",
            source="deploy",
            text="开始接待",
            task_id="abc",
        )
        text = self._brain_text()
        self.assertIn("INBOUND", text)
        self.assertIn("开始接待", text)
        self.assertIn("source=deploy", text)
        lines = self.journal.paths()["jsonl"].read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[-1])
        self.assertEqual("INBOUND", record["kind"])
        self.assertEqual("abc", record["task_id"])

    def test_poll_updates_only_emit_on_fingerprint_change(self):
        call = OutboundCall(
            "dream", "navigate", command_id="nav-1", task_id="t1",
            target="table_2",
        )
        payload = {
            "state": "navigating",
            "command_id": "nav-1",
            "progress": {"message": "navigation executing"},
            "updated_at": "2026-09-18T10:31:42+08:00",
        }
        self.assertTrue(call.update(payload))
        payload["updated_at"] = "2026-09-18T10:32:00+08:00"
        self.assertFalse(call.update(payload))
        payload["state"] = "failed"
        payload["error"] = {"message": "unreachable"}
        self.assertTrue(call.update(payload))
        call.end(False, payload=payload, error="unreachable")
        kinds = [
            json.loads(line)["event"]
            for line in self.journal.paths()["jsonl"].read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(["start", "state", "state", "end"], kinds)

    def test_fingerprint_ignores_motor_noise(self):
        base = {
            "state": "navigating",
            "result": {"success": None},
            "updated_at": "a",
            "motor_health": {"motors": [1, 2, 3]},
        }
        other = dict(base)
        other["updated_at"] = "b"
        other["motor_health"] = {"motors": [9]}
        self.assertEqual(fingerprint(base), fingerprint(other))

    def test_failure_pins_and_new_task_clears(self):
        self.journal.emit("TASK", event="fail", task_id="old", error="timeout", ok=False)
        pin = self.journal.pin_text()
        self.assertIn("timeout", pin)
        self.journal.emit("TASK", event="start", task_id="new", type="reception")
        self.assertIsNone(self.journal.pin_text())

    def test_format_keeps_goal_and_path(self):
        line = format_human(
            "CALL",
            "start",
            {
                "peer": "dream",
                "cmd": "nav-table2",
                "target": "table_2",
                "goal": [0.99, 1.38, -0.39],
                "path_m": 2.86,
            },
            moment=datetime(2026, 9, 18, 13, 31, 42),
        )
        self.assertIn("cmd=nav-table2", line)
        self.assertIn("target=table_2", line)
        self.assertIn("path_m=2.86", line)
        self.assertIn("goal=(0.99,1.38,-0.39)", line)

    def test_step_keeps_order_before_phase(self):
        line = format_human(
            "STEP",
            "success",
            {"order": 2, "phase": "导航到table2", "detail": "DREAM真实终态succeeded", "ok": True},
            moment=datetime(2026, 9, 18, 13, 31, 42),
        )
        self.assertLess(line.index("order=2"), line.index("phase="))
        self.assertIn("ok=true", line)

    def test_in_progress_call_does_not_pin(self):
        call = OutboundCall(
            "dream", "navigate", command_id="nav-1", task_id="t1",
        )
        call.update({"state": "navigating", "result": {"success": False}})
        self.assertIsNone(self.journal.pin_text())
        last = self._brain_text().splitlines()[-1]
        self.assertIn("state=navigating", last)
        self.assertNotIn("ok=false", last)

    def test_inherit_task_false_keeps_previous_task_id_out(self):
        self.journal.set_current_task("task-old")
        self.journal.emit("INBOUND", event="preflight", text="开始接待",
                          inherit_task=False)
        self.journal.emit("PREFLIGHT", event="http", text="开始接待", ready=True,
                          inherit_task=False)
        self.journal.emit("INBOUND", event="publish", text="开始接待",
                          id="task-new", inherit_task=False)
        records = [
            json.loads(line)
            for line in self.journal.paths()["jsonl"].read_text(encoding="utf-8").splitlines()
        ]
        self.assertIsNone(records[0]["task_id"])
        self.assertIsNone(records[1]["task_id"])
        self.assertEqual("task-new", records[2]["task_id"])
        self.assertNotIn("task-old", self._brain_text())

    def test_running_task_still_carries_its_id(self):
        self.journal.set_current_task("task-run")
        self.journal.emit("STEP", event="success", order=1, phase="世界与服务预检")
        record = json.loads(
            self.journal.paths()["jsonl"].read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual("task-run", record["task_id"])

    def test_inbound_headers(self):
        from types import SimpleNamespace
        from brain_journal import inbound_from_flask

        req = SimpleNamespace(
            headers={
                "X-FQ-Source": "feishu",
                "X-FQ-Client": "bridge",
                "X-FQ-Operator": "ou_1",
                "X-FQ-Via": "deploy",
            },
            remote_addr="10.0.0.8",
        )
        fields = inbound_from_flask(req)
        self.assertEqual("feishu", fields["source"])
        self.assertEqual("bridge", fields["from"])
        self.assertEqual("ou_1", fields["operator"])
        self.assertEqual("deploy", fields["via"])


if __name__ == "__main__":
    unittest.main()
