"""Software replay for the phase-1 reception kernel.

The fake port declares contract version fq/reception-lan/v1.
It never calls NX and never stands in for the live reception runner.
"""

import os
import sys
import tempfile
import threading
import unittest

import yaml


from pathlib import Path
ROOT = str(Path(__file__).resolve().parents[2])

from brain.adapters.http_client import HttpContractError
from brain.adapters.execution import BodyAdapter, interpret_observation
from contracts.tasks import (
    CONTRACT_VERSION,
    IllegalTransition,
    ProgressEvent,
    Rejected,
    SkillContract,
    StaleWrite,
    make_command_id,
    task_token,
)
from brain.config_flags import kernel_enabled
from brain.packages.reception import phase_ids
from brain.kernel.runner import Runner
from tests.factories import TaskRuntime
from brain.kernel.runtime import classify_query
from brain.storage.tasks import KernelStore
from brain.service_support import control_task
from brain.kernel.verifier import Verifier
PIPELINE_PHASES = ('INITIALIZING', 'FETCHING_WORLD', 'NAVIGATING_TO_TABLE2', 'DREAM_INSPECTING_TABLE2', 'VLA_PICKING', 'VERIFYING_GRASP', 'NAVIGATING_TO_RELAY2', 'LATERAL_TO_RELAY3', 'NAVIGATING_TO_TABLE1', 'VLA_PLACING', 'VERIFYING_PLACE')


NAV_BODY_KEYS = {
    "contract_version", "command_id", "task_id", "target_id", "route_phase",
    "leg_index", "frame_id", "goal_xyt", "motion_mode", "require_final_orientation",
}
VLA_BODY_KEYS = {
    "contract_version", "command_id", "task_id", "operation", "object_id",
    "target_area", "navigation_proof",
}


class FakeBody:
    """In-process DREAM/VLA stand-in for software replay."""

    contract_version = CONTRACT_VERSION

    def __init__(self):
        self.commands = {}
        self.submits = []
        self.queries = []
        self.cancels = []
        self.handoff_queries = []
        self.outcomes = {}
        self.handoff_confirmed = {"to_vla": True, "to_nav": True, "safe_idle": True}
        self.gate_open = True

    def _observed(self, step_id, command_id):
        outcome = dict(self.outcomes.get(step_id) or {})
        if "terminal" in outcome:
            terminal = outcome.get("terminal") or ""
        elif outcome.get("timed_out"):
            terminal = ""
        else:
            terminal = "succeeded"
        evidence = outcome.get("evidence")
        if evidence is None and terminal == "succeeded" and not outcome.get("timed_out"):
            evidence = {
                "supports": True,
                "contradicts": False,
                "identity_ok": True,
                "time_ok": True,
                "grade": "object",
                "identity": command_id,
            }
        return {
            "command_id": command_id,
            "terminal": terminal,
            "timed_out": bool(outcome.get("timed_out")),
            "started": outcome.get("started", True),
            "stopped": outcome.get("stopped", False),
            "resources_released": outcome.get("resources_released", False),
            "evidence": dict(evidence or {}),
        }

    def submit(self, command_id, request):
        self.commands[command_id] = self._observed(request.get("step_id"), command_id)
        self.submits.append(command_id)
        return {"accepted": True, "completed": False, "command_id": command_id}

    def wait(self, command_id, request):
        self.queries.append(command_id)
        observed = self._observed(request.get("step_id"), command_id)
        self.commands[command_id] = observed
        return dict(observed)

    def query(self, command_id, request):
        del request
        self.queries.append(command_id)
        if command_id in self.commands:
            return dict(self.commands[command_id])
        return {
            "command_id": command_id,
            "terminal": "",
            "timed_out": False,
            "started": None,
            "stopped": False,
            "resources_released": False,
            "evidence": {},
        }

    def cancel(self, command_id, request):
        del request
        self.cancels.append(command_id)
        return {"accepted": True, "completed": False, "command_id": command_id}

    def handoff(self, step_id, kind):
        self.handoff_queries.append((step_id, kind))
        return {
            "available": True,
            "confirmed": bool(self.handoff_confirmed.get(kind)),
            "kind": kind,
        }


class MemoryStore:
    def __init__(self):
        self.state = None
        self.events = []

    def load_state(self):
        return self.state

    def save_state(self, state):
        self.state = state
        return state

    def append_event(self, event, **data):
        self.events.append((event, data))

    def save_image(self, name, image_bytes):
        return name


def _runtime(root, port=None, task_id="task-abcdef123456"):
    port = port or FakeBody()
    runtime = TaskRuntime(
        KernelStore(root),
        port,
        config={"reception_real": {"dream_inspection_enabled": False}},
    )
    runtime.open_task(task_id, task_desc="开始接待")
    return runtime


def _support(command_id="cmd"):
    return {
        "supports": True,
        "contradicts": False,
        "identity_ok": True,
        "time_ok": True,
        "grade": "object",
        "identity": command_id,
    }


