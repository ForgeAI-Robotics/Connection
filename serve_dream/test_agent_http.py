"""Offline contract tests for DREAM Agent HTTP v1.  Never contacts the robot."""

import json
import math
import os
import sys
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from adapters import agent_http, rosmap, scene_graph  # noqa: E402
import reviewed_targets  # noqa: E402


class FakeDreamHandler(BaseHTTPRequestHandler):
    commands = {}

    def log_message(self, *_args):
        pass

    def _json(self, status, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/v1/world":
            return self._json(200, {"frame_id": "map", "map_yaml_url": "/map.yaml"})
        if self.path == "/v1/status":
            return self._json(200, {"localization_approved": True,
                                    "navigation_transport_ready": True,
                                    "robot_xyt": [1.0, 2.0, math.pi / 2]})
        if self.path == "/v1/camera/status":
            return self._json(200, {"rgb": {"available": True, "age_sec": 0.2}})
        if self.path.startswith("/v1/commands/"):
            command_id = self.path.rsplit("/", 1)[-1]
            record = self.commands.get(command_id)
            if not record:
                return self._json(404, {"error": "missing"})
            record["polls"] += 1
            if record["kind"] == "failed":
                state = "failed"
                result = {"success": False, "reason": "9882 rejected"}
            elif record["polls"] == 1:
                state, result = "waiting_safety_ready", {}
            elif record["polls"] == 2:
                state, result = "navigating", {}
            elif record["kind"] == "inspection":
                state = "succeeded"
                result = {"relation_graph_url": "/total_scene_graph_latest.json",
                          "six_d_sync_active": True}
            else:
                state = "succeeded"
                result = {"success": True, "reached": True,
                          "navigation_stopped": True,
                          "accepted_pose": record["goal"]}
            return self._json(200, {"command_id": command_id, "state": state,
                                    "result": result})
        return self._json(404, {"error": "unknown"})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        if self.path == "/v1/navigation/goals":
            command_id = body["command_id"]
            self.commands[command_id] = {
                "polls": 0,
                "kind": "failed" if body.get("target_id") == "reject_me" else "navigation",
                "goal": body["goal_xyt"],
            }
            return self._json(202, {"command_id": command_id, "state": "accepted"})
        if self.path == "/v1/inspection":
            command_id = body["command_id"]
            self.commands[command_id] = {"polls": 0, "kind": "inspection", "goal": []}
            return self._json(202, {"command_id": command_id, "state": "accepted"})
        return self._json(404, {"error": "unknown"})


class AgentHttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDreamHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = agent_http.AgentHttpClient(
            f"http://127.0.0.1:{cls.server.server_port}", request_timeout_sec=2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_payload_converts_degrees_to_radians(self):
        payload = agent_http.build_navigation_payload(
            3.0, 4.0, 180, command_id="nav-yaw", task_id="task-yaw",
            target_id="table_1")
        self.assertEqual(payload["frame_id"], "map")
        self.assertAlmostEqual(payload["goal_xyt"][2], math.pi)

    def test_natural_language_table2_resolves_reviewed_contract(self):
        spec = reviewed_targets.resolve_reviewed_target("导航到 table2")
        self.assertIsNotNone(spec)
        target = reviewed_targets.as_robot_api_target(spec)
        self.assertEqual(target["target_id"], "table_2")
        self.assertEqual(target["motion_mode"], "forward_path")
        self.assertEqual(target["yaw_unit"], "radians")
        self.assertAlmostEqual(target["x"], 1.0)
        self.assertAlmostEqual(target["y"], 2.0)
        self.assertAlmostEqual(target["yaw"], 0.0)

    def test_accepted_is_polled_until_succeeded(self):
        payload = agent_http.build_navigation_payload(
            1.0, 2.0, math.pi / 2, yaw_unit="radians",
            command_id="nav-ok", task_id="task-ok", target_id="table_2")
        result = self.client.navigate(payload, timeout_sec=3, poll_interval_sec=0.01)
        self.assertTrue(result["success"])
        self.assertEqual(result["state"], "succeeded")
        self.assertAlmostEqual(result["yaw"], 90.0)

    def test_failed_terminal_is_not_success(self):
        payload = agent_http.build_navigation_payload(
            1.0, 2.0, 0.0, yaw_unit="radians",
            command_id="nav-fail", task_id="task-fail", target_id="reject_me")
        result = self.client.navigate(payload, timeout_sec=2, poll_interval_sec=0.01)
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "failed")

    def test_inspection_waits_for_terminal(self):
        result = self.client.inspect({
            "command_id": "inspect-ok",
            "navigation_command_id": "nav-ok",
            "target_object_id": "table_2",
        }, timeout_sec=3, poll_interval_sec=0.01)
        self.assertTrue(result["success"])
        self.assertTrue(result["six_d_sync_active"])

    def test_synthetic_map_and_graph(self):
        from selftest import make_pgm
        data_dir = os.path.join(_HERE, "sample")
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(os.path.join(data_dir, "map.yaml"), tmp)
            make_pgm(os.path.join(tmp, "map.pgm"))
            map_data = rosmap.load_map_data(os.path.join(tmp, "map.yaml"))
        self.assertEqual((map_data["width"], map_data["height"]), (40, 30))
        self.assertAlmostEqual(map_data["resolution"], 0.025)
        graph = scene_graph.load_graph(os.path.join(data_dir, "total_scene_graph_latest.json"))
        self.assertIn("table_1", scene_graph.graph_fixtures(graph))
        self.assertIn("fridge_1", scene_graph.graph_fixtures(graph))
        self.assertIsNone(scene_graph.door_navigation_contract(graph))

    def test_server_rejects_real_action_by_default(self):
        from service.server import app
        resolved = app.test_client().get("/reviewed_target/table2")
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.get_json()["motion_mode"], "forward_path")
        response = app.test_client().post(
            "/nav", json={"x": 1.0, "y": 2.0, "target_yaw": 90.0})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.get_json()["real_control_authorized"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
