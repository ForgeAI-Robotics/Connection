import os
import sys
import tempfile
import unittest
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MASTER = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for path in (ROOT, MASTER):
    if path not in sys.path:
        sys.path.insert(0, path)

import log_setup


class ProcessLogLayoutTest(unittest.TestCase):
    def test_path_is_day_then_service_then_start_time(self):
        with tempfile.TemporaryDirectory() as raw:
            os.environ[log_setup.LOG_ROOT_ENV] = raw
            self.addCleanup(os.environ.pop, log_setup.LOG_ROOT_ENV, None)
            stamp = datetime(2026, 9, 16, 11, 43, 5)
            path = log_setup.create_process_log_path("master", now=stamp)
            self.assertEqual(
                Path(raw) / "2026-09-16" / "master" / "11-43-05.log",
                path,
            )
            self.assertTrue(path.parent.is_dir())

    def test_rejects_path_service_names(self):
        with tempfile.TemporaryDirectory() as raw:
            os.environ[log_setup.LOG_ROOT_ENV] = raw
            self.addCleanup(os.environ.pop, log_setup.LOG_ROOT_ENV, None)
            with self.assertRaises(ValueError):
                log_setup.create_process_log_path("../master")

    def test_reception_archive_is_under_the_day_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            os.environ[log_setup.LOG_ROOT_ENV] = raw
            self.addCleanup(os.environ.pop, log_setup.LOG_ROOT_ENV, None)
            stamp = datetime(2026, 9, 16, 12, 54, 0)
            path = log_setup.reception_runtime_dir(now=stamp)
            self.assertEqual(Path(raw) / "2026-09-16" / "reception", path)

    def test_reception_store_mirrors_into_process_log(self):
        from sop.reception_store import ReceptionStore

        with tempfile.TemporaryDirectory() as raw:
            process_log = Path(raw) / "master.log"
            os.environ[log_setup.PROCESS_LOG_ENV] = str(process_log)
            self.addCleanup(os.environ.pop, log_setup.PROCESS_LOG_ENV, None)
            store = ReceptionStore(Path(raw) / "archive")
            captured = StringIO()
            with mock.patch("sys.stdout", captured):
                store.save_state({
                    "task_id": "abc",
                    "state": "FAILED",
                    "runtime_phase": "FAILED",
                    "failure_reason": "DREAM定位尚未Approve",
                })
                store.append_event(
                    "TASK_FAILED", task_id="abc", error="DREAM定位尚未Approve")
            text = captured.getvalue()
            self.assertIn("[reception] task", text)
            self.assertIn("TASK_FAILED", text)
            self.assertIn("DREAM定位尚未Approve", text)
            self.assertTrue((Path(raw) / "archive" / "current_task.json").exists())
            self.assertTrue((Path(raw) / "archive" / "events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
