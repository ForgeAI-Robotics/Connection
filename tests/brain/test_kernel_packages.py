"""Phase-3 packages reuse the existing runtime and only register real skills."""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta


from pathlib import Path
ROOT = str(Path(__file__).resolve().parents[2])

from brain.adapters.execution import BodyAdapter, LookAdapter, parse_capture_scene
from brain.packages.desk import EXISTING_SKILLS, matches as desk_matches
from brain.packages.look import PHASES as LOOK_PHASES
from brain.packages.registry import match_name
from brain.packages.reception import phase_ids
from tests.factories import TaskRuntime
from brain.kernel.runtime import _TRANSITIONS
from brain.skills.catalog import CATALOG, registered_ids
from brain.storage.tasks import KernelStore
from execution.robot_api.desk import SKILLS


_FROZEN_TRANSITIONS = {
    ("running", "action_finished"): "verifying",
    ("verifying", "pass_continue"): "running",
    ("verifying", "pass_final"): "succeeded",
    ("verifying", "fail"): "recovery_required",
    ("verifying", "unknown"): "recovery_required",
    ("running", "unknown"): "recovery_required",
    ("running", "gate"): "waiting_human",
    ("waiting_human", "human_continue"): "running",
    ("running", "pause_ack"): "paused",
    ("running", "pause_nack"): "recovery_required",
    ("paused", "resume_pause"): "running",
    ("cancelling", "cancel_cleared"): "cancelled",
    ("cancelling", "cancel_unclear"): "recovery_required",
    ("recovery_required", "requery_not_started"): "running",
    ("recovery_required", "requery_ended"): "verifying",
    ("recovery_required", "retry_allowed"): "running",
    ("running", "handoff_unconfirmed"): "recovery_required",
    ("verifying", "handoff_unconfirmed"): "recovery_required",
}


def _scene(text="桌上有一个可乐罐", source="overhead_cam") -> str:
    return json.dumps([f"视野描述（{source}）：{text}", {"_status": "success"}])


def _look(root, capture, *, deadline=None, ttl_sec=None):
    real = {"dream_inspection_enabled": False}
    if ttl_sec is not None:
        real["event_memory_ttl_sec"] = ttl_sec
    runtime = TaskRuntime(
        KernelStore(root),
        LookAdapter(capture),
        config={"reception_real": real},
        package="look",
    )
    if deadline is not None:
        runtime.phases = [replace(runtime.phases[0], deadline_sec=deadline)]
    runtime.open_task("task-abcdef123456", task_desc="桌上有什么")
    return runtime


