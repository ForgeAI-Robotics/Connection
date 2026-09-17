import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from web.app import app
from web import services


class PanelAppTests(unittest.TestCase):
    def setUp(self):
        self.logdir = tempfile.TemporaryDirectory()
        os.environ["FQPLANNER_LOG_ROOT"] = self.logdir.name
        self.addCleanup(self.logdir.cleanup)
        self.addCleanup(os.environ.pop, "FQPLANNER_LOG_ROOT", None)
        services.PROBE_HISTORY.clear()
        services._LAST_MONITOR.clear()
        self.client = app.test_client()

    def test_status_lists_layers(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["tmux"] in (True, False))
        self.assertEqual(
            [item["id"] for item in payload["brain"]],
            ["redis", "master", "deploy", "feishu", "slaver", "desk", "mujoco"],
        )
        self.assertEqual([item["id"] for item in payload["robot"]], ["dream", "vla"])
        self.assertTrue(all(item["controllable"] for item in payload["brain"]))
        self.assertTrue(all(not item["controllable"] for item in payload["robot"]))
        robot = {item["id"]: item for item in payload["robot"]}
        self.assertEqual(robot["vla"]["remote_actions"], ["start", "stop"])
        self.assertEqual(
            robot["dream"]["remote_actions"],
            ["start", "stand-enter", "stop"],
        )
        self.assertIn("stand-enter", robot["dream"]["action_confirms"])
        self.assertIn(":9882", robot["dream"]["review_url"])
        self.assertIn(robot["dream"]["review_ok"], (True, False))
        self.assertNotIn("review_url", robot["vla"])
        self.assertFalse(robot["dream"]["disabled_action"])

    def test_unknown_service_and_action(self):
        self.assertEqual(
            self.client.post("/api/services/nope/start").status_code, 404
        )
        self.assertEqual(
            self.client.post("/api/services/redis/explode").status_code, 400
        )

    def test_dream_start_uses_ssh_and_does_not_touch_tmux(self):
        from unittest.mock import patch

        with patch("web.app.dream_remote.run", return_value="started") as mocked:
            response = self.client.post("/api/services/dream/start")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["ok"])
        mocked.assert_called_once_with("start")

    def test_vla_start_uses_ssh_and_does_not_touch_tmux(self):
        from unittest.mock import patch

        with patch("web.app.vla_remote.run", return_value="started") as mocked:
            response = self.client.post("/api/services/vla/start")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["ok"])
        mocked.assert_called_once_with("start")

    def test_status_health_is_not_task_json(self):
        payload = self.client.get("/api/status").get_json()
        by_id = {item["id"]: item for item in payload["brain"]}
        for service_id in ("master", "deploy"):
            detail = (by_id[service_id].get("health") or {}).get("detail") or ""
            self.assertNotIn("subtask_list", detail)
            self.assertNotIn("\\u", detail)

    def test_source_does_not_publish_tasks(self):
        from pathlib import Path

        text = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("publish_task", text)

    def test_unit_file_kills_only_the_panel_process(self):
        text = Path(__file__).resolve().parents[1].joinpath(
            "fqplanner-panel.service"
        ).read_text(encoding="utf-8")
        self.assertIn("KillMode=process", text)

    def test_logs_are_file_only_and_return_path(self):
        day = Path(self.logdir.name) / datetime.now().strftime("%Y-%m-%d") / "master"
        day.mkdir(parents=True)
        (day / "12-00-00.log").write_text("hello from file\n", encoding="utf-8")
        response = self.client.get("/api/services/master/logs")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["id"], "master")
        self.assertEqual(payload["text"], "hello from file")
        self.assertTrue(payload["path"].endswith("master/12-00-00.log"))
        self.assertNotIn("tmux attach", payload["text"])
        self.assertNotIn("HTTP 探测", payload["text"])