def _contract(skill, *, object_evidence=False, safe_idle=False):
    return SkillContract(
        package="reception",
        skill=skill,
        version=CONTRACT_VERSION,
        task_id="task-abcdef123456",
        step_id="VLA_PICKING" if skill == "pick" else "VLA_PLACING",
        attempt_id="attempt-1",
        goal=skill,
        object_id="cola_can_1",
        preconditions="",
        evidence="object_held" if skill == "pick" else "object_at_target",
        failure_budget=0,
        deadline_sec=30,
        command_prefix=f"vla-{skill}",
        command_id="cmd-1",
        postconditions=("object",),
        handoff="",
        tools=(skill,),
        requires_object_evidence=object_evidence,
        requires_safe_idle=safe_idle,
        request={},
    )


def _progress(**overrides):
    payload = dict(
        task_id="task-abcdef123456",
        step_id="VLA_PICKING",
        attempt_id="VLA_PICKING-a1",
        command_id="cmd-1",
        phase="VLA_PICKING",
        progress="succeeded",
        evidence_ref="VLA_PICKING-a1.jpg",
        action_ended=True,
        effect_ok=True,
        publisher_stopped=True,
        handoff_confirmed=False,
        timed_out=False,
        terminal="succeeded",
        evidence=_support("cmd-1"),
        safe_idle=True,
        error="",
    )
    payload.update(overrides)
    return ProgressEvent(**payload)


class StoreTests(unittest.TestCase):
    def test_two_attempts_on_one_step_both_remain(self):
        with tempfile.TemporaryDirectory() as root:
            store = KernelStore(root)
            first = {
                "task_id": "task-abcdef123456",
                "steps": {
                    "VLA_PICKING": {
                        "attempts": [{
                            "attempt_id": "VLA_PICKING-a1",
                            "command_id": "vla-pick-abcdef123456",
                            "contract": {"version": CONTRACT_VERSION, "marker": "first"},
                            "request": {"body": {"goal_xyt": [1, 2, 3]}},
                            "scene": {"phase": "VLA_PICKING"},
                            "delta": {},
                            "verdict": "",
                        }],
                    },
                },
            }
            saved = store.save_state(first)
            self.assertEqual(saved["revision"], 1)
            second = {
                "revision": saved["revision"],
                "task_id": "task-abcdef123456",
                "steps": {
                    "VLA_PICKING": {
                        "attempts": [
                            {
                                "attempt_id": "VLA_PICKING-a1",
                                "command_id": "changed",
                                "contract": {"version": CONTRACT_VERSION, "marker": "mutated"},
                                "request": {"body": {"goal_xyt": [0, 0, 0]}},
                                "scene": {"phase": "mutated"},
                                "delta": {"mutated": True},
                                "verdict": "PASS",
                            },
                            {
                                "attempt_id": "VLA_PICKING-a2",
                                "command_id": "vla-pick-abcdef123456-r1",
                                "contract": {"version": CONTRACT_VERSION, "marker": "second"},
                                "request": {"body": {"goal_xyt": [4, 5, 6]}},
                                "scene": {"phase": "VLA_PICKING"},
                                "delta": {},
                                "verdict": "",
                            },
                        ],
                    },
                },
            }
            store.save_state(second)
            loaded = store.load_state()
            attempts = loaded["steps"]["VLA_PICKING"]["attempts"]
            self.assertEqual(
                [item["attempt_id"] for item in attempts],
                ["VLA_PICKING-a1", "VLA_PICKING-a2"],
            )
            self.assertEqual(attempts[0]["command_id"], "vla-pick-abcdef123456")
            self.assertEqual(attempts[0]["contract"]["marker"], "first")
            self.assertEqual(attempts[0]["request"]["body"]["goal_xyt"], [1, 2, 3])
            self.assertEqual(attempts[0]["verdict"], "PASS")
            self.assertEqual(attempts[1]["command_id"], "vla-pick-abcdef123456-r1")
            self.assertNotIn("grasp.jpg", attempts[0]["attempt_id"])

    def test_stale_revision_does_not_replace_the_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            store = KernelStore(root)
            saved = store.save_state({"task_id": "task-abcdef123456", "blocked_reason": "first"})
            newer = dict(saved)
            newer["blocked_reason"] = "newer"
            store.save_state(newer)
            stale = dict(saved)
            stale["blocked_reason"] = "stale"
            with self.assertRaises(StaleWrite):
                store.save_state(stale)
            self.assertEqual(store.load_state()["blocked_reason"], "newer")


