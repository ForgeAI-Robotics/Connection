"""Execution selection, pinned to the task. No scheduler or task state lives here."""
from __future__ import annotations

import json
import time

from brain.adapters.execution import BodyAdapter, LookAdapter, SimAdapter, _unconfirmed
from contracts.tasks import CONTRACT_VERSION, Rejected


class UnselectedPort:
    """A task can be cancelled while its package is being selected; no action port exists yet."""
    contract_version = CONTRACT_VERSION
    gate_open = False
    gate_reason = "execution_not_selected"


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
        velocity = (scene.get("robot") or {}).get("base_velocity") if isinstance(scene, dict) else None
        if velocity is not None:
            import math
            stopped = stopped and len(velocity) >= 3 and all(
                type(value) in (int, float) and math.isfinite(value) and abs(value) < .03 for value in velocity[:3])
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
        support = objects.get(action[2]) or {}
        if support and obj.get('grasped') is False and support.get('grasped') is False:
            import math
            lower, upper = (obj.get('bounds') or {}).get('min'), (obj.get('bounds') or {}).get('max')
            floor, ceiling = (support.get('bounds') or {}).get('min'), (support.get('bounds') or {}).get('max')
            bounds = (lower, upper, floor, ceiling)
            if all(isinstance(b, list) and len(b) == 3 and all(type(v) in (int, float) and math.isfinite(v) for v in b) for b in bounds):
                if any(lo[i] > hi[i] for lo, hi in ((lower, upper), (floor, ceiling)) for i in range(3)):
                    return None
                center = [(lower[i] + upper[i]) / 2 for i in range(2)]
                return (all(floor[i] - .02 <= center[i] <= ceiling[i] + .02 for i in range(2))
                        and -.02 <= lower[2] - ceiling[2] <= .05)
        target = fixtures.get(action[2]) or {}
        pos, center, size = obj.get("pos"), target.get("pos"), target.get("size")
        if obj.get("grasped") is False and pos and center and size and min(size[:2]) > 0:
            # Same fixture footprint used by the existing place tool; do not infer
            # placement when the scene lacks the object or the target dimensions.
            return (abs(pos[0] - center[0]) <= size[0] / 2 + .05
                    and abs(pos[1] - center[1]) <= size[1] / 2 + .05
                    and abs(pos[2] - (center[2] + size[2] / 2)) <= .15)
    if action and action[0] == "navigate":
        import math
        try:
            coords = [float(x.strip()) for x in action[1].strip("()").split(",")]
        except ValueError:
            coords = []
        if len(coords) in {2, 3} and all(math.isfinite(x) for x in coords):
            robot = scene.get("robot") or {}
            pos = robot.get("base_pos")
            if not pos or len(pos) < 2:
                return None
            near = math.dist(pos[:2], coords[:2]) <= .20
            if len(coords) == 3:
                if robot.get("yaw") is None:
                    return None
                near = near and abs((robot["yaw"] - coords[2] + 180) % 360 - 180) <= 10
            return near
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

    def restore_receipts(self, record):
        """Restore only identity-bound, confirmed-stop receipts already persisted by Runtime.

        Missing/lost replies remain unknown. A current scene alone never proves an old command.
        """
        from copy import deepcopy
        import threading
        if record.get("execution_backend") != "desk":
            return
        for step_id, bucket in (record.get("steps") or {}).items():
            for attempt in bucket.get("attempts") or []:
                progress = attempt.get("progress") or {}
                request = attempt.get("request") or {}
                evidence = progress.get("evidence") or {}
                command_id = attempt.get("command_id")
                body = request.get("body") or {}
                if not (command_id and attempt.get("submitted") is True
                        and progress.get("command_id") == body.get("command_id") == evidence.get("identity") == command_id
                        and progress.get("task_id") == request.get("task_id") == body.get("task_id") == record.get("task_id")
                        and progress.get("step_id") == request.get("step_id") == step_id
                        and progress.get("attempt_id") == attempt.get("attempt_id")
                        and progress.get("publisher_stopped") is True and evidence.get("resources_released") is True
                        and evidence.get("identity_ok") is True and evidence.get("time_ok") is True
                        and not progress.get("timed_out") and not progress.get("error")):
                    continue
                viewed = {"command_id": command_id, "terminal": progress.get("terminal") or "",
                          "started": progress.get("started"), "stopped": True, "resources_released": True,
                          "timed_out": False, "evidence": deepcopy(evidence)}
                viewed["evidence"]["receipt_source"] = "task_ledger"
                done = threading.Event()
                done.set()
                with self._commands_lock:
                    self.commands.setdefault(command_id, {"request": deepcopy(request), "view": viewed,
                                                          "started": True, "done": done})

    def _stationary_navigation(self, command_id, request, viewed):
        from brain.adapters.execution import parse_sim_action
        action = parse_sim_action((request.get("body") or {}).get("subtask"))
        proof = viewed.get("evidence") or {}
        # This exact receipt comes from Desk's non-moving /nav endpoint, not DREAM or a pose claim.
        if (action and action[0] == "navigate" and not viewed.get("terminal")
                and viewed.get("stopped") is True and viewed.get("resources_released") is True
                and proof.get("identity") == command_id and proof.get("identity_ok") is True
                and proof.get("time_ok") is True and proof.get("detail") == "定点 demo,无需导航(no-op)"
                and action[1] in self._read_zones()):
            viewed = dict(viewed, terminal="succeeded", evidence=dict(proof, supports=True, contradicts=False,
                          verification="desk_stationary_workspace"))
        return viewed

    def _execute_view(self, command_id, request):
        return self._stationary_navigation(command_id, request, super()._execute_view(command_id, request))

    def query(self, command_id, request):
        return self._stationary_navigation(command_id, request, super().query(command_id, request))

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
            if route['backend'] == 'simple_o7':
                if package != 'generic':
                    raise Rejected('simple_o7 当前仅支持通用任务中的单步抓取可乐，不支持桌面整理')
                return 'simple_o7'
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
    if backend == 'simple_o7':
        if package != 'generic':
            raise Rejected('simple_o7 当前仅支持通用任务中的单步抓取可乐')
        return backend
    return "desk" if backend == "desk" else "slaver:" + backend


