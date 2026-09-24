"""Execution selection, pinned to the task. No scheduler or task state lives here."""
from __future__ import annotations

import json
import time

from brain.adapters.execution import BodyAdapter, LookAdapter, SimAdapter, _unconfirmed
from contracts.tasks import CONTRACT_VERSION, Rejected


class ActiveRobotAPI:
    """Use the same robot_api implementation, pinning action AND observation to its active backend."""
    def __init__(self, name):
        from dataclasses import replace
        from execution.robot_api import client
        from execution.robot_api.runtime import RobotRuntime
        config = client._RUNTIME.config
        if config.active_backend != name:
            raise Rejected("执行后端已变化，不能恢复原命令")
        chosen = next((b for b in config.backends if b.name == name), None)
        if chosen is None:
            raise Rejected(f"缺少后端配置: {name}")
        self.runtime = RobotRuntime()
        self.runtime.config = replace(config, backends=[chosen], active_backend=name,
                                      navigation=None if name == "desk" else config.navigation)
        self.url = chosen.url

    def grasp_object(self, name):
        return self.runtime.execute("grasp_object", {"object_name": name, "mode": "ik"})

    def place_object(self, name, target):
        return self.runtime.execute("place_object", {"object_name": name, "target": target})

    def navigate_to(self, target):
        return self.runtime.execute("navigate_to", {"target": target, "yaw": None})

    def get_objects(self):
        return self.runtime.get_state("objects")

    def get_scene(self):
        return self.runtime.get_state("scene")

    def get_zones(self):
        import urllib.request
        with urllib.request.urlopen(self.url.rstrip("/") + "/zones", timeout=10) as response:
            return json.load(response)


class MotionGate:
    """Keep real execution disabled while still accepting tasks into Runtime."""
    contract_version = CONTRACT_VERSION

    def __init__(self, port, enabled):
        self.port, self.enabled = port, enabled

    @property
    def gate_open(self):
        return self.enabled and self.port.gate_open

    @property
    def gate_reason(self):
        return "real_execution_disabled" if not self.enabled else getattr(self.port, "gate_reason", "navigation_gate")

    def submit(self, command_id, request):
        if not self.enabled:
            return {"accepted": False, "error": self.gate_reason}
        return self.port.submit(command_id, request)

    def __getattr__(self, name):
        # Queries and requests to stop an original command remain available.
        return getattr(self.port, name)


class SlaverAdapter(SimAdapter):
    """Existing Redis execution substrate; correlate each call using its command identity.

    The legacy worker has no durable query/stop API. Cache misses remain UNKNOWN.
    A success sentence is not independent evidence of the requested effect.
    """
    def __init__(self, agent, observer=None):
        super().__init__()
        self.transport = agent
        self.observer = observer

    def _execute_view(self, command_id, request):
        body = request["body"]
        robot = body["robot_name"]
        # Register BEFORE send: a fast worker can return synchronously.
        slot = self.transport._begin_inflight(robot, command_id)
        self.transport.collaborator.update_agent_busy(robot, True)
        self.transport.collaborator.send(f"fqplanner_to_{robot}", json.dumps({
            "task_id": command_id, "task": body["subtask"], "order": "true",
        }, ensure_ascii=False))
        end = time.monotonic() + float(request.get("deadline_sec") or 120)
        while time.monotonic() < end:
            with self.transport._result_lock:
                if slot["got_result"]:
                    break
            time.sleep(0.02)
        result = self.transport._consume_inflight(robot, slot)
        if not result["got_result"]:
            return _unconfirmed(command_id, "slaver_result_unavailable")
        status = result["status"]
        if status not in {"success", "navigated", "failure"}:
            return _unconfirmed(command_id, str(result.get("result") or status))
        from execution.robot_api import client
        scene = (self.observer or client).get_scene()
        effect = verify_scene(body["subtask"], scene)
        evidence = {"supports": effect is True and status in {"success", "navigated"},
                    "contradicts": effect is False or status == "failure",
                    "identity_ok": True, "time_ok": True, "grade": "object",
                    "identity": command_id, "detail": str(result["result"]), "scene": scene}
        stopped = status in {"success", "navigated"} and effect is True
        return {"command_id": command_id, "terminal": "failed" if status == "failure" else "succeeded",
                "started": True, "stopped": stopped, "resources_released": stopped, "evidence": evidence}


