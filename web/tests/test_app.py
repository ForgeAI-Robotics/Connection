import unittest

from web.app import app


class PanelAppTests(unittest.TestCase):
    def setUp(self):
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

    def test_unknown_service_and_action(self):
        self.assertEqual(
            self.client.post("/api/services/nope/start").status_code, 404
        )
        self.assertEqual(
            self.client.post("/api/services/redis/explode").status_code, 400
        )

    def test_robot_cannot_be_started(self):
        response = self.client.post("/api/services/dream/start")
        self.assertEqual(response.status_code, 400)
        self.assertIn("只能监控", response.get_json()["error"])

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