SIMULATED_RECEPTION = {'reception_protocol': 'protocol', 'reception_simple': 'simple_physics'}


def protocol_endpoints(backend='reception_protocol'):
    from shared.execution_profile import applied_profile, PROTOCOL_URLS
    profile = applied_profile()
    route = (profile or {}).get('routes', {}).get('reception', {})
    endpoints = route.get('endpoints')
    if (route.get('backend') != backend or not isinstance(endpoints, dict)
            or (backend == 'reception_protocol' and endpoints != PROTOCOL_URLS)):
        raise Rejected('导航／操控模拟必须通过运行环境配置显式应用')
    return dict(endpoints)


def target_identity(config, backend):
    from shared.execution_profile import applied_profile
    profile = applied_profile()
    if backend in SIMULATED_RECEPTION:
        return {'backend': backend, **protocol_endpoints(backend)}
    if backend == 'simple_o7':
        from brain.adapters.simple_o7 import settings, WIRE_VERSION
        return {'backend': backend, 'url': settings().url, 'contract_version': WIRE_VERSION}
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
    if backend == 'simple_o7':
        from brain.adapters.simple_o7 import SimpleO7Adapter
        return SimpleO7Adapter.from_config()
    if backend == "camera":
        return LookAdapter()
    if backend in SIMULATED_RECEPTION:
        from copy import deepcopy
        from shared.protocol_simulation import require_simulator_pair
        endpoints = protocol_endpoints(backend)
        require_simulator_pair(endpoints, kind=SIMULATED_RECEPTION[backend])
        selected = deepcopy(config)
        selected.setdefault('reception_real', {}).update(dream_base_url=endpoints['dream'], vla_base_url=endpoints['vla'])
        return BodyAdapter.from_config(selected)
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
