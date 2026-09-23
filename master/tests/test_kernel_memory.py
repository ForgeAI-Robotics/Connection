"""Phase-2 provenance, short-term window, restart, and lost-response replay."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


MASTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.abspath(os.path.join(MASTER_DIR, ".."))
for _path in (ROOT, MASTER_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import yaml

from kernel.memory import (
    DEFAULT_EVENT_LIMIT,
    DEFAULT_EVENT_TTL_SEC,
    append_observation,
    event_window_settings,
    read_subject,
    select_window,
)
from kernel.runtime import TaskRuntime
from kernel.store import KernelStore
from tests.test_kernel_reception import FakeBody, _runtime


def _stamp(minutes: float) -> str:
    return (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(
        timespec="milliseconds"
    )


class MemoryTests(unittest.TestCase):
    def test_defaults_when_config_omits_the_window(self):
        limit, ttl = event_window_settings({})
        self.assertEqual(limit, DEFAULT_EVENT_LIMIT)
        self.assertEqual(ttl, DEFAULT_EVENT_TTL_SEC)
        self.assertEqual(DEFAULT_EVENT_LIMIT, 20)
        self.assertEqual(DEFAULT_EVENT_TTL_SEC, 1800)

    def test_example_config_keeps_the_window_and_the_switch_off(self):
        with open(os.path.join(ROOT, "config", "examples", "master.yaml"), encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        real = loaded["reception_real"]
        self.assertIs(real["kernel_enabled"], False)
        self.assertEqual(real["event_memory_limit"], 20)
        self.assertEqual(real["event_memory_ttl_sec"], 1800)

    def test_window_keeps_twenty_and_leaves_the_file(self):
        now = datetime.now(timezone.utc)
        events = [
            {"time": (now - timedelta(minutes=24 - index)).isoformat(), "event": f"e{index}"}
            for index in range(25)
        ]
        window = select_window(events, limit=20, ttl_sec=1800, now=now)
        self.assertEqual(len(window), 20)
        self.assertEqual(len(events), 25)
        self.assertEqual(window[0]["event"], "e5")
        self.assertEqual(window[-1]["event"], "e24")

    def test_expired_events_leave_the_window_only(self):
        now = datetime.now(timezone.utc)
        events = [
            {"time": (now - timedelta(minutes=40)).isoformat(), "event": "old"},
            {"time": (now - timedelta(minutes=5)).isoformat(), "event": "new"},
        ]
        window = select_window(events, limit=20, ttl_sec=1800, now=now)
        self.assertEqual([item["event"] for item in window], ["new"])
        self.assertEqual(len(events), 2)

    def test_established_progress_does_not_expire(self):
        record = {}
        append_observation(
            record,
            subject="object_location",
            value="in_gripper",
            source="attempt-pick",
            observed_at=_stamp(-90),
            kind="established",
        )
        view = read_subject(record["observations"], "object_location")
        self.assertEqual(view["certain"], "in_gripper")
        self.assertEqual(view["clues"], [])

    def test_expired_location_is_only_a_clue(self):
        record = {}
        append_observation(
            record,
            subject="object_location",
            value="table_2",
            source="camera-1",
            observed_at=_stamp(-40),
            kind="observed",
            valid_until=_stamp(-10),
        )
        view = read_subject(record["observations"], "object_location")
        self.assertIsNone(view["certain"])
        self.assertEqual(view["clues"][0]["value"], "table_2")
        self.assertEqual(view["clues"][0]["source"], "camera-1")

    def test_conflict_keeps_both_sources(self):
        record = {}
        append_observation(
            record,
            subject="object_location",
            value="in_gripper",
            source="attempt-pick",
            observed_at=_stamp(-5),
            kind="established",
        )
        append_observation(
            record,
            subject="object_location",
            value="table_2",
            source="camera-1",
            observed_at=_stamp(-1),
            kind="observed",
            valid_until=_stamp(10),
        )
        view = read_subject(record["observations"], "object_location")
        self.assertIsNone(view["certain"])
        self.assertEqual(view["established"]["source"], "attempt-pick")
        self.assertEqual(view["conflicts"][0]["source"], "camera-1")
        self.assertEqual(len(record["observations"]), 2)

    def test_observed_fact_requires_a_deadline(self):
        with self.assertRaises(ValueError):
            append_observation(
                {},
                subject="object_location",
                value="table_2",
                source="camera-1",
                observed_at=_stamp(0),
                kind="observed",
            )

    def test_window_does_not_drop_an_open_command(self):
        with tempfile.TemporaryDirectory() as root:
            store = KernelStore(root)
            store.save_state({
                "task_id": "task-abcdef123456",
                "state": "recovery_required",
                "open_command_id": "nav-table2-abcdef123456",
                "command_unknown": True,
                "revision": 0,
            })
            now = datetime.now(timezone.utc)
            lines = []
            for index in range(25):
                stamp = (now - timedelta(minutes=40 + index)).isoformat()
                lines.append(json.dumps({"time": stamp, "event": f"old-{index}"}))
            (store.root / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            window = store.recent_events(limit=20, ttl_sec=1800, now=now)
            loaded = store.load_state()
            self.assertEqual(window, [])
            self.assertEqual(len(store.read_events()), 25)
            self.assertEqual(loaded["open_command_id"], "nav-table2-abcdef123456")
            self.assertEqual(loaded["state"], "recovery_required")


class RestartTests(unittest.TestCase):
    def test_restart_requeries_the_original_command(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["NAVIGATING_TO_TABLE2"] = {"timed_out": True, "terminal": None}
            runtime = _runtime(root, port)
            runtime.drive()
            command_id = runtime.record["open_command_id"]
            submits = list(port.submits)
            restarted = TaskRuntime(KernelStore(root), port, config=runtime.config)
            self.assertEqual(restarted.state, "recovery_required")
            self.assertEqual(restarted.record["open_command_id"], command_id)
            restarted.resume()
            self.assertEqual(port.submits, submits)
            self.assertIn(command_id, port.queries)
            self.assertFalse(any(item.endswith("-r1") for item in port.submits))
            self.assertEqual(restarted.state, "recovery_required")

    def test_lost_response_stays_unknown_until_the_same_command_is_checked(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["NAVIGATING_TO_TABLE2"] = {"timed_out": True, "terminal": None}
            runtime = _runtime(root, port)
            runtime.drive()
            command_id = runtime.record["open_command_id"]
            port.commands[command_id] = {
                "command_id": command_id,
                "terminal": None,
                "timed_out": True,
                "started": None,
                "stopped": False,
                "resources_released": False,
                "evidence": {},
            }
            submits = list(port.submits)
            runtime.resume()
            self.assertEqual(port.queries[-1], command_id)
            self.assertEqual(port.submits, submits)
            self.assertTrue(runtime.record["command_unknown"])
            self.assertEqual(runtime.state, "recovery_required")

    def test_confirmed_place_survives_a_later_disagreement(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root)
            runtime.drive()
            self.assertEqual(runtime.record["object_location"], "table_1")
            view = runtime.belief("object_location")
            self.assertEqual(view["certain"], "table_1")
            self.assertGreaterEqual(len(view["history"]), 2)
            runtime.record_scene(
                subject="object_location",
                value="table_2",
                source="camera-1",
                valid_until=_stamp(10),
            )
            disagreed = runtime.belief("object_location")
            self.assertIsNone(disagreed["certain"])
            self.assertEqual(runtime.record["object_location"], "table_1")
            self.assertEqual(disagreed["conflicts"][0]["source"], "camera-1")
            self.assertEqual(disagreed["established"]["value"], "table_1")


if __name__ == "__main__":
    unittest.main()
