"""Remote SIMPLE execution port. No task scheduler or low-level control lives here."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener

from brain.adapters.execution import _unconfirmed
from contracts.planning import PlanningInput, Plan
from contracts.steps import StepSpec
from contracts.tasks import CONTRACT_VERSION, Rejected

WIRE_VERSION = "connection/simple-o7/v1"


def settings():
    from execution.robot_api.config import load_robot_api_config
    config = load_robot_api_config()
    backend = next((b for b in config.backends if b.name == "simple_o7"), None)
    if backend is None or not backend.url:
        raise Rejected("请配置 simple_o7 仿真服务地址")
    return backend


def plan_steps(planned, deadline=900):
    """The existing backend supports one compound pick/hold episode, not placement."""
    items = planned.get("subtask_list") or []
    accepted = {"抓取 coke", "抓取可乐", "抓取 可乐", "抓取并稳定持有 coke", "抓取可乐并稳定持有"}
    if len(items) != 1 or items[0].get("subtask", "").strip() not in accepted:
        raise Rejected("simple_o7 当前只支持单步‘抓取 coke’（包含抬升和稳定持有）；不支持导航、放置或多步重置场景")
    return Plan((StepSpec("STEP_1", "sim", prefix="simple-o7-1", key="抓取 coke",
                         evidence="object_held", requires_object_evidence=True,
                         writes="in_gripper", object_id="coke", robot_name="FQrobot",
                         deadline_sec=deadline),), planned.get("reasoning_explanation", ""))


class SimpleO7Adapter:
    contract_version = CONTRACT_VERSION
    gate_open = True

    def __init__(self, url, *, token=None, timeout=5, poll_interval=.5, deadline=900):
        self.url = url.rstrip("/")
        self.token = os.environ.get("SIMPLE_O7_TOKEN", "") if token is None else token
        self.timeout, self.poll_interval, self.deadline = timeout, poll_interval, deadline

    @classmethod
    def from_config(cls):
        cfg = settings()
        return cls(cfg.url, deadline=cfg.timeout)

    def _call(self, path, body=None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data=data, headers=headers)
        # This explicit execution address is a direct LAN endpoint, not an LLM proxy.
        with build_opener(ProxyHandler({})).open(request, timeout=self.timeout) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("contract_version") != WIRE_VERSION:
            raise ValueError("simple_o7_contract_mismatch")
        return result

    def planning_input(self, task, experiences="", rules=()):
        scene = self._call("/v1/scene")
        if scene.get("capabilities") != ["pick_hold_coke"]:
            raise Rejected("simple_o7 能力合同不匹配")
        return PlanningInput(task, ("FQrobot",), {"FQrobot": {"tools": [
            "抓取 coke：单步完成抓可乐、抬升和稳定持有。subtask 必须准确写成‘抓取 coke’。"
            "没有导航、放置、其他物体抓取或桌面整理能力；不支持的任务返回空计划。"
        ]}}, scene, experiences, tuple(rules))

    def submit(self, command_id, request):
        body = request.get("body") or {}
        if request.get("skill") != "sim" or body.get("subtask") != "抓取 coke":
            return {"accepted": False, "error": "simple_o7_unsupported_action"}
        scene = self._call("/v1/scene")
        raw = self._call("/v1/commands", {
            "contract_version": WIRE_VERSION, "command_id": command_id,
            "task_id": request["task_id"], "step_id": request["step_id"],
            "action": "pick_hold_coke", "object_id": "coke",
            "scene_revision": scene["scene_revision"],
        })
        if not self._identity(raw, command_id, request):
            return {"accepted": False, "unclear": True, "error": "simple_o7_identity_mismatch"}
        return {"accepted": raw.get("accepted") is True, "error": raw.get("error", "")}

    @staticmethod
    def _identity(raw, command_id, request):
        return (raw.get("command_id") == command_id
                and raw.get("task_id") == request.get("task_id")
                and raw.get("step_id") == request.get("step_id")
                and raw.get("action") == "pick_hold_coke" and raw.get("object_id") == "coke")

    def query(self, command_id, request):
        try:
            raw = self._call("/v1/commands/" + quote(command_id, safe=""))
            return self._view(command_id, request, raw)
        except Exception as exc:
            return _unconfirmed(command_id, str(exc))

    def _view(self, command_id, request, raw):
        if not self._identity(raw, command_id, request):
            return _unconfirmed(command_id, "simple_o7_identity_mismatch")
        state = raw.get("state")
        terminal = state if state in {"succeeded", "failed", "cancelled"} else ""
        try:
            observed = datetime.fromisoformat(raw.get("updated_at", "").replace("Z", "+00:00"))
            submitted = datetime.fromisoformat(raw.get("created_at", "").replace("Z", "+00:00"))
            time_ok = observed.tzinfo is not None and submitted.tzinfo is not None and observed >= submitted
        except (ValueError, TypeError):
            time_ok = False
        result = raw.get("result") or {}
        sample = raw.get("observation") or {}
        # A planner success or closed hand is insufficient; require physical hold evidence.
        support = (result.get("pick_hold_passed") is True and result.get("lifted") is True
                   and result.get("stop_reason") == "completed"
                   and result.get("lowering_verified") is True
                   and isinstance(result.get("terminal_stable_hold_s"), (int, float))
                   and result["terminal_stable_hold_s"] >= 3
                   and isinstance(result.get("maximum_guarded_penetration_m"), (int, float))
                   and 0 <= result["maximum_guarded_penetration_m"] <= .002
                   and sample.get("held") is True and sample.get("supported") is False)
        stopped = raw.get("stopped") is True and raw.get("resources_released") is True
        return {"command_id": command_id, "terminal": terminal, "started": raw.get("started"),
                "stopped": stopped, "resources_released": stopped, "timed_out": False,
                "error": raw.get("error", ""),
                "evidence": {"identity": command_id, "identity_ok": True, "time_ok": time_ok,
                             "grade": "object", "effect": "object_held", "supports": support and stopped,
                             "contradicts": state == "failed" or result.get("pick_hold_passed") is False,
                             "result": result, "scene_observation": sample,
                             "diagnostics": raw.get("diagnostics") or {},
                             "episode_id": raw.get("episode_id"), "scene_revision": raw.get("scene_revision"),
                             "detail": raw.get("error") or result.get("stop_reason", state)}}

    def wait(self, command_id, request):
        end = time.monotonic() + float(request.get("deadline_sec") or self.deadline)
        while True:
            result = self.query(command_id, request)
            if result.get("terminal") or result.get("timed_out"):
                return result
            remaining = end - time.monotonic()
            if remaining <= 0:
                return _unconfirmed(command_id, "simple_o7_timeout_query_original_command")
            time.sleep(min(self.poll_interval, remaining))

    def cancel(self, command_id, request):
        raw = self._call("/v1/commands/" + quote(command_id, safe="") + "/cancel", {})
        ok = self._identity(raw, command_id, request)
        return {"accepted": ok and raw.get("cancel_accepted") is True,
                "completed": False, "command_id": command_id}

    def handoff(self, owner, request):
        return {"available": False, "confirmed": False, "error": "simple_o7_handoff_unsupported"}