def verify_scene(subtask, scene):
    """Read actual scene fields. Missing evidence is None, never a successful assertion."""
    import re
    from brain.adapters.execution import parse_sim_action
    if not isinstance(scene, dict) or scene.get("success") is False:
        return None
    action = parse_sim_action(subtask)
    holding = scene.get("holding", (scene.get("robot") or {}).get("holding"))
    objects = scene.get("objects") or {}
    fixtures = scene.get("fixtures") or {}
    if action and action[0] == "grasp":
        obj = objects.get(action[1]) or {}
        if "grasped" in obj:
            return obj["grasped"] is True
        if "holding" in scene:
            actual = str(holding or "").replace("_", " ").lower()
            wanted = action[1].replace("_", " ").lower()
            return actual == wanted or (not re.search(r"\d", wanted) and re.fullmatch(re.escape(wanted) + r" \d+", actual) is not None)
    if action and action[0] == "place":
        obj = objects.get(action[1]) or {}
        target = fixtures.get(action[2]) or {}
        pos, center, size = obj.get("pos"), target.get("pos"), target.get("size")
        if obj.get("grasped") is False and pos and center and size and min(size[:2]) > 0:
            # Same fixture footprint used by the existing place tool; do not infer
            # placement when the scene lacks the object or the target dimensions.
            return (abs(pos[0] - center[0]) <= size[0] / 2 + .05
                    and abs(pos[1] - center[1]) <= size[1] / 2 + .05
                    and abs(pos[2] - (center[2] + size[2] / 2)) <= .15)
    if action and action[0] == "navigate":
        at = scene.get("robot_at", (scene.get("robot") or {}).get("at"))
        if at is not None:
            return str(at).replace("_", " ") == action[1].replace("_", " ")
        target = fixtures.get(action[1]) or objects.get(action[1]) or {}
        pos, center = (scene.get("robot") or {}).get("base_pos"), target.get("pos")
        if pos and center:
            import math
            return math.dist(pos[:2], center[:2]) <= 1.0
    # ALFWorld exposes fresh textual observations; match the requested operation,
    # object and target, rather than accepting arbitrary worker success prose.
    observation = str(scene.get("observation") or "").lower()
    raw = re.search(r"raw_action\s*[:：]\s*(.+)", subtask, re.I)
    if raw:
        command = raw.group(1).strip().lower()
        parts = command.split()
        if len(parts) >= 3:
            obj = " ".join(parts[1:3])
            verb = parts[0]
            if verb in {"clean", "heat", "cool", "slice", "open", "close", "toggle"}:
                past = {"clean": "clean", "heat": "heat", "cool": "cool", "slice": "slice",
                        "open": "open", "close": "close", "toggle": "toggle"}[verb]
                if f"you {past}" in observation and obj in observation:
                    return True
            if verb in {"move", "put"} and " to " in command:
                target = command.split(" to ", 1)[1]
                if "you put" in observation and obj in observation and target in observation:
                    return True
    return None


