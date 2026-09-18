import json
import logging
import os
import sys
import tempfile
import types
import unittest


MASTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO_ROOT = os.path.abspath(os.path.join(MASTER_DIR, ".."))
for path in (MASTER_DIR, REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)


def _install_import_stubs():
    """Allow importing GlobalAgent without the live Redis/OpenAI stack."""
    if "dotenv" not in sys.modules:
        dotenv_mod = types.ModuleType("dotenv")
        dotenv_mod.load_dotenv = lambda *a, **k: True
        sys.modules["dotenv"] = dotenv_mod
    if "openai" not in sys.modules:
        openai_mod = types.ModuleType("openai")
        openai_mod.AzureOpenAI = object
        openai_mod.OpenAI = object
        sys.modules["openai"] = openai_mod
    if "redis" not in sys.modules:
        redis_error = type("RedisError", (Exception,), {})
        exceptions = types.SimpleNamespace(
            ConnectionError=redis_error,
            RedisError=redis_error,
            TimeoutError=redis_error,
        )
        redis_mod = types.ModuleType("redis")
        redis_mod.ConnectionPool = object
        redis_mod.Redis = object
        redis_mod.exceptions = exceptions
        sys.modules["redis"] = redis_mod
        sys.modules["redis.exceptions"] = exceptions
    if "robot_api" not in sys.modules:
        sys.modules["robot_api"] = types.ModuleType("robot_api")
    if "robot_api.config" not in sys.modules:
        cfg_mod = types.ModuleType("robot_api.config")
        cfg_mod.load_robot_api_config = lambda: types.SimpleNamespace(active_backend="")
        sys.modules["robot_api.config"] = cfg_mod
    if "robot_api.client" not in sys.modules:
        client_mod = types.ModuleType("robot_api.client")
        client_mod.get_objects = lambda: []
        client_mod.get_scene = lambda: {}
        client_mod.check_success = lambda: {}
        sys.modules["robot_api.client"] = client_mod


_install_import_stubs()

from agents.agent import GlobalAgent, TaskQueue  # noqa: E402
from sop.reception_store import ReceptionStore  # noqa: E402


class FakeCollaborator:
    def __init__(self):
        self.busy = {}
        self.cleared = []
        self.published = []
        self.free_on_wait = True

    def update_agent_busy(self, name, busy):
        self.busy[name] = bool(busy)
        if not busy:
            self.cleared.append(name)
        return True

    def wait_agents_free(self, names, timeout=None, abort=None, check_interval=0.5):
        del names, check_interval
        if abort is not None and abort():
            return False
        if timeout is not None and timeout <= 0:
            return False
        return self.free_on_wait

    def send(self, channel, message):
        self.published.append((channel, message))
        return True

    def read_all_agents_name(self):
        return ["FQrobot"]

    def clear_agent_status(self, name):
        return True


