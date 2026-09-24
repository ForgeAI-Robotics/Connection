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

from shared import log_setup


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


    def test_monitor_log_is_under_the_day_service_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            os.environ[log_setup.LOG_ROOT_ENV] = raw
            self.addCleanup(os.environ.pop, log_setup.LOG_ROOT_ENV, None)
            stamp = datetime(2026, 9, 17, 14, 40, 0)
            path = log_setup.monitor_log_path("vla", now=stamp)
            self.assertEqual(Path(raw) / "2026-09-17" / "vla" / "monitor.log", path)
            written = log_setup.append_monitor_log("vla", "probe ok")
            self.assertEqual("monitor.log", written.name)
            self.assertEqual("vla", written.parent.name)
            self.assertIn("probe ok", written.read_text(encoding="utf-8"))

    def test_inbound_task_request_prints_task_text(self):
        captured = StringIO()
        with mock.patch("sys.stdout", captured):
            log_setup.note_task_request(
                "preflight",
                "开始接待",
                ready=False,
                required=True,
                blockers=["DREAM 8001 Connection refused"],
            )
        text = captured.getvalue()
        self.assertIn("[task] preflight 开始接待", text)
        self.assertIn("ready=false", text)
        self.assertIn("DREAM 8001 Connection refused", text)


if __name__ == "__main__":
    unittest.main()
