"""Shared reasoning uses immutable facts; no live model or robot is contacted."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from brain.adapters.execution import BodyAdapter, interpret_observation
from brain.app import create_service
from brain.application import BrainApplication
from brain.packages.reception import PHASES
from brain.reasoning import TaskReasoner, recovery_context
from contracts.tasks import CONTRACT_VERSION
from tests.brain.test_kernel_reception import FakeBody


def route(package="reception", scope=True):
    return json.dumps({"package": package, "scope_match": scope, "reason": "匹配单罐任务"})


class ReasoningIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"brain": {"capture_timeline": False}, "reflection": {"enabled": False},
                       "reception_real": {"kernel_runtime_dir": self.temp.name, "dream_inspection_enabled": False}}
        self.body = FakeBody()
        self.ports = []
        for name, fn in (("select_backend", lambda c, package, **kw: "reception_real"),
                         ("target_identity", lambda c, backend: {"backend": backend})):
            patcher = patch("brain.service." + name, side_effect=fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def service(self, model):
        def factory(backend):
            self.ports.append(backend)
            return self.body
        return create_service(self.config, model=model, port_factory=factory)

    def test_semantic_selection_uses_fixed_sop_and_unchanged_wire(self):
        calls = []
        def model(messages):
            calls.append(messages)
            return route()
        service = self.service(model)
        service.publish("把二号桌的一罐可乐拿到一号桌放下", "reasoning-sop")
        record = service.runtime.record
        self.assertEqual(record["state"], "succeeded")
        self.assertEqual(record["selection"]["source"], "llm")
        self.assertEqual(record["selection"]["sop"]["id"], "reception.single_can")
        self.assertEqual(record["phase_order"], [step.step_id for step in PHASES])
        self.assertEqual(len(self.body.submits), 6)
        self.assertEqual(len(calls), 1)
        golden = {item["step_id"]: item["body"] for item in json.loads(
            (Path(__file__).parents[1] / "contracts/reception_wire.json").read_text())}
        for step in PHASES:
            attempts = (record["steps"].get(step.step_id) or {}).get("attempts") or []
            if not attempts:
                continue
            attempt = attempts[-1]
            expected = deepcopy(golden[step.step_id])
            expected.update(task_id=record["task_id"], command_id=attempt["command_id"])
            if step.kind != "navigate":
                proof_step = "NAVIGATING_TO_TABLE2" if step.kind == "pick" else "NAVIGATING_TO_TABLE1"
                proof = record["steps"][proof_step]["attempts"][-1]["command_id"]
                expected["navigation_proof"]["dream_command_id"] = proof
            self.assertEqual(attempt["request"]["body"], expected)

    def test_exact_sop_runs_without_model_and_pins_version(self):
        def model(_):
            raise AssertionError("固定触发词不需要重新生成步骤")
        service = self.service(model)
        service.publish("开始接待", "known-sop")
        self.assertEqual(service.status()["state"], "succeeded")
        self.assertEqual(service.runtime.record["selection"]["sop"]["version"], "1")

    def test_out_of_scope_reception_never_creates_an_action_port(self):
        service = self.service(lambda _: route("clarify", False))
        service.publish("开始接待，给十个人各送一罐", "unsupported-scope")
        self.assertEqual(service.status()["state"], "recovery_required")
        self.assertFalse(self.ports)
        self.assertFalse(self.body.submits)
        self.assertFalse(service.runtime.record["phase_specs"])

    def test_model_cannot_replace_sop_steps_or_inject_coordinates(self):
        service = self.service(lambda _: json.dumps({"package": "reception", "scope_match": True,
            "reason": "skip", "steps": [{"skill": "place"}], "goal_xyt": [99, 99, 0]}))
        service.publish("接待时改走别的路线", "invalid-selection")
        self.assertEqual(service.status()["state"], "recovery_required")
        self.assertFalse(self.body.submits)

    def test_illegal_recovery_advice_is_rejected_without_retry(self):
        self.body.outcomes["VLA_PICKING"] = {"timed_out": True}
        service = self.service(lambda _: json.dumps({"action": "retry", "summary": "直接再抓", "reason": "猜测失败"}))
        service.publish("开始接待", "bad-recovery")
        status = service.status()
        self.assertEqual(status["state"], "recovery_required")
        advice = status["recovery_advice"]
        self.assertEqual(advice["source"], "rules")
        self.assertEqual(advice["action"], "query_original")
        self.assertFalse(advice["automatic"])
        self.assertEqual(len(self.body.submits), 2)
        original = service.runtime.record["open_command_id"]
        service.control("continue")
        self.assertEqual(service.runtime.record["open_command_id"], original)
        self.assertEqual(len(self.body.submits), 2)
        self.assertIn(original, self.body.queries)

    def test_advice_survives_restart_without_becoming_a_new_command(self):
        self.body.outcomes["VLA_PICKING"] = {"timed_out": True}
        service = self.service(lambda _: json.dumps({"action": "query_original", "summary": "回包未确认", "reason": "先查询"}))
        service.publish("开始接待", "restart-advice")
        original = service.runtime.record["open_command_id"]
        restarted = self.service(lambda _: "{}")
        self.assertEqual(restarted.status()["recovery_advice"]["command_id"], original)
        restarted.attach()
        self.assertEqual(len(self.body.submits), 2)

    def test_replaced_task_rejects_old_advice_without_writing(self):
        self.body.outcomes["NAVIGATING_TO_TABLE2"] = {"terminal": "failed", "stopped": True, "resources_released": True}
        service = self.service(lambda _: "{}")
        service.publish("开始接待", "advice-old")
        old_runtime = service.runtime
        context = recovery_context(old_runtime.snapshot())
        self.assertTrue(service.control("cancel")["completed"])
        self.body.outcomes.clear()
        service.publish("开始接待", "advice-new")
        before = service.runtime.snapshot()
        self.assertFalse(old_runtime.record_advice(context, {"action": "cancel"}))
        self.assertEqual(service.runtime.snapshot(), before)

    def test_cancel_while_selecting_prevents_late_binding_and_dispatch(self):
        entered, release = threading.Event(), threading.Event()
        def model(_):
            entered.set()
            release.wait(5)
            return route()
        app = BrainApplication(self.service(model))
        thread = threading.Thread(target=app.publish, args=("搬一罐可乐到目标桌", "cancel-selection"))
        try:
            thread.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(app.status()["state"], "running")
            self.assertTrue(app.control("cancel")["completed"])
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(app.status()["state"], "cancelled")
            self.assertFalse(self.ports)
            self.assertFalse(self.body.submits)
        finally:
            release.set()
            thread.join(3)
            app.close(3)

    def test_cancel_while_analyzing_discards_late_advice(self):
        self.body.outcomes["NAVIGATING_TO_TABLE2"] = {"terminal": "failed", "stopped": True, "resources_released": True}
        entered, release = threading.Event(), threading.Event()
        def model(_):
            entered.set()
            release.wait(5)
            return json.dumps({"action": "query_original", "summary": "查询", "reason": "等待"})
        app = BrainApplication(self.service(model))
        try:
            app.publish("开始接待", "cancel-analysis")
            self.assertTrue(entered.wait(2))
            before = time.monotonic()
            self.assertTrue(app.control("cancel")["completed"])
            self.assertLess(time.monotonic() - before, 1)
            release.set()
            app.owner.call(lambda: None)
            self.assertEqual(app.status()["state"], "cancelled")
            self.assertIsNone(app.status()["recovery_advice"])
            self.assertEqual(len(self.body.submits), 1)
        finally:
            release.set()
            app.close(3)


class EvidenceAndHandoffTests(unittest.TestCase):
    def payload(self):
        request = {"skill": "pick", "task_id": "task", "body": {"contract_version": CONTRACT_VERSION,
            "task_id": "task", "command_id": "pick", "operation": "pick", "object_id": "cola_can_1"}}
        raw = {"contract_version": CONTRACT_VERSION, "task_id": "task", "command_id": "pick",
            "operation": "pick", "state": "succeeded", "completed_at": datetime.now().astimezone().isoformat(),
            "result": {"success": True, "object_id": "cola_can_1", "object_grasped": True,
                       "holding": "cola_can_1", "policy_stopped": True, "navigation_port_ready": True,
                       "evidence_level": "hand_state_only", "evidence": {"object_presence_verified": False}}}
        return request, raw

    def test_reported_success_preserves_weak_evidence_in_ledger(self):
        request, raw = self.payload()
        viewed = interpret_observation("pick", raw, request)
        self.assertTrue(viewed["evidence"]["reported_success"])
        self.assertFalse(viewed["evidence"]["effect_verified"])
        self.assertFalse(viewed["evidence"]["supports"])
        from brain.kernel.runner import Runner
        from tests.brain.test_kernel_reception import _contract
        progress = Runner()._progress(_contract("pick", object_evidence=True), viewed)
        self.assertEqual(progress.evidence["downstream"], raw)
        self.assertIs(progress.evidence["downstream"]["result"]["evidence"]["object_presence_verified"], False)

    def test_explicit_unverified_object_cannot_be_upgraded_by_omitting_grade(self):
        request, raw = self.payload()
        del raw["result"]["evidence_level"]
        result = interpret_observation("pick", raw, request)
        self.assertTrue(result["evidence"]["reported_success"])
        self.assertFalse(result["evidence"]["supports"])

    def test_timeout_preserves_last_waiting_state_for_analysis(self):
        from brain.adapters.http_client import HttpContractError
        request, raw = self.payload()
        raw.update(state="waiting_operator_approval", completed_at=None, result=None)
        class Vla:
            def wait_task(self, *args, **kwargs):
                raise HttpContractError("timeout", payload=raw)
        viewed = BodyAdapter(object(), Vla()).wait("pick", request)
        self.assertTrue(viewed["timed_out"])
        self.assertEqual(viewed["raw"]["state"], "waiting_operator_approval")
        self.assertFalse(viewed["evidence"])

    def test_idle_transport_is_never_promoted_to_controller_takeover(self):
        request, raw = self.payload()
        class Dream:
            def status(self):
                return {"active_command_id": "", "active_command_state": None, "navigation_transport_ready": True}
        class Vla:
            def task(self, command_id):
                return deepcopy(raw)
            def control_status(self):
                return {"active_command_id": None, "policy_running": False,
                        "action_port": {"navigation_port_ready": True}}
        adapter = BodyAdapter(Dream(), Vla())
        context = {"task_id": "task", "kind": "to_nav", "source_command_id": "pick", "source_request": request}
        result = adapter.handoff_with_context(context)
        self.assertTrue(result["transport_ready"])
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["reason"], "controller_receipt_unavailable")
        raw["task_id"] = "foreign"
        result = adapter.handoff_with_context(context)
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["reason"], "source_stop_unconfirmed")

    def test_model_outage_gives_actionable_advice_without_fake_facts(self):
        def unavailable(_):
            raise TimeoutError("model timed out")
        reasoner = TaskReasoner(unavailable, {})
        context = recovery_context({"task_id": "task", "revision": 1, "state": "recovery_required",
                                    "open_command_id": "original", "holding": "cola_can_1"})
        result = reasoner.advise(context)
        self.assertEqual(result["action"], "query_original")
        self.assertEqual(result["source"], "rules")
        self.assertEqual(context["ledger_state"]["holding"], "cola_can_1")
