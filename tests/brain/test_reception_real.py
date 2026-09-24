import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


from pathlib import Path
ROOT = str(Path(__file__).resolve().parents[2])

from brain.adapters.dream_client import DreamClient
from brain.adapters.http_client import HttpContractError
from brain.adapters.reception_verify import ReceptionVerifier
from brain.adapters.vla_client import VlaClient, VlaRecoveryRequired


JPEG = b"\xff\xd8fake-reception-frame\xff\xd9"
EVENTS = []


class _JsonHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _json(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeDreamHandler(_JsonHandler):
    commands = {}
    motion_ready = True
    motion_blockers = []

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "interface_online": True})
        if self.path == "/v1/status":
            return self._json(200, {
                "interface_online": True,
                "world_state_available": True,
                "localization_approved": True,
                "navigation_transport_ready": True,
                "motion_ready": self.motion_ready,
                "motion_blockers": list(self.motion_blockers),
                "active_command_id": None,
            })
        if self.path == "/v1/world":
            return self._json(200, {
                "frame_id": "map",
                "relation_graph_url": "/total_scene_graph_latest.json",
            })
        if self.path == "/v1/camera/status":
            return self._json(200, {
                "driver_enabled": False,
                "external_owner": "vla",
            })
        if self.path == "/total_scene_graph_latest.json":
            return self._json(200, {
                "contract_version": "fq/reception-lan/v1",
                "frame_id": "map",
                "objects": [
                    {"id": "table_2"}, {"id": "door_1"}, {"id": "table_1"}
                ],
                "agent_navigation_contract": {
                    "frame_id": "map",
                    "legs": [
                        {
                            "target_id": "table_2", "route_phase": "", "leg_index": 1,
                            "goal_xyt": [0.9903405869861586, 1.3761315438191244, -0.39236607751253016],
                            "motion_mode": "forward_path", "require_final_orientation": True,
                        },
                        {
                            "target_id": "door_1", "route_phase": "door_approach", "leg_index": 2,
                            "goal_xyt": [3.733075988421528, 6.215369909530748, 2.718279944258407],
                            "motion_mode": "forward_path", "require_final_orientation": True,
                        },
                        {
                            "target_id": "door_1", "route_phase": "door_lateral_exit", "leg_index": 3,
                            "goal_xyt": [4.185939449618811, 7.560143924693016, 2.7689146673931306],
                            "motion_mode": "lateral_path_aligned", "require_final_orientation": True,
                        },
                        {
                            "target_id": "table_1", "route_phase": "table1_approach", "leg_index": 4,
                            "goal_xyt": [3.0873798986272165, 8.279995338440145, 1.175238157458919],
                            "motion_mode": "forward_path", "require_final_orientation": True,
                        },
                    ],
                },
                "nodes": {
                    "door_1": {
                        "object_id": "door_1",
                        "evidence": {
                            "navigation_contract": {
                                "door_approach": {
                                    "goal_xyt": [3.733075988421528, 6.215369909530748, 2.718279944258407],
                                    "motion_mode": "forward_path",
                                    "require_final_orientation": True,
                                },
                                "door_lateral_exit": {
                                    "goal_xyt": [4.185939449618811, 7.560143924693016, 2.7689146673931306],
                                    "motion_mode": "lateral_path_aligned",
                                    "require_final_orientation": True,
                                },
                            }
                        },
                    }
                },
            })
        if self.path.startswith("/v1/commands/"):
            command_id = self.path.rsplit("/", 1)[-1]
            return self._json(200, self.commands[command_id])
        return self._json(404, {"error": {"code": "NOT_FOUND"}})

    def do_POST(self):
        if self.path == "/v1/inspection":
            body = self._body()
            command_id = body["command_id"]
            EVENTS.append(("dream", "inspection", body["target_object_id"]))
            self.commands[command_id] = {
                "task_id": body["task_id"],
                "command_id": command_id,
                "kind": "inspection",
                "state": "succeeded",
                "completed_at": "2026-08-27T14:00:01.000+08:00",
                "result": {"success": True},
            }
            return self._json(202, {
                "accepted": True,
                "command_id": command_id,
                "state": "accepted",
            })
        if self.path != "/v1/navigation/goals":
            return self._json(404, {"error": {"code": "NOT_FOUND"}})
        body = self._body()
        command_id = body["command_id"]
        EVENTS.append(("dream", body["target_id"], body.get("route_phase") or ""))
        self.commands[command_id] = {
            "task_id": body["task_id"],
            "command_id": command_id,
            "target_id": body["target_id"],
            "route_phase": body.get("route_phase") or "",
            "leg_index": body["leg_index"],
            "state": "succeeded",
            "completed_at": "2026-08-27T14:00:00.000+08:00",
            "result": {
                "success": True,
                "reached": True,
                "navigation_stopped": True,
            },
        }
        return self._json(202, {
            "accepted": True,
            "command_id": command_id,
            "state": "accepted",
        })


