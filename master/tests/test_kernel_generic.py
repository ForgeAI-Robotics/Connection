"""Generic plans run on the same runtime. Simulation is the execution port."""

import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request


MASTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.abspath(os.path.join(MASTER_DIR, ".."))
for _path in (ROOT, MASTER_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from kernel.adapters import SimAdapter
from kernel.flags import scheduler_runtime
from kernel.packages.generic import steps_from_subtasks
from kernel.runtime import TaskRuntime
from kernel.store import KernelStore
from kernel.switch import control_task
from tests.test_kernel_reception import FakeBody


PLAN = [
    {"robot_name": "FQrobot", "subtask": "抓取 milk_1", "subtask_order": 1},
    {"robot_name": "FQrobot", "subtask": "放置 milk_1 到 milk_area", "subtask_order": 2},
]


def _generic(root, port, task_id="task-abcdef123456"):
    runtime = TaskRuntime(
        KernelStore(root),
        port,
        config={"reception_real": {"dream_inspection_enabled": False}},
        package="generic",
    )
    runtime.open_task(task_id, task_desc="把牛奶放到牛奶区", phases=steps_from_subtasks(PLAN))
    return runtime


class GenericRuntimeTests(unittest.TestCase):
    def test_scheduler_defaults_to_runtime_and_legacy_stays_available(self):
        self.assertTrue(scheduler_runtime({}))
        self.assertTrue(scheduler_runtime({"brain": {"scheduler": "runtime"}}))
        self.assertFalse(scheduler_runtime({"brain": {"scheduler": "legacy"}}))

    def test_plan_becomes_steps_without_a_second_state_machine(self):
        steps = steps_from_subtasks(PLAN)
        self.assertEqual([step.step_id for step in steps], ["STEP_1", "STEP_2"])
        self.assertTrue(all(step.kind == "sim" and step.body for step in steps))

    def test_sim_success_stays_on_one_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            runtime = _generic(root, port)
            runtime.drive()
            self.assertEqual(runtime.state, "succeeded")
            self.assertEqual(port.submits, [
                "sim-1-abcdef123456",
                "sim-2-abcdef123456",
            ])
            self.assertFalse(any("-r" in item for item in port.submits))
            self.assertEqual(runtime.record["package"], "generic")

    def test_sim_failure_does_not_dispatch_the_next_step(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["STEP_1"] = {
                "terminal": "failed",
                "evidence": {
                    "supports": False,
                    "contradicts": True,
                    "identity_ok": True,
                    "time_ok": True,
                },
            }
            runtime = _generic(root, port)
            runtime.drive()
            self.assertEqual(runtime.state, "recovery_required")
            self.assertEqual(port.submits, ["sim-1-abcdef123456"])
            self.assertEqual(runtime.dispatch_count("STEP_2"), 0)

    def test_cancel_before_motion_clears_the_generic_task(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = _generic(root, FakeBody())
            payload, runtime = control_task(
                {
                    "reception_real": {
                        "kernel_enabled": False,
                        "kernel_runtime_dir": root,
                    },
                },
                "cancel",
                runtime=runtime,
            )
            self.assertEqual(payload["state"], "cancelled")
            self.assertTrue(payload["completed"])

    def test_restart_queries_the_original_sim_command(self):
        with tempfile.TemporaryDirectory() as root:
            port = FakeBody()
            port.outcomes["STEP_1"] = {"timed_out": True, "started": None, "stopped": False}
            runtime = _generic(root, port)
            runtime.drive()
            self.assertEqual(runtime.state, "recovery_required")
            command_id = runtime.record["open_command_id"]
            self.assertEqual(command_id, "sim-1-abcdef123456")
            restarted = TaskRuntime(KernelStore(root), port)
            self.assertEqual(restarted.package_name, "generic")
            self.assertEqual([step.step_id for step in restarted.phases], ["STEP_1", "STEP_2"])
            before = list(port.submits)
            restarted.resume()
            self.assertEqual(port.submits, before)
            self.assertIn(command_id, port.queries)
            self.assertEqual(restarted.state, "recovery_required")


class DeskSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from simulation.backends.desk.main import ZONES, app, _reset_world

        cls.zones = ZONES
        _reset_world()
        from werkzeug.serving import make_server
        cls.server = make_server("127.0.0.1", 0, app, threaded=True)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{self.port}/reset",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=2,
        )
        from robot_api.config import BackendConfig, RobotApiConfig
        from robot_api import client

        previous = client._RUNTIME.config
        self.addCleanup(setattr, client._RUNTIME, "config", previous)
        client._RUNTIME.config = RobotApiConfig(
            backends=[
                BackendConfig(
                    name="desk",
                    enabled=True,
                    provide_state=True,
                    accept_action=True,
                    required=True,
                    url=f"http://127.0.0.1:{self.port}",
                    timeout=10,
                )
            ],
            active_backend="desk",
        )

    def _drive(self, root, task_id):
        runtime = _generic(root, SimAdapter(zones=self.zones), task_id)
        runtime.drive()
        return runtime

    def test_desk_moves_milk_into_its_zone(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = self._drive(root, "task-milkdemo0001")
            self.assertEqual(runtime.state, "succeeded")
            from robot_api import client

            milk = client.get_objects()["milk_1"]
            zone = self.zones["milk_area"]
            dx = milk["pos"][0] - zone["pos"][0]
            dy = milk["pos"][1] - zone["pos"][1]
            self.assertFalse(milk["grasped"])
            self.assertLessEqual((dx * dx + dy * dy) ** 0.5, zone["radius"] + 0.05)
            print(
                "仿真闭环:",
                runtime.state,
                "milk_1",
                milk["pos"],
                "命令",
                [
                    item.get("command_id")
                    for bucket in runtime.record["steps"].values()
                    for item in bucket["attempts"]
                ],
            )

    def test_desk_grasp_fault_stops_before_place(self):
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{self.port}/fault",
                data=b'{"next":"grasp_fail"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=2,
        )
        with tempfile.TemporaryDirectory() as root:
            runtime = self._drive(root, "task-milkfail0001")
            self.assertEqual(runtime.state, "recovery_required")
            self.assertEqual(runtime.dispatch_count("STEP_2"), 0)
            from robot_api import client

            self.assertIsNot(client.get_objects()["milk_1"].get("grasped"), True)
