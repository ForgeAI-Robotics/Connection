"""fq/reception-lan/v1 NAV and VLA endpoints of the SIMPLE physical reception episode.

Served by the existing facade under /reception/nav and /reception/vla, so the
brain's DreamClient and VlaClient are used unchanged with a path-prefixed base URL.
Like the real LAN services these paths take no bearer token; they never reach a robot.
"""
import os
import hashlib
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import unquote

from .reception_contract import CONTRACT_VERSION, InvalidRequest, validate_request
from .reception_store import Conflict, ReceptionStore, stamp
from .reception_task_info import SCENE

RELEASE_SRC = "/mnt/simple/generated-data/o6-planner-production-20260923/sonic_release_v1/src"
PREFIXES = {"/reception/nav": "nav", "/reception/vla": "vla"}


class Reception:
    def __init__(self, config, launcher=None):
        self.config = config
        self.scene = config.get('reception_scene', 'formal_room')
        if self.scene not in {'formal_room', 'office_v2'}:
            raise ValueError('Unknown reception_scene: ' + str(self.scene))
        self.semantic_scene = SCENE
        self.scene_sha256 = None
        if self.scene == 'office_v2':
            from .office_layout import SCENE as OFFICE_SCENE
            if not config.get('reception_office_assets'):
                raise ValueError('office_v2 requires reception_office_assets')
            asset = Path(config['reception_office_assets']) / 'scene.xml'
            self.scene_sha256 = hashlib.sha256(asset.read_bytes()).hexdigest()
            self.semantic_scene = OFFICE_SCENE
        self.root = Path(config["runtime"])
        self.store = ReceptionStore(self.root)
        self.launcher = launcher or self.launch

    def launch(self, task, episode):
        Path(episode).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONPATH=RELEASE_SRC + ":" + str(Path(__file__).resolve().parents[1]))
        with (Path(episode) / "process.log").open("ab") as output:
            extra = ['--scene', self.scene]
            if self.scene == 'office_v2':
                extra += ['--office-assets', self.config['reception_office_assets']]
            process = subprocess.Popen([sys.executable, "-m", "service.reception_worker", "--runtime", str(self.root),
                                        "--task", task, "--episode", episode,
                                        "--seed", str(self.config.get("reception_seed", 601)), *extra],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=env,
                                       cwd=str(Path(__file__).resolve().parents[1]), start_new_session=True)
        threading.Thread(target=process.wait, daemon=True).start()
        return process.pid

    @staticmethod
    def error(code, message, status, started=False):
        return status, {"success": False, "error": {"code": code, "message": message, "retryable": False,
                        "details": {"action_started": started, "action_state_uncertain": False}}}

    def health(self, role):
        return 200, {"contract_version": CONTRACT_VERSION,
                     "service": "dream-agent-http" if role == "nav" else "vla-task-service",
                     "status": "ok", "interface_online": True, "time": stamp(),
                     "simulation": {"kind": "simple_physics", "role": role, "instance_id": self.store.instance_id,
                                    "engine": "MuJoCo · G1 O6 · SONIC v1.1 (sonic_release_v1)",
                                    "scene": self.scene, "scene_source_sha256": self.scene_sha256,
                                    "semantic_targets": self.semantic_scene}}

    def status(self, role):
        active, receipt = self.store.status()
        current = active if active and active["role"] == role else None
        nav_ready = not active or active["role"] != "vla"
        common = {"contract_version": CONTRACT_VERSION,
                  "active_command_id": current["body"]["command_id"] if current else None}
        if role == "nav":
            value = dict(common, interface_online=True, world_state_available=True,
                         active_command_state=current["state"] if current else None,
                         navigation_transport_ready=nav_ready, motion_ready=not active,
                         motion_blockers=["COMMAND_ACTIVE"] if active else [],
                         localization_initialized=True, localization_approved=True, gateway_ready=True,
                         token_ready=True,
                         agent_mode={"enabled": True, "auto_queue_enabled": False, "publish_nav_done_for_vla": False})
        else:
            value = dict(common, service_ready=True, policy_running=current is not None,
                         action_port={"owner": "vla" if current else "navigation", "navigation_port_ready": nav_ready,
                                      "last_changed_at": stamp()},
                         camera={"owner": "vla", "ready": False, "streaming": False},
                         robot={"sonic_healthy": True, "gripper_ready": True})
        if receipt and ((role == "nav" and receipt["kind"] == "to_nav") or
                        (role == "vla" and receipt["kind"] in {"to_vla", "safe_idle"})):
            value["control_receipt"] = receipt
        return 200, value

    def handle(self, method, path, body, headers):
        """Return (status, json) for a /reception/... path, or None if the path is not ours."""
        prefix = next((p for p in PREFIXES if path == p or path.startswith(p + "/")), None)
        if prefix is None:
            return None
        role, sub = PREFIXES[prefix], path[len(prefix):] or "/"
        header = headers.get("X-Contract-Version")
        try:
            if header and header != CONTRACT_VERSION:
                raise InvalidRequest("X-Contract-Version 不匹配")
            if method == "GET" and sub == "/health":
                return self.health(role)
            if method == "GET" and sub == ("/v1/status" if role == "nav" else "/v1/vla/control/status"):
                return self.status(role)
            if method == "POST" and sub == ("/v1/navigation/goals" if role == "nav" else "/v1/vla/tasks"):
                validate_request(role, body)
                value, code = self.store.submit(role, body, self.launcher)
                if code == 202:
                    value = {k: value[k] for k in ("contract_version", "task_id", "command_id", "state", "accepted_at")}
                    value.update(accepted=True, status_url=(prefix + ("/v1/commands/" if role == "nav" else "/v1/vla/tasks/")
                                                            + body["command_id"]))
                    value["target_id" if role == "nav" else "operation"] = body["target_id" if role == "nav" else "operation"]
                return code, value
            command_prefix = "/v1/commands/" if role == "nav" else "/v1/vla/tasks/"
            if role == "vla" and method == "POST" and sub.startswith(command_prefix) and sub.endswith("/cancel"):
                return self.cancel(role, body, unquote(sub[len(command_prefix):-len("/cancel")]))
            if method == "GET" and sub.startswith(command_prefix):
                return 200, self.store.command(role, unquote(sub[len(command_prefix):]))
            if role == "nav" and method == "POST" and sub == "/v1/navigation/cancel":
                return self.cancel(role, body, None)
            if method == "GET" and sub == "/v1/camera/status":
                return 200, {"ready": False, "streaming": False, "reason": "simple_reception_has_no_live_camera"}
            if role == "nav" and method == "GET" and sub == "/v1/world":
                return 200, {"contract_version": CONTRACT_VERSION, "frame_id": "map", "door_object_id": "door_1",
                             "relation_graph_url": "/total_scene_graph_latest.json", "updated_at": stamp(),
                             "simulation_frame": 'simple_' + self.scene}
            if role == "nav" and method == "GET" and sub == "/total_scene_graph_latest.json":
                return 200, {"frame_id": "map", "updated_at": stamp(),
                             "objects": [{"id": k, "type": v["type"]} for k, v in self.semantic_scene.items()]}
            if method == "POST" and sub in {"/v1/inspection", "/v1/camera/snapshots"}:
                return self.error("UNSUPPORTED_CAPABILITY", "SIMPLE 接待仿真不提供相机证据；请关闭可选检查", 501)
            return self.error("HTTP_ERROR", "unknown path", 404)
        except InvalidRequest as exc:
            return self.error("INVALID_REQUEST", str(exc), 400)
        except Conflict as exc:
            return self.error(exc.code, exc.message, exc.status)

    def cancel(self, role, body, command_id):
        if not isinstance(body, dict) or not isinstance(body.get("task_id"), str) or not body["task_id"]:
            raise InvalidRequest("取消必须指定 task_id")
        target = command_id or body.get("command_id")
        if not isinstance(target, str) or not target or body.get("command_id", target) != target:
            raise InvalidRequest("取消 command_id 不匹配")
        self.store.command(role, target, cancel_task=body["task_id"])
        return 202, {"accepted": True, "task_id": body["task_id"], "command_id": target}
