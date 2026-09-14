import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MASTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if MASTER_DIR not in sys.path:
    sys.path.insert(0, MASTER_DIR)

from integrations.dream_client import DreamClient
from integrations.http_client import HttpContractError
from integrations.reception_verify import ReceptionVerifier
from integrations.vla_client import VlaClient, VlaRecoveryRequired
from sop.reception_real import ReceptionRealRunner, check_reception_real_preflight
from sop.reception_store import ReceptionStore


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
                            "goal_xyt": [1.0, 2.0, 0.0],
                            "motion_mode": "forward_path", "require_final_orientation": True,
                        },
                        {
                            "target_id": "door_1", "route_phase": "door_approach", "leg_index": 2,
                            "goal_xyt": [3.0, 2.0, 1.5707963267948966],
                            "motion_mode": "forward_path", "require_final_orientation": True,
                        },
                        {
                            "target_id": "door_1", "route_phase": "door_lateral_exit", "leg_index": 3,
                            "goal_xyt": [3.0, 3.0, 1.5707963267948966],
                            "motion_mode": "lateral_path_aligned", "require_final_orientation": True,
                        },
                        {
                            "target_id": "table_1", "route_phase": "table1_approach", "leg_index": 4,
                            "goal_xyt": [4.0, 4.0, 0.0],
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
                                    "goal_xyt": [3.0, 2.0, 1.5707963267948966],
                                    "motion_mode": "forward_path",
                                    "require_final_orientation": True,
                                },
                                "door_lateral_exit": {
                                    "goal_xyt": [3.0, 3.0, 1.5707963267948966],
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


class ReceptionRealPipelineTest(unittest.TestCase):
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

    def _preflight_report(self):
        dream_url = f"http://127.0.0.1:{self.dream_server.server_port}"
        vla_url = f"http://127.0.0.1:{self.vla_server.server_port}"
        config = {
            "contract_version": "fq/reception-lan/v1",
            "dream_base_url": dream_url,
            "vla_base_url": vla_url,
            "request_timeout_sec": 2,
        }
        return check_reception_real_preflight(
            config,
            dream=DreamClient(dream_url),
            vla=VlaClient(vla_url),
        )

    def test_preflight_accepts_exact_idle_disarmed_gate_without_commands(self):
        FakeDreamHandler.motion_ready = False
        FakeDreamHandler.motion_blockers = ["GATEWAY_NOT_READY", "TOKEN_NOT_READY"]
        report = self._preflight_report()
        self.assertTrue(report["ready"], report)
        self.assertTrue(report["dream"]["idle_disarmed_before_first_command"])
        self.assertEqual({}, FakeDreamHandler.commands)
        self.assertEqual({}, FakeVlaHandler.tasks)

    def test_preflight_rejects_any_nonstandby_motion_blocker(self):
        FakeDreamHandler.motion_ready = False
        FakeDreamHandler.motion_blockers = [
            "GATEWAY_NOT_READY", "TOKEN_NOT_READY", "LOCALIZATION_DEGRADED",
        ]
        report = self._preflight_report()
        self.assertFalse(report["ready"], report)
        self.assertIn("LOCALIZATION_DEGRADED", report["blockers"][0])
        self.assertEqual({}, FakeDreamHandler.commands)
        self.assertEqual({}, FakeVlaHandler.tasks)

    def test_preflight_blocks_stale_vla_holding(self):
        FakeVlaHandler.holding = "cola_can_1"
        report = self._preflight_report()
        self.assertFalse(report["ready"], report)
        self.assertIn("持有物体", report["blockers"][0])
        self.assertEqual({}, FakeDreamHandler.commands)
        self.assertEqual({}, FakeVlaHandler.tasks)

    def test_complete_single_pipeline(self):
        dream_url = f"http://127.0.0.1:{self.dream_server.server_port}"
        vla_url = f"http://127.0.0.1:{self.vla_server.server_port}"
        with tempfile.TemporaryDirectory() as runtime_dir:
            config = {
                "contract_version": "fq/reception-lan/v1",
                "dream_base_url": dream_url,
                "vla_base_url": vla_url,
                "runtime_dir": runtime_dir,
                "dream_poll_interval_sec": 0.01,
                "vla_poll_interval_sec": 0.01,
                "navigation_timeout_sec": 2,
                "door_lateral_timeout_sec": 2,
                "vla_timeout_sec": 2,
                "dream_inspection_enabled": True,
                "photo_verification_enabled": True,
            }
            runner = ReceptionRealRunner(
                config,
                dream=DreamClient(dream_url),
                vla=VlaClient(vla_url),
                verifier=ReceptionVerifier({
                    "min_vlm_confidence": 0.5,
                    "vlm": {"kind": "vlm"},
                    "llm": {"kind": "llm"},
                }, chat_completion=fake_chat_completion),
                store=ReceptionStore(runtime_dir),
            )
            result = runner.run("reception-test-001")
            self.assertEqual("SUCCEEDED", result["state"], result)
            self.assertEqual("PLACE_VERIFIED", result["verified_state"])
            self.assertIsNone(result["holding"])
            self.assertEqual("table_1", result["object_location"])
            self.assertTrue(os.path.exists(os.path.join(runtime_dir, "current_task.json")))
            self.assertTrue(os.path.exists(os.path.join(runtime_dir, "images", "pick_after.jpg")))
            self.assertTrue(os.path.exists(os.path.join(runtime_dir, "images", "place_after.jpg")))

        semantic_events = [(a, b) for a, b, _c in EVENTS]
        self.assertEqual([
            ("dream", "table_2"),
            ("dream", "inspection"),
            ("vla", "pick"),
            ("snapshot", "verify_pick"),
            ("verify", "pick"),
            ("dream", "door_1"),
            ("dream", "door_1"),
            ("dream", "table_1"),
            ("vla", "place"),
            ("snapshot", "verify_place"),
            ("verify", "place"),
        ], semantic_events)

    def test_complete_pipeline_without_photo_verification(self):
        dream_url = f"http://127.0.0.1:{self.dream_server.server_port}"
        vla_url = f"http://127.0.0.1:{self.vla_server.server_port}"
        with tempfile.TemporaryDirectory() as runtime_dir:
            runner = ReceptionRealRunner(
                {
                    "contract_version": "fq/reception-lan/v1",
                    "dream_base_url": dream_url,
                    "vla_base_url": vla_url,
                    "runtime_dir": runtime_dir,
                    "dream_poll_interval_sec": 0.01,
                    "vla_poll_interval_sec": 0.01,
                    "navigation_timeout_sec": 2,
                    "door_lateral_timeout_sec": 2,
                    "vla_timeout_sec": 2,
                    "place_timeout_sec": 2,
                    "vla_result_policy": "hand_state_only",
                    "dream_inspection_enabled": True,
                    "photo_verification_enabled": False,
                },
                dream=DreamClient(dream_url),
                vla=VlaClient(vla_url),
                store=ReceptionStore(runtime_dir),
            )
            result = runner.run("reception-no-photo-001")
            self.assertEqual("COMPLETED_HAND_STATE_ONLY", result["state"], result)
            self.assertEqual("PLACE_CONFIRMED_BY_VLA", result["verified_state"])
            self.assertEqual("vla_only", result["evidence_level"])
            self.assertFalse(os.path.exists(
                os.path.join(runtime_dir, "images", "pick_after.jpg")))
            self.assertFalse(any(event[0] in {"snapshot", "verify"} for event in EVENTS))

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
