"""Application admission and recovery tests. No real body, Redis or LLM is contacted."""
import ast
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "master")):
    if path not in sys.path:
        sys.path.insert(0, path)
from master.tests import test_execution_base as execution_base
from kernel.adapters import SimAdapter, LookAdapter
from kernel.brain import Brain
from kernel.ports import MotionGate, DeskAdapter, SlaverAdapter
from kernel.packages.reception_mock import MockAdapter
from kernel.runtime import TaskRuntime
from kernel.store import KernelStore
from kernel.switch import attach_runtime
from kernel.contracts import Rejected


class UnifiedBrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = execution_base.ExecutionBaseTests()._agent(runtime_dir=os.path.join(self.tmp.name, "legacy"))
        self.agent.config.update({"brain": {"scheduler": "runtime", "reception_backend": "real"},
                                 "model": {"model_retry_planning": 0}, "reflection": {"enabled": False}})
        self.agent.config["reception_real"].update({"kernel_runtime_dir": os.path.join(self.tmp.name, "kernel"),
                                                   "kernel_enabled": False})
        self.agent._load_experiences = lambda **kwargs: ""
        self.agent._run_task_reflection = Mock()
        self.agent.planner = SimpleNamespace(forward=lambda *args: json.dumps({
            "reasoning_explanation": "test", "subtask_list": [
                {"robot_name": "FQrobot", "subtask_order": 1, "subtask": "抓取 milk_1"},
                {"robot_name": "FQrobot", "subtask_order": 2, "subtask": "放置 milk_1 到 milk_area"}]}))
        self.backend = patch("kernel.brain.select_backend", side_effect=self._backend)
        self.backend.start()
        self.addCleanup(self.backend.stop)
        self.world = {"milk_1": {"grasped": False, "pos": [0, 0], "category": "milk"}}
        self.zones = {"milk_area": {"pos": [1, 1], "radius": .1},
                      "drinks_area": {"pos": [2, 2], "radius": .1},
                      "penholder_spot": {"pos": [3, 3], "radius": .1},
                      "trash_bin": {"pos": [4, 4], "radius": .1}}
        self.actions = []
        self.port = DeskAdapter(perform=self.perform, world=lambda: self.world, zones=self.zones)
        self.agent._kernel_port_factory = self.port_factory
        # Synchronous worker keeps assertions and filesystem cleanup deterministic.
        self.brain = self.agent._brain()
        self.brain._launch = self.brain._drive

    def _backend(self, config, package, mock=False):
        return "reception_mock" if mock else {"reception": "reception_real", "look": "camera"}.get(package, "desk")

    def port_factory(self, backend):
        if backend == "reception_real":
            downstream = Mock()
            self.real_body = downstream
            return MotionGate(downstream, False)
        if backend == "camera":
            return LookAdapter(lambda task: json.dumps(["视野描述（test_camera）：桌上有牛奶", {"_status": "success"}]))
        if backend == "reception_mock":
            return MockAdapter()
        return self.port

    def perform(self, subtask):
        self.actions.append(subtask)
        if subtask.startswith("抓取"):
            self.world["milk_1"]["grasped"] = True
        else:
            self.world["milk_1"].update(grasped=False, pos=[1, 1])
        return {"success": True}

    def publish(self, text="把牛奶放好", task_id="task-unified0001", **kw):
        return self.agent.publish_global_task(text, False, task_id, **kw)

    def test_generic_already_satisfied_skill_is_observed_without_motion(self):
        self.world["milk_1"]["pos"] = [1, 1]
        self.agent.planner.forward = lambda *args: json.dumps({"subtask_list": [
            {"robot_name": "FQrobot", "subtask_order": 1, "subtask": "整理牛奶"}]})
        self.publish("把牛奶放好")
        self.assertEqual(self.agent.get_task_status()["state"], "succeeded")
        self.assertEqual(self.actions, [])
        self.assertEqual(self.agent._kernel_runtime.record["dispatch_counts"], {})

    def test_generic_entry_uses_runtime_and_actual_evidence(self):
        self.agent._dispath_subtasks_body = Mock(side_effect=AssertionError("legacy dispatch"))
        self.publish()
        status = self.agent.get_task_status()
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["source"], "kernel")
        self.assertEqual(status["completed"], 2)
        self.assertEqual(len(self.actions), 2)
        episode = self.agent._run_task_reflection.call_args.kwargs["episode"]
        contract = self.agent._kernel_runtime.record["steps"]["STEP_1"]["attempts"][0]["contract"]
        self.assertEqual(contract["object_id"], "milk_1")
        self.assertEqual(len(episode["steps"]), 2)
        self.assertTrue(all(s["verify_ok"] for s in episode["steps"]))

    def test_api_without_task_id_allocates_unique_identity(self):
        self.publish(task_id=None)
        first = self.agent.get_task_status()["task_id"]
        self.assertEqual(len(first), 32)
        self.publish("桌上有什么", task_id=None)
        second = self.agent.get_task_status()["task_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.agent.get_task_status()["state"], "succeeded")

    def test_real_reception_enters_runtime_but_flag_blocks_all_body_calls(self):
        self.agent._run_reception_skill = Mock(side_effect=AssertionError("old reception"))
        self.publish("开始接待")
        self.assertEqual(self.agent.get_task_status()["state"], "waiting_human")
        self.assertEqual(self.agent.get_task_status()["blocked_reason"], "real_execution_disabled")
        self.assertFalse(self.real_body.mock_calls)
        self.assertEqual(self.agent._kernel_runtime.record["dispatch_counts"], {})
        self.assertTrue(self.agent.kernel_cancel()["completed"])

    def test_look_entry_observes_without_commands(self):
        self.publish("桌上有什么")
        runtime = self.agent._kernel_runtime
        self.assertEqual(runtime.state, "succeeded")
        self.assertEqual(runtime.package_name, "look")
        self.assertEqual(runtime.record["dispatch_counts"], {})
        self.assertEqual(runtime.belief("scene_description")["certain"], "桌上有牛奶")

    def test_desk_entry_expands_actions_and_verifies_final_layout(self):
        self.agent._plan_desk_tidy = lambda task: {"reasoning_explanation": "test", "subtask_list": [
            {"robot_name": "FQrobot", "subtask": "整理牛奶"}]}
        self.publish("整理桌面")
        runtime = self.agent._kernel_runtime
        self.assertEqual(runtime.state, "succeeded")
        self.assertEqual(runtime.package_name, "desk")
        self.assertEqual(len(self.actions), 2)
        self.assertEqual(runtime.record["steps"]["DESK_CHECK"]["attempts"][0]["verdict"], "PASS")

    def test_empty_desk_plan_does_not_claim_an_untidy_desk_is_done(self):
        self.agent._plan_desk_tidy = lambda task: {"reasoning_explanation": "test", "subtask_list": []}
        self.publish("整理桌面")
        self.assertEqual(self.agent._kernel_runtime.state, "recovery_required")
        self.assertEqual(self.actions, [])

    def test_planner_is_called_after_runtime_owns_task_and_cancel_blocks_dispatch(self):
        def plan(*args):
            status = self.agent.get_task_status()
            self.assertEqual(status["state"], "running")
            self.assertEqual(status["source"], "kernel")
            self.agent.kernel_cancel()
            return json.dumps({"reasoning_explanation": "cancelled", "subtask_list": [
                {"robot_name": "FQrobot", "subtask_order": 1, "subtask": "抓取 milk_1"}]})
        self.agent.planner.forward = plan
        self.publish()
        self.assertEqual(self.agent._kernel_runtime.state, "cancelled")
        self.assertFalse(self.actions)

    def test_planning_error_is_owned_by_runtime(self):
        self.agent.planner.forward = Mock(side_effect=RuntimeError("planner down"))
        self.publish()
        self.assertEqual(self.agent.get_task_status()["state"], "recovery_required")
        self.assertIn("planner down", self.agent.get_task_status()["blocked_reason"])
        self.assertFalse(self.actions)

    def test_new_package_does_not_reuse_completed_generic_phases(self):
        self.publish()
        self.publish("桌上有什么", task_id="task-observe0002")
        self.assertEqual(self.agent.get_task_status()["state"], "succeeded")
        self.assertEqual(self.agent._kernel_runtime.package_name, "look")
        self.assertEqual(len(self.actions), 2)

    def test_mock_reception_uses_same_ledger_and_no_legacy_loop(self):
        self.publish("开始接待", options={"mock": True, "headcount": 4, "reflect": False})
        runtime = self.agent._kernel_runtime
        self.assertEqual(runtime.state, "succeeded")
        self.assertEqual(runtime.port.world.w_count_cola("会议室"), 4)
        self.assertEqual(len(runtime.record["dispatch_counts"]), 14)
        self.assertFalse(self.agent._run_task_reflection.called)

    def test_mock_false_place_claim_stops_without_retry(self):
        self.publish("开始接待", options={"mock": True, "scenario": "place_miss"})
        runtime = self.agent._kernel_runtime
        self.assertEqual(runtime.state, "recovery_required")
        self.assertEqual(runtime.port.world.w_count_cola("会议室"), 1)
        self.assertEqual(len(runtime.record["dispatch_counts"]), 5)

    def test_restart_generic_queries_original_not_body_adapter(self):
        self.port._perform = Mock(side_effect=TimeoutError("lost reply"))
        self.publish()
        command = self.agent._kernel_runtime.record["open_command_id"]
        self.agent._kernel_runtime = None
        self.port = DeskAdapter(perform=Mock(side_effect=AssertionError("resubmitted")), world=lambda: self.world, zones=self.zones)
        self.publish(resume=True)
        self.assertEqual(self.agent._kernel_runtime.record["open_command_id"], command)
        self.assertEqual(self.agent._kernel_runtime.state, "recovery_required")
        self.assertFalse(self.port._perform.called)

    def test_legacy_rollback_cannot_bypass_open_runtime(self):
        self.publish("开始接待")
        self.agent.config["brain"]["scheduler"] = "legacy"
        result = self.publish(task_id="task-other")
        self.assertTrue(result["ignored"])
        self.assertFalse(self.actions)

    def test_unresolved_legacy_is_read_only_and_force_cannot_bypass(self):
        from sop.reception_store import ReceptionStore
        store = ReceptionStore(self.agent._reception_runtime_dir())
        state = {"task_id": "old", "state": "RUNNING"}
        store.save_state(state)
        before = store.load_state()
        result = self.publish(force_new_task=True)
        self.assertTrue(result["ignored"])
        self.assertEqual(store.load_state(), before)
        self.assertFalse(self.actions)

    def test_external_cancel_during_planning_is_not_blocked_by_admission_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = self.agent.planner.forward
        def slow(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        self.agent.planner.forward = slow
        worker = threading.Thread(target=self.publish)
        worker.start()
        self.assertTrue(entered.wait(2))
        result = []
        cancel = threading.Thread(target=lambda: result.append(self.agent.kernel_cancel()))
        cancel.start()
        cancel.join(2)
        release.set()
        worker.join(5)
        cancel.join(5)
        self.assertTrue(result[0]["completed"])
        self.assertEqual(self.actions, [])

    def test_pause_during_planning_keeps_plan_for_continue(self):
        entered, release = threading.Event(), threading.Event()
        original = self.agent.planner.forward
        def slow(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        self.agent.planner.forward = slow
        worker = threading.Thread(target=self.publish)
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.agent.kernel_pause()["state"], "paused")
        release.set()
        worker.join(5)
        self.assertEqual(self.agent._kernel_runtime.state, "paused")
        self.assertTrue(self.agent._kernel_runtime.record["plan_ready"])
        self.assertEqual(self.actions, [])
        self.agent.kernel_continue()
        self.assertEqual(self.agent._kernel_runtime.state, "succeeded")

    def test_continue_restarts_dispatch_after_pause(self):
        self.brain._launch = lambda runtime: None
        self.publish()
        self.assertEqual(self.agent.kernel_pause()["state"], "paused")
        self.brain._launch = self.brain._drive
        self.agent.kernel_continue()
        self.assertEqual(self.agent.get_task_status()["state"], "succeeded")
        self.assertEqual(len(self.actions), 2)

    def test_old_runtime_cannot_drive_a_new_task(self):
        self.publish()
        old = self.agent._kernel_runtime
        self.publish("桌上有什么", task_id="task-next")
        with self.assertRaises(Rejected):
            old.drive()
        self.assertEqual(len(self.actions), 2)

    def test_restart_checks_original_endpoint_before_query(self):
        self.publish("开始接待")
        self.agent._kernel_runtime = None
        self.agent.config["reception_real"]["dream_base_url"] = "http://different-host"
        result = self.publish("开始接待", resume=True)
        self.assertTrue(result["ignored"])
        self.assertIn("地址", result["error"])



class AdapterConcurrencyTests(unittest.TestCase):
    def test_cancel_cannot_fabricate_stop_of_running_action(self):
        entered, release = threading.Event(), threading.Event()
        def perform(text):
            entered.set()
            release.wait(3)
            return {"success": True}
        port = SimAdapter(perform=perform, world=lambda: {"milk": {"grasped": True}}, zones={})
        request = {"deadline_sec": .1, "body": {"subtask": "抓取 milk"}}
        port.submit("original", request)
        thread = threading.Thread(target=port.wait, args=("original", request))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(port.cancel("original", request)["accepted"])
        self.assertFalse(port.query("original", request)["stopped"])
        release.set()
        thread.join(2)
        port.wait("original", request)
        self.assertEqual(len(port.commands), 1)

    def test_restart_missing_receipt_is_unknown_not_not_started(self):
        query = SimAdapter().query("original", {})
        self.assertIsNone(query["started"])
        self.assertFalse(query["stopped"])

    def test_slaver_registers_before_send_and_rejects_success_without_evidence(self):
        agent = execution_base.ExecutionBaseTests()._agent()
        def send(channel, text):
            body = json.loads(text)
            agent._handle_result(json.dumps({"robot_name": "FQrobot", "task_id": body["task_id"],
                "subtask_handle": body["task"], "subtask_result": "done", "status": "success"}))
        agent.collaborator.send = send
        port = SlaverAdapter(agent)
        request = {"body": {"robot_name": "FQrobot", "subtask": "未知动作"}, "deadline_sec": 1}
        port.submit("unique-command", request)
        with patch("robot_api.client.get_scene", return_value={}):
            result = port.wait("unique-command", request)
        self.assertFalse(result["evidence"]["supports"])
        self.assertFalse(agent.collaborator.busy["FQrobot"])

    def test_missing_place_observation_and_transport_failure_are_unknown(self):
        from kernel.adapters import _sim_view
        missing = _sim_view("cmd", ("place", "milk", "zone"), {"success": True},
                            {"milk": {}}, {"zone": {"pos": [0, 0], "radius": 1}})
        self.assertFalse(missing["evidence"]["supports"])
        self.assertEqual(missing["terminal"], "")
        lost = _sim_view("cmd", ("grasp", "milk"), {"success": False, "result": "desk 请求错误: timed out"}, {}, {})
        self.assertFalse(lost["stopped"])
        self.assertFalse(lost["resources_released"])
        self.assertIsNone(lost["started"])



class HttpRuntimeRoutingTests(unittest.TestCase):
    def test_http_resume_accepts_generic_runtime_task(self):
        from flask import Flask, jsonify, request
        from robot_api.intent import Intent, classify_task
        app = Flask(__name__)
        agent = Mock()
        agent.config = {"brain": {"scheduler": "runtime"}}
        agent._is_reception_task.return_value = False
        agent.current_task_id = "original"
        agent.publish_global_task.return_value = {"source": "kernel"}
        tree = ast.parse((ROOT / "master/run.py").read_text())
        funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "publish_task"]
        namespace = {"app": app, "jsonify": jsonify, "request": request, "master_agent": agent,
                     "Intent": Intent, "classify_task": classify_task, "_validated_task_id": lambda x: x,
                     "_inbound": lambda: {}, "journal_emit": Mock(), "note_task_request": Mock()}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "master/run.py", "exec"), namespace)
        with app.test_client() as client:
            response = client.post("/publish_task", json={"task": "抓取 milk_1", "task_id": "original", "resume": True})
        self.assertTrue(response.json["accepted"])
        self.assertTrue(agent.publish_global_task.call_args.kwargs["resume"])

    def test_master_demo_uses_global_admission_with_mock_only(self):
        from flask import Flask, jsonify, request
        app = Flask(__name__)
        agent = Mock()
        agent.config = {"brain": {"scheduler": "runtime"}}
        agent.publish_global_task.return_value = {"source": "kernel"}
        agent.get_task_status.return_value = {"state": "succeeded", "source": "kernel"}
        tree = ast.parse((ROOT / "master/run.py").read_text())
        funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "reception_demo"]
        namespace = {"app": app, "jsonify": jsonify, "request": request, "master_agent": agent}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "master/run.py", "exec"), namespace)
        with app.test_client() as client:
            response = client.post("/api/reception/run", json={"headcount": 3, "scenario": "place_miss"})
        self.assertTrue(response.json["success"])
        kwargs = agent.publish_global_task.call_args.kwargs
        self.assertEqual(kwargs["options"]["mock"], True)
        self.assertEqual(kwargs["options"]["headcount"], 3)

    def test_deploy_demo_proxies_master_without_starting_a_subprocess(self):
        from flask import Flask, jsonify, request
        app = Flask(__name__)
        requests = Mock()
        requests.exceptions.RequestException = RuntimeError
        requests.post.return_value.json.return_value = {"success": True, "task_id": "same-task"}
        requests.post.return_value.status_code = 200
        tree = ast.parse((ROOT / "deploy/run.py").read_text())
        funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "api_reception_run"]
        namespace = {"app": app, "jsonify": jsonify, "request": request, "requests": requests, "MASTER_URL": "http://master"}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "deploy/run.py", "exec"), namespace)
        with app.test_client() as client:
            response = client.post("/api/reception/run", json={"headcount": 3})
        self.assertEqual(response.json["task_id"], "same-task")
        self.assertEqual(requests.post.call_args.args, ("http://master/api/reception/run",))
        self.assertEqual(requests.post.call_args.kwargs["json"], {"headcount": 3})