class StateMachineTests(unittest.TestCase):
    def _fresh(self):
        runtime = TaskRuntime(MemoryStore(), FakeBody(), config={})
        runtime.record["task_id"] = "task-1"
        return runtime

    def test_table_transitions(self):
        cases = []

        def add(name, events, expected):
            cases.append((name, events, expected))

        add("accept", [("accept", {})], "running")
        add("action finished", [("accept", {}), ("action_finished", {})], "verifying")
        add(
            "pass continue",
            [("accept", {}), ("action_finished", {}), ("pass_continue", {"handoff_ok": True})],
            "running",
        )
        add(
            "pass final",
            [("accept", {}), ("action_finished", {}), ("pass_final", {"safe_idle": True})],
            "succeeded",
        )
        add(
            "fail",
            [("accept", {}), ("action_finished", {}), ("fail", {})],
            "recovery_required",
        )
        add(
            "unknown while verifying",
            [("accept", {}), ("action_finished", {}), ("unknown", {})],
            "recovery_required",
        )
        add("gate", [("accept", {}), ("gate", {})], "waiting_human")
        add(
            "human continue",
            [("accept", {}), ("gate", {}), ("human_continue", {})],
            "running",
        )
        add("pause ack", [("accept", {}), ("pause_ack", {})], "paused")
        add("pause nack", [("accept", {}), ("pause_nack", {})], "recovery_required")
        add(
            "resume pause",
            [("accept", {}), ("pause_ack", {}), ("resume_pause", {})],
            "running",
        )
        add("cancel accepted", [("accept", {}), ("cancel_accepted", {})], "cancelling")
        add(
            "cancel cleared",
            [("accept", {}), ("cancel_accepted", {}), ("cancel_cleared", {})],
            "cancelled",
        )
        add(
            "cancel unclear",
            [("accept", {}), ("cancel_accepted", {}), ("cancel_unclear", {})],
            "recovery_required",
        )
        add(
            "requery not started",
            [("accept", {}), ("pause_nack", {}), ("requery_not_started", {})],
            "running",
        )
        add(
            "requery ended",
            [("accept", {}), ("pause_nack", {}), ("requery_ended", {})],
            "verifying",
        )
        add(
            "retry allowed",
            [("accept", {}), ("pause_nack", {}), ("retry_allowed", {})],
            "running",
        )
        add(
            "timeout while running",
            [("accept", {}), ("unknown", {})],
            "recovery_required",
        )
        for name, events, expected in cases:
            with self.subTest(name=name):
                runtime = self._fresh()
                for event, facts in events:
                    runtime.apply(event, **facts)
                self.assertEqual(runtime.state, expected)

    def test_illegal_transition_keeps_state(self):
        runtime = self._fresh()
        runtime.apply("accept")
        runtime.apply("pause_ack")
        with self.assertRaises(IllegalTransition):
            runtime.apply("pass_final", safe_idle=True)
        self.assertEqual(runtime.state, "paused")

    def test_waiting_human_does_not_become_recovery(self):
        runtime = self._fresh()
        runtime.apply("accept")
        runtime.apply("gate")
        self.assertEqual(runtime.note_still_waiting(), "waiting_human")
        with self.assertRaises(IllegalTransition):
            runtime.apply("unknown")
        self.assertEqual(runtime.state, "waiting_human")

    def test_human_continue_rejects_uncleared_command(self):
        runtime = self._fresh()
        runtime.apply("accept")
        runtime.apply("gate")
        runtime.record["command_unknown"] = True
        with self.assertRaises(Rejected):
            runtime.apply("human_continue")
        self.assertEqual(runtime.state, "waiting_human")

    def test_republish_and_force_new_rejected(self):
        runtime = self._fresh()
        runtime.open_task("task-keep")
        with self.assertRaises(Rejected):
            runtime.open_task("task-other")
        with self.assertRaises(Rejected):
            runtime.open_task("task-forced", force_new=True)
        self.assertEqual(runtime.state, "running")
        self.assertEqual(runtime.record["task_id"], "task-keep")

        runtime.apply("unknown")
        with self.assertRaises(Rejected):
            runtime.open_task("task-forced", force_new=True)
        self.assertEqual(runtime.state, "recovery_required")
        self.assertEqual(runtime.record["task_id"], "task-keep")

    def test_public_status_exposes_nonterminal_names(self):
        runtime = self._fresh()
        runtime.apply("accept")
        runtime.apply("pause_ack")
        self.assertEqual(runtime.public_status()["state"], "paused")
        runtime.apply("resume_pause")
        runtime.apply("gate")
        self.assertEqual(runtime.public_status()["state"], "waiting_human")
        runtime.apply("cancel_accepted")
        self.assertEqual(runtime.public_status()["state"], "cancelling")
        runtime.apply("cancel_unclear")
        status = runtime.public_status()
        self.assertEqual(status["state"], "recovery_required")
        self.assertFalse(status["terminal"])
        self.assertTrue(status["blocks_new_motion"])


class VerifierTests(unittest.TestCase):
    def test_three_verdicts_for_grasp_and_place(self):
        verifier = Verifier()
        for skill in ("pick", "place"):
            contract = _contract(
                skill,
                object_evidence=True,
                safe_idle=(skill == "place"),
            )
            passed = verifier.judge(contract, _progress(safe_idle=True))
            failed = verifier.judge(
                contract,
                _progress(evidence={**_support(), "supports": False, "contradicts": True}),
            )
            unknown = verifier.judge(
                contract,
                _progress(
                    terminal="",
                    action_ended=False,
                    evidence={},
                    safe_idle=False,
                ),
            )
            hand_only = verifier.judge(
                contract,
                _progress(evidence={**_support(), "grade": "hand_state_only"}),
            )
            self.assertEqual(passed, "PASS", skill)
            self.assertEqual(failed, "FAIL", skill)
            self.assertEqual(unknown, "UNKNOWN", skill)
            self.assertEqual(hand_only, "UNKNOWN", skill)
            self.assertNotEqual(unknown, "FAIL")