class PackageTests(unittest.TestCase):
    def test_catalog_lists_only_existing_executors(self):
        self.assertEqual(
            registered_ids(),
            ("navigate", "inspect", "pick", "place", "describe", "desk_check"),
        )
        self.assertNotIn("voice", registered_ids())
        self.assertNotIn("help", registered_ids())
        describe = next(item for item in CATALOG if item.skill_id == "describe")
        self.assertEqual(describe.execution, "camera.capture_scene")
        self.assertFalse(describe.physical)

    def test_match_order_uses_existing_tasks(self):
        self.assertEqual(match_name("开始接待"), "reception")
        self.assertEqual(match_name("桌上有什么"), "look")
        self.assertEqual(match_name("整理桌面"), "desk")
        self.assertEqual(match_name("去门口等一下"), "")

    def test_desk_skills_are_the_existing_four(self):
        self.assertEqual(set(EXISTING_SKILLS), set(SKILLS))
        self.assertTrue(desk_matches("收拾桌"))
        self.assertFalse(desk_matches("开始接待"))

    def test_state_machine_is_unchanged(self):
        self.assertEqual(_TRANSITIONS, _FROZEN_TRANSITIONS)

    def test_look_runs_on_the_same_runtime_without_a_body_command(self):
        calls = []

        def capture(task):
            calls.append(task)
            return _scene()

        with tempfile.TemporaryDirectory() as root:
            runtime = _look(root, capture)
            self.assertEqual(runtime.drive(), "succeeded")
            self.assertEqual(calls, ["桌上有什么"])
            self.assertIsNone(runtime.record["open_command_id"])
            self.assertFalse(runtime.record["command_unknown"])
            self.assertEqual(runtime.record.get("dispatch_counts") or {}, {})
            self.assertIsNone(runtime.record["object_location"])
            self.assertFalse(runtime.record["safe_idle"])
            evidence = runtime.record["pending_progress"]["evidence"]
            self.assertEqual(evidence["text"], "桌上有一个可乐罐")
            self.assertEqual(evidence["source"], "overhead_cam")
            self.assertEqual(evidence["grade"], "description")
            row = runtime.record["observations"][0]
            self.assertEqual(row["subject"], "scene_description")
            self.assertEqual(row["value"], "桌上有一个可乐罐")
            self.assertEqual(row["source"], "overhead_cam")
            self.assertTrue(row["observed_at"])
            self.assertTrue(row["valid_until"])
            fresh = runtime.belief("scene_description")
            self.assertEqual(fresh["certain"], "桌上有一个可乐罐")
            self.assertEqual(fresh["clues"], [])
            expired_at = datetime.fromisoformat(row["valid_until"]) + timedelta(milliseconds=1)
            expired = runtime.belief("scene_description", now=expired_at)
            self.assertIsNone(expired["certain"])
            self.assertEqual(expired["current"], [])
            self.assertEqual(expired["clues"][0]["value"], "桌上有一个可乐罐")
            self.assertEqual(expired["clues"][0]["source"], "overhead_cam")
            self.assertEqual(runtime.record["package"], "look")
            self.assertNotEqual(runtime.record["phase_order"], phase_ids())
            restarted = TaskRuntime(KernelStore(root), LookAdapter(capture))
            self.assertEqual(restarted.package_name, "look")
            self.assertEqual(restarted.state, "succeeded")
            self.assertEqual(calls, ["桌上有什么"])

    def test_configured_observation_ttl_is_used(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = _look(root, lambda task: _scene(), ttl_sec=5)
            self.assertEqual(runtime.drive(), "succeeded")
            row = runtime.record["observations"][0]
            span = datetime.fromisoformat(row["valid_until"]) - datetime.fromisoformat(
                row["observed_at"]
            )
            self.assertEqual(span.total_seconds(), 5)

    def test_capture_scene_result_is_the_adapter_contract(self):
        path = os.path.join(ROOT, "src", "execution", "slaver", "robot", "module", "camera.py")
        spec = importlib.util.spec_from_file_location("slaver_camera_scene_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.backend_capture_image = lambda camera_name="": {"success": True, "image": "abc"}
        module._call_vlm = lambda images, context="": ("described", "桌上有一个可乐罐")
        evidence = parse_capture_scene(module.capture_scene("桌上有什么"))
        self.assertEqual(evidence["text"], "桌上有一个可乐罐")
        self.assertIn("overhead_cam", evidence["source"])
        self.assertEqual(evidence["grade"], "description")
        observed = evidence["observation"]
        self.assertEqual(observed["subject"], "scene_description")
        self.assertEqual(observed["source"], evidence["source"])
        self.assertTrue(observed["observed_at"])
        self.assertTrue(observed["valid_until"])

    def test_describe_exception_enters_recovery_required(self):
        def capture(task):
            raise RuntimeError("camera down")

        with tempfile.TemporaryDirectory() as root:
            runtime = _look(root, capture)
            self.assertEqual(runtime.drive(), "recovery_required")
            self.assertEqual(runtime.record["blocked_reason"], "camera down")
            self.assertIsNone(runtime.record["open_command_id"])
            self.assertFalse(runtime.record["command_unknown"])

    def test_describe_timeout_enters_recovery_required(self):
        def capture(task):
            time.sleep(1)
            return _scene()

        with tempfile.TemporaryDirectory() as root:
            runtime = _look(root, capture, deadline=0.05)
            self.assertEqual(runtime.drive(), "recovery_required")
            self.assertEqual(runtime.record["blocked_reason"], "timeout")
            self.assertIsNone(runtime.record["open_command_id"])

    def test_body_adapter_does_not_accept_a_description_as_motion(self):
        adapter = BodyAdapter(object(), object())
        with self.assertRaises(RuntimeError):
            adapter.submit("look-abcdef123456", {"skill": "describe", "body": {}})

    def test_look_phases_do_not_move(self):
        self.assertEqual([step.kind for step in LOOK_PHASES], ["describe"])
        self.assertFalse(LOOK_PHASES[0].body)


if __name__ == "__main__":
    unittest.main()
