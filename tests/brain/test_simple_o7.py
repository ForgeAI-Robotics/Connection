"""Remote executor contract, persistence and Runtime failure boundaries; no GPU required."""
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError

from brain.adapters.simple_o7 import SimpleO7Adapter, WIRE_VERSION, plan_steps
from brain.adapters.planning_inputs import PlanningInputs
from brain.adapters.ports import select_backend, build_port
from brain.storage.tasks import KernelStore
from contracts.tasks import Rejected
from shared.execution_profile import resolve
from tests.factories import TaskRuntime
from simulation.backends.simple_o7.service.server import Bridge, handler, scene
from simulation.backends.simple_o7.service.store import Journal, Conflict
from simulation.backends.simple_o7.service.worker import run_stage


GOOD_RESULT = {"pick_hold_passed": True, "lifted": True, "stop_reason": "completed",
               "terminal_stable_hold_s": 3.2, "lowering_verified": True,
               "maximum_guarded_penetration_m": .001}
GOOD_OBSERVATION = {"held": True, "supported": False, "can_position": [1, 1, 1], "object_speed_m_s": .01}


class SimpleO7Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        manifest = self.source / "data/scenes/g1_coke_tabletop/manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"scene_id": "g1_coke_tabletop", "asset_version": "test",
                                        "assets": {"coke": {"pose": [1, 1, 1], "fixed": False}}}))
        self.config = {"source": str(self.source), "runtime": str(self.root / "remote")}
        self.launches = []
        self.bridge = Bridge(self.config, launcher=self.launches.append)
        serve = handler(self.bridge, "test-token")
        serve.log_message = lambda *args: None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = SimpleO7Adapter(f"http://127.0.0.1:{self.server.server_port}", token="test-token", poll_interval=.01)
        self.request = {"skill": "sim", "task_id": "simple-task-1", "step_id": "STEP_1",
                        "deadline_sec": .05, "body": {"subtask": "抓取 coke"}}

    def wire(self, command="simple-1", task="simple-task-1"):
        return {"contract_version": WIRE_VERSION, "command_id": command, "task_id": task,
                "step_id": "STEP_1", "action": "pick_hold_coke", "object_id": "coke",
                "scene_revision": scene(self.source)["scene_revision"]}

    def finish(self, command, **changes):
        self.bridge.store.update(command, state="succeeded", started=True, stopped=True,
                                 resources_released=True, result=GOOD_RESULT, observation=GOOD_OBSERVATION, **changes)

    def runtime(self, folder="brain"):
        runtime = TaskRuntime(KernelStore(self.root / folder), self.port, package="generic")
        step = plan_steps({"subtask_list": [{"subtask": "抓取 coke"}]}, deadline=.05).steps
        runtime.open_task("simple-task-1", task_desc="抓取可乐", phases=step, execution_backend="simple_o7")
        return runtime

    def test_planning_reads_remote_capabilities_without_local_robot_state(self):
        context = PlanningInputs({}).snapshot("抓取可乐", "simple_o7", self.port)
        self.assertEqual(context.scene["scope"], "new_episode_template_not_live_camera")
        self.assertEqual(set(context.scene["objects"]), {"coke"})
        self.assertNotIn("milk", str(context))
        self.assertNotIn("counter", str(context))

    def test_unsupported_plan_rejected_before_any_command(self):
        for plan in [["放置 coke 到 table"], ["抓取 coke", "放置 coke 到 table"], ["抓取 milk_1"],
                     ["抓取 coke 然后放置"]]:
            with self.assertRaises(Rejected):
                plan_steps({"subtask_list": [{"subtask": text} for text in plan]})
        self.assertEqual(self.launches, [])

    def test_duplicate_submission_is_atomic_and_rejects_different_payload(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda _: self.bridge.submit(self.wire()), range(10)))
        self.assertEqual(self.launches, ["simple-1"])
        with self.assertRaises(Conflict):
            self.bridge.submit(dict(self.wire(), step_id="STEP_2"))

    def test_same_episode_never_resets_and_uncleared_command_blocks_next_task(self):
        self.bridge.submit(self.wire())
        with self.assertRaises(Conflict):
            self.bridge.submit(self.wire("new-command", "task-2"))
        self.finish("simple-1")
        with self.assertRaisesRegex(Conflict, "episode_already_used"):
            self.bridge.submit(self.wire("simple-1-r1"))
        self.bridge.submit(self.wire("new-command", "task-2"))
        self.assertEqual(len(self.launches), 2)

    def test_timeout_and_brain_restart_query_original_without_resubmitting(self):
        runtime = self.runtime()
        runtime.drive()
        self.assertEqual(runtime.state, "recovery_required")
        command = runtime.record["open_command_id"]
        self.assertEqual(self.launches, [command])
        self.bridge = Bridge(self.config, launcher=self.launches.append)
        self.assertEqual(self.bridge.store.get(command)["state"], "queued")
        reopened = TaskRuntime(KernelStore(self.root / "brain"), self.port)
        reopened.resume()
        self.assertEqual(self.launches, [command])
        self.assertEqual(reopened.state, "recovery_required")

    def test_lost_submit_response_is_queryable(self):
        self.bridge.submit(self.wire())
        reopened = Bridge(self.config, launcher=self.launches.append)
        reopened.submit(self.wire())
        self.assertEqual(self.launches, ["simple-1"])
        self.finish("simple-1")
        self.assertTrue(self.port.query("simple-1", self.request)["evidence"]["supports"])

    def test_verified_physics_completes_same_runtime_and_establishes_holding(self):
        def launch(command):
            self.launches.append(command)
            self.finish(command)
        self.bridge.launcher = launch
        runtime = self.runtime()
        runtime.drive()
        self.assertEqual(runtime.state, "succeeded")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(runtime.belief("holding")["certain"], "coke")

    def test_physical_failure_cannot_establish_holding(self):
        def launch(command):
            self.bridge.store.update(command, state="failed", started=True, stopped=True, resources_released=True,
                                     result={"pick_hold_passed": False, "stop_reason": "lost_grip_or_supported"})
        self.bridge.launcher = launch
        runtime = self.runtime()
        runtime.drive()
        self.assertEqual(runtime.state, "recovery_required")
        self.assertIsNone(runtime.belief("holding")["certain"])

    def test_claimed_success_without_physical_evidence_stays_unknown(self):
        self.bridge.submit(self.wire())
        self.bridge.store.update("simple-1", state="succeeded", started=True, stopped=True, resources_released=True,
                                 result={"pick_hold_passed": True}, observation={"hand_closed": True})
        view = self.port.query("simple-1", self.request)
        self.assertFalse(view["evidence"]["supports"])

    def test_foreign_identity_does_not_confirm_stop_or_success(self):
        self.bridge.submit(self.wire())
        self.finish("simple-1")
        view = self.port.query("simple-1", dict(self.request, task_id="foreign"))
        self.assertFalse(view["stopped"])
        self.assertTrue(view["timed_out"])

    def test_cancel_acceptance_does_not_fabricate_completion(self):
        self.port.submit("simple-1", self.request)
        reply = self.port.cancel("simple-1", self.request)
        self.assertTrue(reply["accepted"])
        self.assertFalse(reply["completed"])
        self.assertFalse(self.port.query("simple-1", self.request)["stopped"])
        self.bridge.store.update("simple-1", state="cancelled", stopped=True, resources_released=True)
        view = self.port.query("simple-1", self.request)
        self.assertTrue(view["stopped"])
        self.assertEqual(view["terminal"], "cancelled")

    def test_worker_cancellation_stops_its_own_subprocess(self):
        self.bridge.submit(self.wire())
        results = []
        thread = threading.Thread(target=lambda: results.append(run_stage(
            self.bridge.store, "simple-1", [sys.executable, "-c", "import time;time.sleep(20)"],
            self.root / "child.log", timeout=10)))
        thread.start()
        time.sleep(.1)
        self.bridge.store.cancel("simple-1")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["cancelled"])

    def test_token_required_and_missing_command_remains_unknown(self):
        self.assertTrue(self.port.query("missing", self.request)["timed_out"])
        with self.assertRaises(HTTPError) as raised:
            SimpleO7Adapter(self.port.url, token="wrong").planning_input("抓取可乐")
        self.assertEqual(raised.exception.code, 401)

    def test_stale_scene_is_rejected_and_finished_record_is_immutable(self):
        with self.assertRaisesRegex(Conflict, "scene_revision"):
            self.bridge.submit(dict(self.wire(), scene_revision="old"))
        self.bridge.submit(self.wire())
        self.finish("simple-1")
        self.bridge.store.cancel("simple-1")
        self.bridge.store.update("simple-1", state="failed")
        self.assertEqual(self.bridge.store.get("simple-1")["state"], "succeeded")

    def test_routing_uses_direct_port_and_does_not_enable_real_reception(self):
        profile = resolve({"simulation_backend": "simple_o7"}, {"backends": {"simple_o7": {"url": self.port.url}}})
        self.assertFalse(profile["routes"]["observation"]["available"])
        self.assertEqual(profile["routes"]["reception"]["backend"], "reception_mock")
        with patch("shared.execution_profile.applied_profile", return_value=profile):
            self.assertEqual(select_backend({}, "generic"), "simple_o7")
            with self.assertRaises(Rejected): select_backend({}, "desk")
            with patch("brain.adapters.simple_o7.settings", return_value=SimpleNamespace(url=self.port.url, timeout=900)):
                self.assertIsInstance(build_port({}, "simple_o7"), SimpleO7Adapter)

    def test_remote_backend_switch_does_not_start_slaver_or_remote_services(self):
        import yaml
        from ops.execution import Switcher
        from tests.ops.test_execution import FakeServices
        (self.root / 'config').mkdir()
        (self.root / 'config/brain.yaml').write_text('brain: {scheduler: runtime}\n')
        (self.root / 'config/robot_api.yaml').write_text(yaml.safe_dump({
            'backends': {'simple_o7': {'url': self.port.url}}}))
        services = FakeServices()
        services.up = {'master', 'deploy', 'feishu'}
        Switcher(self.root, services).apply({'simulation_backend': 'simple_o7'})
        self.assertEqual([name for operation, name in services.calls if operation == 'start'], ['master'])

    def test_cancel_can_clear_an_unknown_result_only_after_confirmed_stop(self):
        self.bridge.submit(self.wire())
        self.bridge.store.update('simple-1', state='unknown', stopped=False)
        self.bridge.store.cancel('simple-1')
        self.assertEqual(self.bridge.store.get('simple-1')['state'], 'cancelling')
        self.bridge.store.update('simple-1', state='unknown', stopped=True, resources_released=True)
        self.bridge.store.cancel('simple-1')
        self.assertEqual(self.bridge.store.get('simple-1')['state'], 'cancelled')

    def test_no_eligible_endpoint_never_starts_motion_or_fabricates_observation(self):
        from simulation.backends.simple_o7.service.worker import execute
        self.bridge.submit(self.wire())
        stages = []
        def stage(journal, command_id, command, log, **kwargs):
            stages.append(Path(log).stem)
            if stages[-1] == 'prepare':
                folder = Path(command[-1]); folder.mkdir()
            else:
                folder = Path(command[-1])
                (folder / 'reachability.json').write_text(json.dumps({'candidates': [{'endpoint_eligible': False}]}))
            return 'ok'
        with patch('simulation.backends.simple_o7.service.worker.source_fingerprint', return_value={}), \
             patch('simulation.backends.simple_o7.service.worker.run_stage', side_effect=stage):
            execute(self.config, 'simple-1')
        record = self.bridge.store.get('simple-1')
        self.assertEqual(stages, ['prepare', 'reachability'])
        self.assertFalse(record['started'])
        self.assertEqual(record['error'], 'no_eligible_grasp_endpoint')
        self.assertTrue(record['stopped'])
        self.assertEqual(record['observation'], {})


if __name__ == "__main__":
    unittest.main()