class ReplayTests(unittest.TestCase):
    def test_phase_order_matches_reception_package(self):
        self.assertEqual(tuple(phase_ids()), PIPELINE_PHASES)

    def test_command_id_shape_uses_step_attempt_not_task_resume_count(self):
        task_id = "task-abcdef123456"
        self.assertEqual(task_token(task_id), "abcdef123456")
        self.assertEqual(make_command_id("nav-table2", task_id, 1), "nav-table2-abcdef123456")
        self.assertEqual(make_command_id("vla-pick", task_id, 2), "vla-pick-abcdef123456-r1")
        self.assertNotIn("-r5", make_command_id("nav-table2", task_id, 2))

    def test_fake_success_reaches_succeeded_with_safe_idle(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            runtime = _runtime(root, port)
            self.assertEqual(port.contract_version, CONTRACT_VERSION)
            self.assertEqual(runtime.drive(), "succeeded")
            self.assertEqual(runtime.record["object_location"], "table_1")
            self.assertIsNone(runtime.record["holding"])
            self.assertTrue(runtime.record["safe_idle"])
            prefixes = [item.split("-abcdef123456")[0] for item in port.submits]
            self.assertEqual(
                prefixes,
                ["nav-table2", "vla-pick", "nav-relay2", "nav-relay3", "nav-table1", "vla-place"],
            )
            self.assertFalse(any(item.startswith("inspect-") for item in port.submits))
            pick = runtime.record["steps"]["VLA_PICKING"]["attempts"][0]
            self.assertIn(pick["attempt_id"], pick["evidence_ref"])
            self.assertNotEqual(pick["evidence_ref"], "grasp.jpg")
            self.assertEqual(pick["contract"]["failure_budget"], 0)
            nav_body = runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][0]["request"]["body"]
            place_body = runtime.record["steps"]["VLA_PLACING"]["attempts"][0]["request"]["body"]
            self.assertEqual(set(nav_body), NAV_BODY_KEYS)
            self.assertEqual(set(place_body), VLA_BODY_KEYS)
            relay3_queries = [item for item in port.handoff_queries if item[0] == "LATERAL_TO_RELAY3"]
            self.assertEqual(relay3_queries, [])
            self.assertEqual(runtime.dispatch_count("LATERAL_TO_RELAY3"), 1)

    def test_runner_ignores_foreign_step(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root)
            before = runtime.record["cursor"]
            accepted = runtime.consume_foreign_progress(
                _progress(step_id="VLA_PLACING", phase="VLA_PLACING"),
            )
            self.assertFalse(accepted)
            self.assertEqual(runtime.record["cursor"], before)
            self.assertEqual(runtime.state, "running")

    def test_wait_deadline_keeps_command_id(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["NAVIGATING_TO_TABLE2"] = {"timed_out": True, "terminal": None}
            runtime = _runtime(root, port)
            self.assertEqual(runtime.drive(), "recovery_required")
            command_id = runtime.record["open_command_id"]
            self.assertEqual(port.submits, [command_id])
            self.assertNotIn("-r", command_id)
            self.assertEqual(
                runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][0]["verdict"],
                "UNKNOWN",
            )

    def test_resume_before_clearance_only_queries_original_id(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["NAVIGATING_TO_TABLE2"] = {"timed_out": True, "terminal": None}
            runtime = _runtime(root, port)
            runtime.drive()
            command_id = runtime.record["open_command_id"]
            submits = list(port.submits)
            runtime.resume()
            with self.assertRaises(Rejected):
                runtime.open_new_attempt()
            self.assertEqual(port.submits, submits)
            self.assertIn(command_id, port.queries)
            self.assertFalse(any("-r" in item for item in port.submits))
            self.assertEqual(runtime.state, "recovery_required")

    def test_new_attempt_only_after_not_started(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["NAVIGATING_TO_TABLE2"] = {"timed_out": True, "terminal": None}
            runtime = _runtime(root, port)
            runtime.drive()
            command_id = runtime.record["open_command_id"]
            port.commands[command_id]["started"] = False
            port.commands[command_id]["stopped"] = True
            port.commands[command_id]["timed_out"] = False
            runtime.requery()
            self.assertTrue(runtime.record["not_started_confirmed"])
            runtime.open_new_attempt()
            runtime.drive()
            attempts = runtime.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"]
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["command_id"], command_id)
            self.assertTrue(attempts[1]["command_id"].endswith("-r1"))
            self.assertIn(attempts[1]["command_id"], port.submits)

    def test_missing_grasp_terminal_does_not_claim_gripper_or_navigate(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["VLA_PICKING"] = {"terminal": None, "evidence": {}}
            runtime = _runtime(root, port)
            self.assertEqual(runtime.drive(), "recovery_required")
            self.assertEqual(
                runtime.record["steps"]["VLA_PICKING"]["attempts"][0]["verdict"],
                "UNKNOWN",
            )
            self.assertNotEqual(runtime.record["object_location"], "in_gripper")
            self.assertIsNone(runtime.record["holding"])
            self.assertEqual(runtime.dispatch_count("NAVIGATING_TO_RELAY2"), 0)
            self.assertEqual(runtime.dispatch_count("LATERAL_TO_RELAY3"), 0)
            self.assertEqual(runtime.dispatch_count("NAVIGATING_TO_TABLE1"), 0)

    def test_missing_place_terminal_does_not_claim_table(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["VLA_PLACING"] = {"terminal": None, "evidence": {}}
            runtime = _runtime(root, port)
            self.assertEqual(runtime.drive(), "recovery_required")
            self.assertNotEqual(runtime.state, "succeeded")
            self.assertNotEqual(runtime.record["object_location"], "table_1")
            self.assertEqual(
                runtime.record["steps"]["VLA_PLACING"]["attempts"][0]["verdict"],
                "UNKNOWN",
            )

    def test_hand_state_only_is_not_success(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["VLA_PLACING"] = {
                "terminal": "succeeded",
                "evidence": {**_support(), "grade": "hand_state_only"},
            }
            runtime = _runtime(root, port)
            self.assertEqual(runtime.drive(), "recovery_required")
            verdict = runtime.record["steps"]["VLA_PLACING"]["attempts"][0]["verdict"]
            self.assertNotEqual(verdict, "PASS")
            self.assertNotEqual(runtime.state, "succeeded")
            self.assertNotEqual(runtime.record["object_location"], "table_1")

    def test_unconfirmed_handoff_after_grasp_blocks_relay2(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.handoff_confirmed["to_nav"] = False
            runtime = _runtime(root, port)
            self.assertEqual(runtime.drive(), "recovery_required")
            self.assertEqual(runtime.dispatch_count("NAVIGATING_TO_RELAY2"), 0)
            self.assertGreater(runtime.dispatch_count("VLA_PICKING"), 0)

    def test_place_without_safe_idle_is_not_succeeded(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.handoff_confirmed["safe_idle"] = False
            port.handoff_confirmed["to_nav"] = True
            runtime = _runtime(root, port)
            self.assertNotEqual(runtime.drive(), "succeeded")
            self.assertNotEqual(runtime.record["object_location"], "table_1")

    def test_cancel_accepted_is_not_cancelled(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            runtime = _runtime(root, port)
            result = runtime.request_cancel()
            self.assertTrue(result["accepted"])
            self.assertFalse(result["completed"])
            self.assertEqual(runtime.state, "cancelling")

    def test_cancel_cleared_becomes_cancelled(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root)
            runtime.request_cancel()
            runtime.resolve_cancel(
                command_terminal=True, stopped=True, resources_released=True,
            )
            self.assertEqual(runtime.state, "cancelled")

    def test_cancel_unclear_becomes_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root)
            runtime.request_cancel()
            runtime.resolve_cancel(
                command_terminal=True, stopped=False, resources_released=False,
            )
            self.assertEqual(runtime.state, "recovery_required")

    def test_navigation_gate_waits_for_human(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.gate_open = False
            runtime = _runtime(root, port)
            self.assertEqual(runtime.drive(), "waiting_human")
            self.assertEqual(port.submits, [])
            self.assertEqual(runtime.note_still_waiting(), "waiting_human")
            self.assertNotEqual(runtime.state, "recovery_required")

    def test_pause_without_stop_ack_is_not_paused(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root)
            runtime.request_pause(stop_acknowledged=False)
            self.assertNotEqual(runtime.state, "paused")
            self.assertEqual(runtime.state, "recovery_required")

    def test_republish_while_running_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = _runtime(root, task_id="task-abcdef123456")
            with self.assertRaises(Rejected):
                runtime.open_task("task-zzzzzzzzzzzz")
            self.assertEqual(runtime.record["task_id"], "task-abcdef123456")
            self.assertEqual(runtime.state, "running")

    def test_force_new_while_resources_unknown_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["NAVIGATING_TO_TABLE2"] = {"timed_out": True, "terminal": None}
            runtime = _runtime(root, port)
            runtime.drive()
            self.assertTrue(runtime.record["command_unknown"])
            with self.assertRaises(Rejected):
                runtime.open_task("task-zzzzzzzzzzzz", force_new=True)
            self.assertEqual(runtime.state, "recovery_required")
            self.assertEqual(runtime.record["open_command_id"].startswith("nav-table2-"), True)


class AdapterTests(unittest.TestCase):
    def test_handoff_is_unavailable_without_calling_clients(self):
        class Exploding:
            def __getattr__(self, name):
                raise AssertionError(name)

        adapter = BodyAdapter(Exploding(), Exploding())
        result = adapter.handoff("VLA_PICKING", "to_vla")
        self.assertFalse(result["available"])
        self.assertFalse(result["confirmed"])
        self.assertEqual(adapter.contract_version, CONTRACT_VERSION)

    def test_timeout_does_not_mint_a_new_command(self):
        class Dream:
            def __init__(self):
                self.submits = []

            def submit_navigation(self, payload):
                self.submits.append(payload["command_id"])
                return {"accepted": True}

            def wait_command(self, command_id, **kwargs):
                raise HttpContractError(f"deadline {command_id}")

        dream = Dream()
        adapter = BodyAdapter(dream, object())
        contract = _contract("pick")
        contract.skill = "navigate"
        contract.step_id = "NAVIGATING_TO_TABLE2"
        contract.command_id = "nav-table2-abcdef123456"
        contract.request = {
            "skill": "navigate",
            "deadline_sec": 1,
            "body": {"command_id": contract.command_id},
        }
        progress = Runner().execute(contract, adapter)
        self.assertTrue(progress.timed_out)
        self.assertEqual(progress.command_id, "nav-table2-abcdef123456")
        self.assertEqual(dream.submits, ["nav-table2-abcdef123456"])
        self.assertFalse(any(item.endswith("-r1") for item in dream.submits))


class SwitchTests(unittest.TestCase):
    def test_kernel_switch_defaults_off(self):
        self.assertFalse(kernel_enabled({}))
        self.assertFalse(kernel_enabled({"reception_real": {}}))
        self.assertFalse(kernel_enabled({"reception_real": {"kernel_enabled": False}}))
        self.assertTrue(kernel_enabled({"reception_real": {"kernel_enabled": True}}))
        for relative in (
            os.path.join("config", "examples", "brain.yaml"),
            os.path.join("config", "brain.yaml"),
        ):
            with open(os.path.join(ROOT, relative), "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
            self.assertIs(loaded["reception_real"]["kernel_enabled"], False)

    def test_control_routes_are_wired(self):
        with open(os.path.join(ROOT, "src/brain/api/app.py"), encoding="utf-8") as handle:
            run_text = handle.read()
        with open(os.path.join(ROOT, "src/entries/web/app.py"), encoding="utf-8") as handle:
            deploy_text = handle.read()
        with open(
            os.path.join(ROOT, "src/entries/web/templates/index.html"),
            encoding="utf-8",
        ) as handle:
            page = handle.read()
        for path in ("/api/task_pause", "/api/task_continue", "/api/task_cancel"):
            self.assertIn(path, run_text)
            self.assertIn(path, deploy_text)
        self.assertIn("已暂停", page)
        self.assertIn("等待人工", page)
        self.assertIn("取消中，停止尚未核清", page)
        self.assertIn("recovery_required", page)


def _nav_request(command_id="nav-table2-abcdef123456", task_id="task-abcdef123456"):
    return {
        "skill": "navigate",
        "task_id": task_id,
        "step_id": "NAVIGATING_TO_TABLE2",
        "body": {
            "contract_version": CONTRACT_VERSION,
            "command_id": command_id,
            "task_id": task_id,
            "target_id": "table_2",
            "route_phase": "",
            "leg_index": 1,
        },
    }


def _nav_raw(**overrides):
    result = {
        "success": True,
        "reached": True,
        "navigation_stopped": True,
    }
    if "result" in overrides:
        result = overrides.pop("result")
    raw = {
        "contract_version": CONTRACT_VERSION,
        "task_id": "task-abcdef123456",
        "command_id": "nav-table2-abcdef123456",
        "target_id": "table_2",
        "route_phase": "",
        "leg_index": 1,
        "state": "succeeded",
        "completed_at": "2026-08-27T14:00:00.000+08:00",
        "result": result,
    }
    raw.update(overrides)
    return raw


def _pick_request():
    return {
        "skill": "pick",
        "task_id": "task-abcdef123456",
        "body": {
            "contract_version": CONTRACT_VERSION,
            "command_id": "vla-pick-abcdef123456",
            "task_id": "task-abcdef123456",
            "operation": "pick",
            "object_id": "cola_can_1",
        },
    }


def _pick_raw(**result_overrides):
    result = {
        "success": True,
        "object_id": "cola_can_1",
        "object_grasped": True,
        "holding": "cola_can_1",
        "policy_stopped": True,
        "navigation_port_ready": True,
    }
    result.update(result_overrides)
    return {
        "contract_version": CONTRACT_VERSION,
        "task_id": "task-abcdef123456",
        "command_id": "vla-pick-abcdef123456",
        "operation": "pick",
        "state": "succeeded",
        "completed_at": "2026-08-27T14:01:00.000+08:00",
        "result": result,
    }


class HoldPort(FakeBody):
    def __init__(self):
        super().__init__()
        self.in_wait = threading.Event()
        self.release_wait = threading.Event()
        self.seen = {}
        self.root = None

    def wait(self, command_id, request):
        self.in_wait.set()
        if not self.release_wait.wait(5):
            raise TimeoutError("wait stayed blocked")
        return super().wait(command_id, request)

    def cancel(self, command_id, request):
        if self.root is not None:
            disk = KernelStore(self.root).load_state()
            self.seen = {
                "dispatch_closed": disk.get("dispatch_closed"),
                "state": disk.get("state"),
                "control_request": disk.get("control_request"),
            }
        return super().cancel(command_id, request)


class ReviewTests(unittest.TestCase):
    def test_concurrent_drive_submits_each_command_once(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            config = {"reception_real": {"dream_inspection_enabled": False}}
            TaskRuntime(KernelStore(root), port, config=config).open_task(
                "task-abcdef123456", task_desc="开始接待",
            )
            errors = []

            def run():
                try:
                    TaskRuntime(KernelStore(root), port, config=config).drive()
                except Exception as exc:
                    errors.append(repr(exc))

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(port.submits), len(set(port.submits)))
            self.assertEqual(port.submits.count("nav-table2-abcdef123456"), 1)
            self.assertEqual(KernelStore(root).load_state()["state"], "succeeded")

    def test_concurrent_recovery_issues_one_new_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["NAVIGATING_TO_TABLE2"] = {"timed_out": True, "terminal": None}
            runtime = _runtime(root, port)
            self.assertEqual(runtime.drive(), "recovery_required")
            command_id = runtime.record["open_command_id"]
            port.commands[command_id]["started"] = False
            port.commands[command_id]["stopped"] = True
            port.commands[command_id]["timed_out"] = False
            port.outcomes["NAVIGATING_TO_TABLE2"] = {}
            errors = []

            def recover():
                try:
                    other = TaskRuntime(KernelStore(root), port, config=runtime.config)
                    try:
                        other.resume()
                    except IllegalTransition:
                        return
                    try:
                        other.open_new_attempt()
                    except (Rejected, IllegalTransition, StaleWrite):
                        pass
                    other.drive()
                except Exception as exc:
                    errors.append(repr(exc))

            threads = [threading.Thread(target=recover) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(port.submits.count(command_id), 1)
            self.assertEqual(port.submits.count(f"{command_id}-r1"), 1)
            self.assertFalse(any(item.endswith("-r2") for item in port.submits))

    def test_cancel_persists_before_downstream_and_blocks_the_next_submit(self):
        with tempfile.TemporaryDirectory() as root:
            port = HoldPort()
            port.root = root
            runtime = _runtime(root, port)
            errors = []

            def drive():
                try:
                    runtime.drive()
                except Exception as exc:
                    errors.append(repr(exc))

            thread = threading.Thread(target=drive)
            thread.start()
            self.assertTrue(port.in_wait.wait(5))
            runtime.request_cancel()
            self.assertTrue(port.seen.get("dispatch_closed"))
            self.assertEqual(port.seen.get("state"), "running")
            self.assertEqual(port.seen.get("control_request"), "cancel")
            self.assertEqual(runtime.state, "cancelling")
            port.release_wait.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(port.submits), 1)
            self.assertEqual(runtime.dispatch_count("VLA_PICKING"), 0)
            self.assertEqual(runtime.state, "cancelling")

    def test_pause_persists_before_downstream_and_is_not_paused_without_stop(self):
        with tempfile.TemporaryDirectory() as root:
            port = HoldPort()
            port.root = root
            runtime = _runtime(root, port)
            errors = []

            def drive():
                try:
                    runtime.drive()
                except Exception as exc:
                    errors.append(repr(exc))

            thread = threading.Thread(target=drive)
            thread.start()
            self.assertTrue(port.in_wait.wait(5))
            runtime.request_pause(stop_acknowledged=False)
            self.assertTrue(port.seen.get("dispatch_closed"))
            self.assertEqual(port.seen.get("state"), "running")
            self.assertNotEqual(runtime.state, "paused")
            self.assertEqual(runtime.state, "recovery_required")
            port.release_wait.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(port.submits), 1)
            self.assertEqual(runtime.dispatch_count("NAVIGATING_TO_RELAY2"), 0)

    def test_foreign_success_and_pseudo_terminal_do_not_clear_the_step(self):
        request = _nav_request()
        command_id = request["body"]["command_id"]
        foreign = _nav_raw(command_id="nav-table2-someoneelse", task_id="other-task")
        viewed = interpret_observation(command_id, foreign, request)
        self.assertFalse(viewed["evidence"]["identity_ok"])
        self.assertFalse(viewed["evidence"]["supports"])
        self.assertFalse(viewed["stopped"])
        self.assertEqual(viewed["terminal"], "")
        self.assertEqual(
            classify_query(viewed, requires_object_evidence=False),
            "unknown",
        )

        pseudo = _nav_raw(
            state="cancelled",
            result={"success": False, "reached": False},
        )
        viewed = interpret_observation(command_id, pseudo, request)
        self.assertEqual(viewed["terminal"], "cancelled")
        self.assertFalse(viewed["stopped"])
        self.assertFalse(viewed["resources_released"])
        self.assertEqual(
            classify_query(viewed, requires_object_evidence=False),
            "unknown",
        )

        explicit = _nav_raw(
            state="failed",
            result={"success": False, "reached": False, "navigation_stopped": True},
        )
        viewed = interpret_observation(command_id, explicit, request)
        self.assertTrue(viewed["stopped"])
        self.assertFalse(viewed["evidence"]["supports"])
        self.assertEqual(
            classify_query(viewed, requires_object_evidence=False),
            "stopped",
        )

        matched = interpret_observation(command_id, _nav_raw(), request)
        self.assertTrue(matched["evidence"]["identity_ok"])
        self.assertTrue(matched["evidence"]["supports"])
        self.assertTrue(matched["stopped"])

        pick = interpret_observation("vla-pick-abcdef123456", _pick_raw(), _pick_request())
        self.assertTrue(pick["evidence"]["supports"])
        self.assertEqual(pick["evidence"]["grade"], "object")
        self.assertTrue(pick["stopped"])
        hand = interpret_observation(
            "vla-pick-abcdef123456",
            _pick_raw(evidence_level="hand_state_only"),
            _pick_request(),
        )
        self.assertEqual(hand["evidence"]["grade"], "hand_state_only")
        self.assertFalse(hand["evidence"]["supports"])
        self.assertNotEqual(Verifier().judge(_contract("pick", object_evidence=True), _progress(
            terminal=hand["terminal"],
            evidence=hand["evidence"],
            safe_idle=False,
        )), "PASS")
        missing_stop = interpret_observation(
            "vla-pick-abcdef123456",
            _pick_raw(policy_stopped=False),
            _pick_request(),
        )
        self.assertFalse(missing_stop["stopped"])
        self.assertFalse(missing_stop["evidence"]["supports"])
        wrong_object = _pick_raw()
        wrong_object["result"] = dict(wrong_object["result"])
        wrong_object["result"]["object_id"] = "bottle_1"
        mismatch = interpret_observation(
            "vla-pick-abcdef123456", wrong_object, _pick_request(),
        )
        self.assertFalse(mismatch["evidence"]["identity_ok"])
        self.assertEqual(mismatch["terminal"], "")
        self.assertFalse(mismatch["evidence"]["supports"])
        place_request = {
            "skill": "place",
            "task_id": "task-abcdef123456",
            "body": {
                "contract_version": CONTRACT_VERSION,
                "command_id": "vla-place-abcdef123456",
                "task_id": "task-abcdef123456",
                "operation": "place",
                "object_id": "cola_can_1",
            },
        }
        place_raw = {
            "contract_version": CONTRACT_VERSION,
            "task_id": "task-abcdef123456",
            "command_id": "vla-place-abcdef123456",
            "operation": "place",
            "state": "succeeded",
            "completed_at": "2026-08-27T14:05:00.000+08:00",
            "result": {
                "success": True,
                "object_id": "cola_can_1",
                "object_grasped": False,
                "released": True,
                "holding": None,
                "object_at_target": True,
                "policy_stopped": True,
                "navigation_port_ready": True,
            },
        }
        placed = interpret_observation(
            "vla-place-abcdef123456", place_raw, place_request,
        )
        self.assertTrue(placed["evidence"]["supports"])
        self.assertEqual(placed["evidence"]["grade"], "object")
        self.assertTrue(placed["stopped"])
        missing_target = {
            "result": dict(place_raw["result"], object_at_target=False),
        }
        weak_place = interpret_observation(
            "vla-place-abcdef123456",
            dict(place_raw, **missing_target),
            place_request,
        )
        self.assertFalse(weak_place["evidence"]["supports"])
        self.assertTrue(weak_place["evidence"]["contradicts"])
        self.assertNotEqual(Verifier().judge(
            _contract("place", object_evidence=True, safe_idle=True),
            _progress(
                step_id="VLA_PLACING",
                phase="VLA_PLACING",
                terminal=weak_place["terminal"],
                evidence=weak_place["evidence"],
                safe_idle=True,
            ),
        ), "PASS")

    def test_pause_continue_cancel_entry_replay(self):
        with tempfile.TemporaryDirectory() as root:
            missing = os.path.join(root, "absent")
            payload, runtime = control_task(
                {
                    "reception_real": {
                        "kernel_enabled": False,
                        "kernel_runtime_dir": missing,
                    },
                },
                "cancel",
            )
            self.assertFalse(payload["accepted"])
            self.assertEqual(payload["error"], "内核开关关闭")
            self.assertIsNone(runtime)
            self.assertFalse(os.path.exists(missing))

            port = FakeBody()
            runtime = _runtime(root, port)
            config = {
                "reception_real": {
                    "kernel_enabled": True,
                    "kernel_runtime_dir": root,
                    "dream_inspection_enabled": False,
                },
            }
            paused, runtime = control_task(config, "pause", runtime=runtime)
            self.assertEqual(paused["state"], "paused")
            self.assertTrue(paused["paused"])
            continued, runtime = control_task(config, "continue", runtime=runtime)
            self.assertEqual(continued["state"], "running")
            cancelled, runtime = control_task(config, "cancel", runtime=runtime)
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertTrue(cancelled["completed"])
            self.assertEqual(port.submits, [])

        with tempfile.TemporaryDirectory() as root:
            port = HoldPort()
            port.root = root
            runtime = _runtime(root, port)
            errors = []

            def drive():
                try:
                    runtime.drive()
                except Exception as exc:
                    errors.append(repr(exc))

            thread = threading.Thread(target=drive)
            thread.start()
            self.assertTrue(port.in_wait.wait(5))
            config = {
                "reception_real": {
                    "kernel_enabled": True,
                    "kernel_runtime_dir": root,
                },
            }
            paused, runtime = control_task(config, "pause", runtime=runtime)
            self.assertNotEqual(paused["state"], "paused")
            self.assertFalse(paused.get("paused"))
            self.assertEqual(paused["state"], "recovery_required")
            port.release_wait.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            cancelled, runtime = control_task(config, "cancel", runtime=runtime)
            self.assertNotEqual(cancelled["state"], "cancelled")
            self.assertFalse(cancelled["completed"])
            self.assertEqual(cancelled["state"], "recovery_required")
            self.assertEqual(len(port.submits), 1)
            self.assertEqual(runtime.dispatch_count("VLA_PICKING"), 0)


if __name__ == "__main__":
    unittest.main()