class DeskCheckAdapter:
    contract_version = CONTRACT_VERSION
    gate_open = True

    def __init__(self, sim):
        self.sim = sim

    def submit(self, command_id, request):
        return {"accepted": True}

    def wait(self, command_id, request):
        from execution.robot_api.desk import SKILLS, pending_objects
        world, zones = self.sim._read_world(), self.sim._read_zones()
        if not world or not zones or any(spec["zone"] not in zones for spec in SKILLS.values()):
            return _unconfirmed(command_id, "desk_observation_unavailable")
        scope = (request.get("body") or {}).get("subtask")
        names = [scope] if scope in SKILLS else list(SKILLS)
        pending = {name: pending_objects(name, world, zones) for name in names}
        ok = not any(pending.values()) and not any(item.get("grasped") is True for item in world.values())
        return {"command_id": "", "terminal": "succeeded" if ok else "failed",
                "evidence": {"supports": ok, "contradicts": not ok, "identity_ok": True,
                             "time_ok": True, "effect": "desk_tidy", "detail": str(pending)}}

    query = wait


class DeskAdapter(SimAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.check = DeskCheckAdapter(self)

    def submit(self, command_id, request):
        if request.get("skill") == "desk_check":
            return self.check.submit(command_id, request)
        return super().submit(command_id, request)

    def wait(self, command_id, request):
        if request.get("skill") == "desk_check":
            return self.check.wait(command_id, request)
        return super().wait(command_id, request)


def select_backend(config, package, *, mock=False):
    import os
    from shared.execution_profile import applied_profile
    profile = applied_profile()
    if profile:
        name = {'look': 'observation', 'reception': 'reception'}.get(package, 'execution')
        route = profile['routes'][name]
        if not route['available']:
            raise Rejected(route['reason'])
        if package == 'reception':
            return 'reception_mock' if mock else route['backend']
        if name == 'execution':
            return 'desk' if route['backend'] == 'desk' else 'slaver:' + route['backend']
    if package == "look":
        return "camera"
    if package == "reception":
        if mock:
            return "reception_mock"
        brain = config.get("brain") or {}
        mode = os.environ.get("RECEPTION_MODE") or brain.get("reception_backend")
        mode = mode or ("real" if (config.get("reception_real") or {}).get("enabled") else "mock")
        if mode not in {"real", "mock"}:
            raise Rejected(f"不支持的接待后端: {mode}")
        return "reception_" + mode
    from execution.robot_api import client
    backend = client._RUNTIME.config.active_backend
    if not backend:
        raise Rejected("没有配置执行后端")
    return "desk" if backend == "desk" else "slaver:" + backend


def target_identity(config, backend):
    from shared.execution_profile import applied_profile
    profile = applied_profile()
    if backend == 'camera' and profile:
        return {'backend': 'camera', 'observation': profile['routes']['observation']}
    if backend == "reception_real":
        real = config.get("reception_real") or {}
        return {"dream": real.get("dream_base_url"), "vla": real.get("vla_base_url")}
    if backend == "desk" or backend.startswith("slaver:"):
        from execution.robot_api import client
        cfg = client._RUNTIME.config
        return {"active_backend": cfg.active_backend,
                "urls": {b.name: b.url for b in cfg.backends if b.name == cfg.active_backend}}
    return {"backend": backend}


def build_port(config, backend, *, agent=None):
    from brain.config_flags import kernel_enabled
    if backend == "camera":
        return LookAdapter()
    if backend == "reception_real":
        return MotionGate(BodyAdapter.from_config(config), kernel_enabled(config))
    if backend == "reception_mock":
        from brain.packages.reception_mock import MockAdapter
        return MockAdapter()
    if backend == "desk":
        return DeskAdapter(api=ActiveRobotAPI("desk"))
    if backend.startswith("slaver:") and agent is not None:
        from execution.robot_api import client
        if client._RUNTIME.config.active_backend != backend.split(":", 1)[1]:
            raise Rejected("执行后端已变化，不能把原命令交给另一台执行器")
        port = SlaverAdapter(agent, observer=ActiveRobotAPI(backend.split(":", 1)[1]))
        if backend in {"slaver:real", "slaver:dream"}:
            return MotionGate(port, kernel_enabled(config))
        return port
    raise Rejected(f"无法恢复执行适配器: {backend}")