class ExecutionBaseTests(unittest.TestCase):
    def _agent(self, *, runtime_dir=None):
        agent = GlobalAgent.__new__(GlobalAgent)
        agent.logger = logging.getLogger("execution-base-test")
        agent.logger.addHandler(logging.NullHandler())
        agent.config = {
            "execution": {
                "subtask_wait_timeout_sec": 2,
                "camera_wait_timeout_sec": 1,
            },
            "reception_real": {
                "runtime_dir": runtime_dir or tempfile.mkdtemp(),
            },
        }
        agent.collaborator = FakeCollaborator()
        agent.current_task_id = "task-current"
        agent.current_task_desc = "开始接待"
        agent.current_reasoning = ""
        agent.current_task_queue = None
        agent.terminated_tasks = set()
        agent._reception_running = False
        agent._reception_state = None
        agent._dispatch_running = False
        agent._dispatch_token = 0
        agent._last_subtask_status = None
        agent._last_subtask_result = None
        agent._result_lock = __import__("threading").Lock()
        agent._inflight_by_robot = {}
        return agent

    def test_stale_result_does_not_clear_busy(self):
        agent = self._agent()
        agent._begin_inflight("FQrobot", "task-current")
        agent.collaborator.update_agent_busy("FQrobot", True)
        agent.collaborator.cleared.clear()
        agent._handle_result(json.dumps({
            "robot_name": "FQrobot",
            "subtask_handle": "nav",
            "subtask_result": "old success",
            "task_id": "task-old",
            "status": "success",
        }))
        self.assertEqual([], agent.collaborator.cleared)
        self.assertTrue(agent.collaborator.busy["FQrobot"])
        self.assertFalse(agent._inflight_by_robot["FQrobot"]["got_result"])

    def test_missing_result_is_unknown_not_success(self):
        agent = self._agent()
        agent.collaborator.free_on_wait = True
        status, result = agent._wait_for_subtask_result(
            "FQrobot", "task-current", timeout=2
        )
        self.assertEqual("unknown", status)
        self.assertIn("没有匹配", result)
        self.assertEqual("unknown", agent._last_subtask_status)

    def test_wait_timeout_is_timeout_not_success(self):
        agent = self._agent()
        agent.collaborator.free_on_wait = False
        status, result = agent._wait_for_subtask_result(
            "FQrobot", "task-current", timeout=0
        )
        self.assertEqual("timeout", status)
        self.assertIn("超时", result)

    def test_matching_result_releases_busy(self):
        agent = self._agent()
        slot = agent._begin_inflight("FQrobot", "task-current")
        agent.collaborator.update_agent_busy("FQrobot", True)
        agent._handle_result(json.dumps({
            "robot_name": "FQrobot",
            "subtask_handle": "nav",
            "subtask_result": "ok",
            "task_id": "task-current",
            "status": "success",
        }))
        self.assertEqual(["FQrobot"], agent.collaborator.cleared)
        self.assertTrue(slot["got_result"])
        self.assertEqual("success", slot["status"])

    def test_wait_uses_injected_result_instead_of_default_success(self):
        agent = self._agent()

        def wait_and_inject(*_args, **_kwargs):
            agent._handle_result(json.dumps({
                "robot_name": "FQrobot",
                "subtask_handle": "nav",
                "subtask_result": "reached",
                "task_id": "task-current",
                "status": "success",
            }))
            return True

        agent.collaborator.wait_agents_free = wait_and_inject
        status, result = agent._wait_for_subtask_result(
            "FQrobot", "task-current", timeout=2
        )
        self.assertEqual("success", status)
        self.assertEqual("reached", result)

    def test_incomplete_result_does_not_clear_busy(self):
        agent = self._agent()
        agent._begin_inflight("FQrobot", "task-current")
        agent.collaborator.update_agent_busy("FQrobot", True)
        agent.collaborator.cleared.clear()
        agent._handle_result(json.dumps({
            "robot_name": "FQrobot",
            "subtask_handle": "",
            "subtask_result": "",
            "task_id": "task-current",
            "status": "success",
        }))
        self.assertEqual([], agent.collaborator.cleared)

    def test_publish_rejects_persisted_recovery_without_force(self):
        with tempfile.TemporaryDirectory() as runtime_dir:
            ReceptionStore(runtime_dir).save_state({
                "task_id": "old-task",
                "state": "RECOVERY_REQUIRED",
                "holding": "cola_can_1",
            })
            agent = self._agent(runtime_dir=runtime_dir)
            result = agent.publish_global_task("开始接待", False, "new-task")
            self.assertTrue(result["ignored"])
            self.assertTrue(result["blocks_new_motion"])
            self.assertIn("RECOVERY_REQUIRED", result["error"])
            self.assertIsNone(agent.current_task_queue)

    def test_leftover_recovery_status_is_not_all_done(self):
        with tempfile.TemporaryDirectory() as runtime_dir:
            ReceptionStore(runtime_dir).save_state({
                "task_id": "old-task",
                "state": "RECOVERY_REQUIRED",
                "failure_reason": "relay2 failed",
            })
            agent = self._agent(runtime_dir=runtime_dir)
            status = agent.get_task_status()
            self.assertTrue(status["active"])
            self.assertFalse(status["all_done"])
            self.assertFalse(status["terminal"])
            self.assertTrue(status["blocks_new_motion"])
            self.assertEqual("RECOVERY_REQUIRED", status["state"])
            self.assertFalse(status["execution_active"])

    def test_blocked_remaining_are_not_counted_as_executed_failures(self):
        agent = self._agent()
        queue = TaskQueue([
            {"robot_name": "FQrobot", "subtask": "nav table2", "subtask_order": 1},
            {"robot_name": "FQrobot", "subtask": "pick", "subtask_order": 2},
            {"robot_name": "FQrobot", "subtask": "place", "subtask_order": 3},
        ])
        queue.mark_done(queue.tasks[0], status="timeout", result="lost")
        agent._block_remaining_tasks(queue, "前置无终态")
        agent.current_task_queue = queue
        agent.current_task_id = "task-current"
        status = agent.get_task_status()
        self.assertEqual("blocked", queue.tasks[1]["status"])
        self.assertEqual("blocked", queue.tasks[2]["status"])
        self.assertEqual(2, status["blocked_steps"])
        self.assertTrue(status["failed"])
        self.assertEqual(1, status["completed"])

    def test_reception_recovery_status_keeps_polling_fields(self):
        agent = self._agent()
        agent.current_task_queue = TaskQueue([])
        agent.current_task_queue.tasks.append({
            "order": 99,
            "robot_name": "FQrobot",
            "subtask": "任务进入人工恢复",
            "done": True,
            "status": "failure",
            "result": "nav failed",
            "inserted": False,
        })
        agent._reception_state = {
            "task_id": "task-current",
            "state": "RECOVERY_REQUIRED",
            "failure_reason": "nav failed",
        }
        status = agent.get_task_status()
        self.assertEqual("RECOVERY_REQUIRED", status["state"])
        self.assertFalse(status["all_done"])
        self.assertFalse(status["terminal"])
        self.assertTrue(status["blocks_new_motion"])
        self.assertFalse(status["can_resume"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