class FakeVlaHandler(_JsonHandler):
    tasks = {}
    snapshots = {}
    holding = None

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok"})
        if self.path == "/v1/vla/control/status":
            return self._json(200, {
                "service_ready": True,
                "policy_running": False,
                "active_command_id": None,
                "recovery_required": False,
                "holding": self.holding,
                "action_port": {"navigation_port_ready": True},
                "camera": {"owner": "vla", "ready": True, "streaming": True},
            })
        if self.path == "/v1/camera/status":
            return self._json(200, {
                "owner": "vla", "ready": True, "streaming": True,
                "latest_frame_id": "frame-1",
                "latest_frame_at": "2026-08-27T14:00:00.000+08:00",
            })
        if self.path.startswith("/v1/vla/tasks/"):
            command_id = self.path.rsplit("/", 1)[-1]
            return self._json(200, self.tasks[command_id])
        if self.path.startswith("/v1/camera/snapshots/") and self.path.endswith("/rgb"):
            snapshot_id = self.path.split("/")[-2]
            if snapshot_id not in self.snapshots:
                return self._json(404, {"error": {"code": "NOT_FOUND"}})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("X-Snapshot-Id", snapshot_id)
            self.send_header(
                "X-Captured-At",
                "2026-08-27T14:01:01.000+08:00"
                if snapshot_id == "snap-verify_pick"
                else "2026-08-27T14:05:01.000+08:00",
            )
            self.send_header("X-Content-SHA256", hashlib.sha256(JPEG).hexdigest())
            self.send_header("Content-Length", str(len(JPEG)))
            self.end_headers()
            self.wfile.write(JPEG)
            return
        return self._json(404, {"error": {"code": "NOT_FOUND"}})

    def do_POST(self):
        body = self._body()
        if self.path == "/v1/vla/tasks":
            operation = body["operation"]
            command_id = body["command_id"]
            EVENTS.append(("vla", operation, body["target_area"]))
            completed_at = (
                "2026-08-27T14:01:00.000+08:00"
                if operation == "pick"
                else "2026-08-27T14:05:00.000+08:00"
            )
            result = {
                "success": True,
                "object_id": "cola_can_1",
                "object_grasped": operation == "pick",
                "holding": "cola_can_1" if operation == "pick" else None,
                "released": operation == "place",
                "object_at_target": operation == "place",
                "policy_stopped": True,
                "navigation_port_ready": True,
                "evidence_level": "hand_state_only",
                "evidence": (
                    {
                        "hand": "left",
                        "hand_closed_confirmed": True,
                        "close_score": 0.74,
                        "confirmed_frames": 3,
                        "object_presence_verified": False,
                    }
                    if operation == "pick"
                    else {
                        "hand": "left",
                        "hand_open_confirmed": True,
                        "confirmed_frames": 3,
                        "object_at_target_verified": False,
                    }
                ),
            }
            self.tasks[command_id] = {
                "contract_version": "fq/reception-lan/v1",
                "task_id": body["task_id"],
                "command_id": command_id,
                "operation": operation,
                "state": "succeeded",
                "completed_at": completed_at,
                "result": result,
            }
            return self._json(202, {
                "accepted": True, "command_id": command_id, "state": "accepted"
            })
        if self.path == "/v1/camera/snapshots":
            purpose = body["purpose"]
            EVENTS.append(("snapshot", purpose, body["source_command_id"]))
            snapshot_id = f"snap-{purpose}"
            captured_at = (
                "2026-08-27T14:01:01.000+08:00"
                if purpose == "verify_pick"
                else "2026-08-27T14:05:01.000+08:00"
            )
            self.snapshots[snapshot_id] = True
            return self._json(201, {
                "snapshot_id": snapshot_id,
                "captured_at": captured_at,
                "content_type": "image/jpeg",
                "sha256": hashlib.sha256(JPEG).hexdigest(),
                "rgb_url": f"/v1/camera/snapshots/{snapshot_id}/rgb",
            })
        return self._json(404, {"error": {"code": "NOT_FOUND"}})


