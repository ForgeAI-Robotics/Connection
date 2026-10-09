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

from brain.adapters.execution import SimAdapter, LookAdapter
from brain.app import create_service
from brain.learning.worker import episode_from_record
from brain.adapters.ports import MotionGate, DeskAdapter, SlaverAdapter
from brain.packages.reception_mock import MockAdapter
from tests.factories import TaskRuntime
from brain.storage.tasks import KernelStore
from brain.service_support import attach_runtime
from contracts.tasks import Rejected


class UnifiedBrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"brain": {"scheduler": "runtime", "reception_backend": "real"},
                       "model": {"model_retry_planning": 0}, "reflection": {"enabled": False},
                       "reception_real": {"kernel_runtime_dir": self.tmp.name, "kernel_enabled": False}}
        self.model = SimpleNamespace(forward=lambda *args: json.dumps({"reasoning_explanation": "test", "subtask_list": [
            {"robot_name": "FQrobot", "subtask": "抓取 milk_1"},
            {"robot_name": "FQrobot", "subtask": "放置 milk_1 到 milk_area"}]}))
        self.reflection = Mock()
        self.backend = patch("brain.service.select_backend", side_effect=self._backend)
        self.backend.start()
        self.addCleanup(self.backend.stop)
        self.world = {"milk_1": {"grasped": False, "pos": [0, 0], "category": "milk"}}
        self.zones = {"milk_area": {"pos": [1, 1], "radius": .1},
                      "drinks_area": {"pos": [2, 2], "radius": .1},
                      "penholder_spot": {"pos": [3, 3], "radius": .1},
                      "trash_bin": {"pos": [4, 4], "radius": .1}}
        self.actions = []
        self.port = DeskAdapter(perform=self.perform, world=lambda: self.world, zones=self.zones)
        self.service = create_service(self.config, model=lambda messages: self.model.forward(messages),
                                      port_factory=self.port_factory, reflection=self.reflection)
        self.brain = self.service

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
        return self.service.publish(text, task_id, **kw)

    def test_control_target_race_cannot_cancel_new_task(self):
        self.publish()
        before = self.service.runtime.snapshot()
        with self.assertRaisesRegex(Rejected, '目标任务已变化'):
            self.service.control('cancel', task_id='older-task')
        self.assertEqual(before, self.service.runtime.snapshot())
        result = self.service.control('cancel', task_id=before['task_id'])
        self.assertTrue(result['no_op'])
        self.assertEqual(before, self.service.runtime.snapshot())

    def test_control_text_never_creates_planning_ledger(self):
        with self.assertRaisesRegex(Rejected, '控制入口'):
            self.publish('停止')
        self.assertIsNone(self.service.runtime)

    def test_generic_already_satisfied_skill_is_observed_without_motion(self):
        self.world["milk_1"]["pos"] = [1, 1]
        self.model.forward = lambda *args: json.dumps({"subtask_list": [
            {"robot_name": "FQrobot", "subtask_order": 1, "subtask": "整理牛奶"}]})
        self.publish("把牛奶放好")
        self.assertEqual(self.service.status()["state"], "succeeded")
        self.assertEqual(self.actions, [])
        self.assertEqual(self.service.runtime.record["dispatch_counts"], {})

    def test_generic_entry_uses_runtime_and_actual_evidence(self):
        self.publish()
        status = self.service.status()
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["source"], "kernel")
        self.assertEqual(status["completed"], 2)
        self.assertEqual(len(self.actions), 2)
        episode = episode_from_record(self.reflection.call_args.args[0])
        contract = self.service.runtime.record["steps"]["STEP_1"]["attempts"][0]["contract"]
        self.assertEqual(contract["object_id"], "milk_1")
        self.assertEqual(len(episode["steps"]), 2)
        self.assertTrue(all(s["verify_ok"] for s in episode["steps"]))

    def test_api_without_task_id_allocates_unique_identity(self):
        self.publish(task_id=None)
        first = self.service.status()["task_id"]
        self.assertEqual(len(first), 32)
        self.publish("桌上有什么", task_id=None)
        second = self.service.status()["task_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.service.status()["state"], "succeeded")

    def test_real_reception_enters_runtime_but_flag_blocks_all_body_calls(self):
        self.publish("开始接待")
        self.assertEqual(self.service.status()["state"], "waiting_human")
        self.assertEqual(self.service.status()["blocked_reason"], "real_execution_disabled")
        self.assertFalse(self.real_body.mock_calls)
        self.assertEqual(self.service.runtime.record["dispatch_counts"], {})
        self.assertTrue(self.service.control('cancel')["completed"])

    def test_cancel_is_local_after_address_change_but_continue_remains_blocked(self):
        self.publish("开始接待")
        self.assertIsNone(self.service.runtime.record.get("open_command_id"))
        self.assertFalse(self.service.runtime.record.get("command_unknown"))
        before = list(self.real_body.mock_calls)
        from brain.storage.tasks import KernelStore
        store = KernelStore(self.tmp.name)
        record = store.load_state()
        record["execution_target"] = {
            "dream": "http://192.0.2.18:8001",
            "vla": "http://192.0.2.194:8091",
        }
        record["open_command_id"] = "cmd-old"
        store.save_state(record)
        self.service.runtime = None
        with self.assertRaisesRegex(Rejected, "执行地址或后端已变化"):
            self.service.control("continue")
        self.service.runtime = None
        result = self.service.control("cancel")
        self.assertTrue(result["completed"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(list(self.real_body.mock_calls), before)

    def test_look_entry_observes_without_commands(self):
        self.publish("桌上有什么")
        runtime = self.service.runtime
        self.assertEqual(runtime.state, "succeeded")
        self.assertEqual(runtime.package_name, "look")
        self.assertEqual(runtime.record["dispatch_counts"], {})
        self.assertEqual(runtime.belief("scene_description")["certain"], "桌上有牛奶")

    def test_desk_entry_expands_actions_and_verifies_final_layout(self):
        self.service.planning.desk_planner = lambda task: {"reasoning_explanation": "test", "subtask_list": [
            {"robot_name": "FQrobot", "subtask": "整理牛奶"}]}
        self.publish("整理桌面")
        runtime = self.service.runtime
        self.assertEqual(runtime.state, "succeeded")
        self.assertEqual(runtime.package_name, "desk")
        self.assertEqual(len(self.actions), 2)
        self.assertEqual(runtime.record["steps"]["DESK_CHECK"]["attempts"][0]["verdict"], "PASS")

    def test_empty_desk_plan_does_not_claim_an_untidy_desk_is_done(self):
        self.service.planning.desk_planner = lambda task: {"reasoning_explanation": "test", "subtask_list": []}
        self.publish("整理桌面")
        self.assertEqual(self.service.runtime.state, "recovery_required")
        self.assertEqual(self.actions, [])

    def test_planner_is_called_after_runtime_owns_task_and_cancel_blocks_dispatch(self):
        def plan(*args):
            status = self.service.status()
            self.assertEqual(status["state"], "running")
            self.assertEqual(status["source"], "kernel")
            self.service.control('cancel')
            return json.dumps({"reasoning_explanation": "cancelled", "subtask_list": [
                {"robot_name": "FQrobot", "subtask_order": 1, "subtask": "抓取 milk_1"}]})
        self.model.forward = plan
        self.publish()
        self.assertEqual(self.service.runtime.state, "cancelled")
        self.assertFalse(self.actions)

    def test_planning_error_is_owned_by_runtime(self):
        self.model.forward = Mock(side_effect=RuntimeError("planner down"))
        self.publish()
        self.assertEqual(self.service.status()["state"], "recovery_required")
        self.assertIn("planner down", self.service.status()["blocked_reason"])
        self.assertFalse(self.actions)

    def test_new_package_does_not_reuse_completed_generic_phases(self):
        self.publish()
        self.publish("桌上有什么", task_id="task-observe0002")
        self.assertEqual(self.service.status()["state"], "succeeded")
        self.assertEqual(self.service.runtime.package_name, "look")
        self.assertEqual(len(self.actions), 2)

    def test_mock_reception_uses_same_ledger_and_no_legacy_loop(self):
        self.publish("开始接待", options={"mock": True, "headcount": 4, "reflect": False})
        runtime = self.service.runtime
        self.assertEqual(runtime.state, "succeeded")
        self.assertEqual(runtime.port.world.w_count_cola("会议室"), 4)
        self.assertEqual(len(runtime.record["dispatch_counts"]), 14)
        self.assertFalse(self.reflection.called)

    def test_mock_false_place_claim_stops_without_retry(self):
        self.publish("开始接待", options={"mock": True, "scenario": "place_miss"})
        runtime = self.service.runtime
        self.assertEqual(runtime.state, "recovery_required")
        self.assertEqual(runtime.port.world.w_count_cola("会议室"), 1)
        self.assertEqual(len(runtime.record["dispatch_counts"]), 5)

    def test_restart_generic_queries_original_not_body_adapter(self):
        self.port._perform = Mock(side_effect=TimeoutError("lost reply"))
        self.publish()
        command = self.service.runtime.record["open_command_id"]
        self.service.runtime = None
        self.port = DeskAdapter(perform=Mock(side_effect=AssertionError("resubmitted")), world=lambda: self.world, zones=self.zones)
        with self.assertRaisesRegex(Rejected, "停止或资源释放尚未确认"):
            self.publish(resume=True)
        self.assertEqual(self.service.runtime.record["open_command_id"], command)
        self.assertEqual(self.service.runtime.state, "recovery_required")
        self.assertFalse(self.port._perform.called)



    def test_external_cancel_during_planning_is_not_blocked_by_admission_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = self.model.forward
        def slow(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        self.model.forward = slow
        worker = threading.Thread(target=self.publish)
        worker.start()
        self.assertTrue(entered.wait(2))
        result = []
        cancel = threading.Thread(target=lambda: result.append(self.service.control('cancel')))
        cancel.start()
        cancel.join(2)
        release.set()
        worker.join(5)
        cancel.join(5)
        self.assertTrue(result[0]["completed"])
        self.assertEqual(self.actions, [])

    def test_pause_during_planning_keeps_plan_for_continue(self):
        entered, release = threading.Event(), threading.Event()
        original = self.model.forward
        def slow(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        self.model.forward = slow
        worker = threading.Thread(target=self.publish)
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.service.control('pause')["state"], "paused")
        release.set()
        worker.join(5)
        self.assertEqual(self.service.runtime.state, "paused")
        self.assertTrue(self.service.runtime.record["plan_ready"])
        self.assertEqual(self.actions, [])
        self.service.control('continue')
        self.assertEqual(self.service.runtime.state, "succeeded")

    def test_continue_restarts_dispatch_after_pause(self):
        self.brain._launch = lambda runtime: None
        self.publish()
        self.assertEqual(self.service.control('pause')["state"], "paused")
        self.brain._launch = self.brain._drive
        self.service.control('continue')
        self.assertEqual(self.service.status()["state"], "succeeded")
        self.assertEqual(len(self.actions), 2)

    def test_old_runtime_cannot_drive_a_new_task(self):
        self.publish()
        old = self.service.runtime
        self.publish("桌上有什么", task_id="task-next")
        with self.assertRaises(Rejected):
            old.drive()
        self.assertEqual(len(self.actions), 2)

    def test_restart_checks_original_endpoint_before_query(self):
        self.publish("开始接待")
        self.service.runtime = None
        self.service.config["reception_real"]["dream_base_url"] = "http://different-host"
        with self.assertRaisesRegex(Rejected, "地址"):
            self.publish("开始接待", resume=True)



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
        from brain.adapters.slaver import SlaverTransport
        agent = SlaverTransport.__new__(SlaverTransport)
        agent._result_lock = threading.RLock()
        agent._inflight_by_robot = {}
        ready = threading.Event(); ready.set()
        agent._subscriptions = {"FQrobot": ready}
        agent.start = lambda: None
        agent.collaborator = Mock()
        def send(channel, text):
            body = json.loads(text)
            agent._handle_result(json.dumps({"robot_name": "FQrobot", "task_id": body["task_id"],
                "subtask_handle": body["task"], "subtask_result": "done", "status": "success"}))
        agent.collaborator.send = send
        port = SlaverAdapter(agent)
        request = {"body": {"robot_name": "FQrobot", "subtask": "未知动作"}, "deadline_sec": 1}
        port.submit("unique-command", request)
        with patch("execution.robot_api.client.get_scene", return_value={}):
            result = port.wait("unique-command", request)
        self.assertFalse(result["evidence"]["supports"])
        agent.collaborator.update_agent_busy.assert_called_with("FQrobot", False)

    def test_missing_place_observation_and_transport_failure_are_unknown(self):
        from brain.adapters.execution import _sim_view
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
        from contracts.intent import Intent, classify_task
        app = Flask(__name__)
        agent = Mock()
        agent.config = {"brain": {"scheduler": "runtime"}}
        agent._is_reception_task.return_value = False
        agent.current_task_id = "original"
        agent.publish_global_task.return_value = {"source": "kernel"}
        from brain.api.app import create_app
        app = create_app(Mock(), facade=agent)
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
        from brain.api.app import create_app
        app = create_app(Mock(), facade=agent)
        with app.test_client() as client:
            response = client.post("/api/reception/run", json={"headcount": 3, "scenario": "place_miss"})
        self.assertTrue(response.json["success"])
        kwargs = agent.publish_global_task.call_args.kwargs
        self.assertEqual(kwargs["options"]["mock"], True)
        self.assertEqual(kwargs["options"]["headcount"], 3)

    def test_deploy_demo_proxies_master_without_starting_a_subprocess(self):
        from entries.web.app import create_app
        client = Mock()
        client.request.return_value.content = b'{"success":true,"task_id":"same-task"}'
        client.request.return_value.status_code = 200
        client.request.return_value.headers = {'content-type': 'application/json'}
        response = create_app(client=client).test_client().post('/api/reception/run', json={'headcount': 3})
        self.assertEqual(response.json['task_id'], 'same-task')
        self.assertEqual(client.request.call_args.args, ('POST', '/api/reception/run'))
        self.assertEqual(client.request.call_args.kwargs['json'], {'headcount': 3})