def fake_chat_completion(endpoint_config, messages, *, max_tokens):
    del max_tokens
    if endpoint_config.get("kind") == "vlm":
        prompt = messages[0]["content"][0]["text"]
        if "抓取结果" in prompt:
            return json.dumps({
                "target_visible": True,
                "target_in_gripper": True,
                "confidence": 0.99,
                "reason": "目标在夹爪中",
            }, ensure_ascii=False)
        return json.dumps({
            "target_visible": True,
            "target_on_table1": True,
            "target_in_gripper": False,
            "confidence": 0.99,
            "reason": "目标位于table1",
        }, ensure_ascii=False)
    prompt = messages[0]["content"]
    operation = "pick" if "确实holding目标" in prompt else "place"
    target_id = "table_2" if operation == "pick" else "table_1"
    EVENTS.append(("verify", operation, target_id))
    return json.dumps({
        "verified": True,
        "operation": operation,
        "object_id": "cola_can_1",
        "target_id": target_id,
        "reason": "VLA和VLM证据一致",
    }, ensure_ascii=False)


class RealClientContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dream_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDreamHandler)
        cls.vla_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeVlaHandler)
        cls.threads = [
            threading.Thread(target=cls.dream_server.serve_forever, daemon=True),
            threading.Thread(target=cls.vla_server.serve_forever, daemon=True),
        ]
        for thread in cls.threads:
            thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.dream_server, cls.vla_server):
            server.shutdown()
            server.server_close()

    def setUp(self):
        EVENTS.clear()
        FakeDreamHandler.commands.clear()
        FakeDreamHandler.motion_ready = True
        FakeDreamHandler.motion_blockers = []
        FakeVlaHandler.tasks.clear()
        FakeVlaHandler.snapshots.clear()
        FakeVlaHandler.holding = None
        self._reflection_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._reflection_tmp.cleanup)
        self._old_reflection_dir = os.environ.get("FQPLANNER_REFLECTION_DIR")
        self._old_reflection_llm = os.environ.get("FQPLANNER_REFLECTION_LLM")
        os.environ["FQPLANNER_REFLECTION_DIR"] = self._reflection_tmp.name
        os.environ["FQPLANNER_REFLECTION_LLM"] = "off"
        def _restore_reflection_env():
            if self._old_reflection_dir is None:
                os.environ.pop("FQPLANNER_REFLECTION_DIR", None)
            else:
                os.environ["FQPLANNER_REFLECTION_DIR"] = self._old_reflection_dir
            if self._old_reflection_llm is None:
                os.environ.pop("FQPLANNER_REFLECTION_LLM", None)
            else:
                os.environ["FQPLANNER_REFLECTION_LLM"] = self._old_reflection_llm
        self.addCleanup(_restore_reflection_env)







    def test_vla_uncertain_post_never_creates_a_new_command(self):
        class UncertainHttp:
            def request_json(self, method, path, payload=None, **_kwargs):
                if method == "POST":
                    raise HttpContractError("connection lost")
                raise HttpContractError("missing", status_code=404)

        client = VlaClient("http://127.0.0.1:1")
        client.http = UncertainHttp()
        payload = {
            "command_id": "vla-pick-fixed-id",
            "task_id": "task-fixed-id",
            "operation": "pick",
        }
        with self.assertRaises(VlaRecoveryRequired) as raised:
            client.submit_task(
                payload, recovery_attempts=2, recovery_interval_sec=0.0)
        self.assertEqual(
            "vla-pick-fixed-id", raised.exception.payload["command_id"])












if __name__ == "__main__":
    unittest.main(verbosity=2)
