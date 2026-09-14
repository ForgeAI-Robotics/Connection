"""Fixed real-robot reception pipeline: DREAM -> VLA -> evidence -> DREAM."""

from __future__ import annotations

import copy
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

try:
    # Normal entry: master/run.py places the master directory on sys.path.
    from integrations.dream_client import DreamClient, DreamRecoveryRequired
    from integrations.reception_verify import ReceptionVerifier
    from integrations.vla_client import VlaClient, VlaRecoveryRequired
except ModuleNotFoundError as exc:
    # Managed Windows tasks may preserve only the repository root on sys.path.
    # Fall back only when the top-level integrations package itself is absent;
    # never hide a missing dependency imported from inside these modules.
    if exc.name != "integrations" and not str(exc.name).startswith("integrations."):
        raise
    from master.integrations.dream_client import DreamClient, DreamRecoveryRequired
    from master.integrations.reception_verify import ReceptionVerifier
    from master.integrations.vla_client import VlaClient, VlaRecoveryRequired
from .reception_store import ReceptionStore, now_iso


CONTRACT_VERSION = "fq/reception-lan/v1"
OBJECT_ID = "cola_can_1"
TERMINAL_TASK_STATES = {
    "SUCCEEDED", "COMPLETED_HAND_STATE_ONLY", "FAILED", "CANCELLED",
    "RECOVERY_REQUIRED",
}

NAVIGATION_LEGS = {
    "table2": {
        "target_id": "table_2", "route_phase": "", "leg_index": 1,
        "goal_xyt": [1.0, 2.0, 0.0],
        "motion_mode": "forward_path",
    },
    "relay2": {
        "target_id": "door_1", "route_phase": "door_approach", "leg_index": 2,
        "goal_xyt": [3.0, 2.0, 1.5707963267948966],
        "motion_mode": "forward_path",
    },
    "relay3": {
        "target_id": "door_1", "route_phase": "door_lateral_exit", "leg_index": 3,
        "goal_xyt": [3.0, 3.0, 1.5707963267948966],
        "motion_mode": "lateral_path_aligned",
    },
    "table1": {
        "target_id": "table_1", "route_phase": "table1_approach", "leg_index": 4,
        "goal_xyt": [4.0, 4.0, 0.0],
        "motion_mode": "forward_path",
    },
}


class ReceptionPipelineError(RuntimeError):
    pass


class _ReadOnlyPreflightStore:
    """Sentinel store used by external preflight; any write is a bug."""

    def __getattr__(self, name):
        raise RuntimeError(f"只读预检禁止访问持久化方法: {name}")


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        raise ReceptionPipelineError("缺少带时区时间戳")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReceptionPipelineError(f"无效ISO 8601时间: {value}") from exc
    if parsed.tzinfo is None:
        raise ReceptionPipelineError(f"时间戳必须包含时区: {value}")
    return parsed


def _task_token(task_id):
    token = re.sub(r"[^A-Za-z0-9]+", "", str(task_id))[-12:]
    return token or "task"


class ReceptionRealRunner:
    def __init__(
        self, config, *, dream=None, vla=None, verifier=None, store=None,
        on_step=None, on_state=None,
    ):
        self.config = dict(config or {})
        self.contract_version = str(
            self.config.get("contract_version") or CONTRACT_VERSION)
        self.vla_result_policy = str(
            self.config.get("vla_result_policy") or "strict_object_evidence")
        legacy_photo = bool(self.config.get("photo_verification_enabled", False))
        self.grasp_photo_verification_enabled = bool(
            self.config.get("grasp_photo_verification_enabled", legacy_photo))
        self.place_photo_verification_enabled = bool(
            self.config.get("place_photo_verification_enabled", legacy_photo))
        self.on_step = on_step
        self.on_state = on_state
        request_timeout = float(self.config.get("request_timeout_sec", 10))
        self.dream = dream or DreamClient(
            os.getenv("DREAM_BASE_URL") or self.config.get("dream_base_url"),
            contract_version=self.contract_version,
            request_timeout_sec=request_timeout,
        )
        self.vla = vla or VlaClient(
            os.getenv("VLA_BASE_URL") or self.config.get("vla_base_url"),
            contract_version=self.contract_version,
            request_timeout_sec=request_timeout,
        )
        self.verifier = verifier or ReceptionVerifier(
            self.config.get("verification") or {})
        default_runtime = Path(__file__).resolve().parent / "runtime" / "reception"
        runtime_dir = self.config.get("runtime_dir") or str(default_runtime)
        self.store = store or ReceptionStore(runtime_dir)
        self.state = {}

    def _publish_state(self):
        self.state = self.store.save_state(self.state)
        if self.on_state:
            self.on_state(copy.deepcopy(self.state))

    def _set_state(self, **updates):
        self.state.update(updates)
        self._publish_state()

    def _event(self, event, **data):
        payload = dict(data)
        payload.setdefault("task_id", self.state.get("task_id"))
        self.store.append_event(event, **payload)

    def _step(self, order, phase, detail, status="success"):
        if self.on_step:
            self.on_step(order, phase, detail, status)

    def _remote_update(self, service, command_id, payload):
        remote = dict(self.state.get("remote_states") or {})
        remote[command_id] = {
            "service": service,
            "state": payload.get("state"),
            "updated_at": now_iso(),
        }
        self.state["remote_states"] = remote
        self._publish_state()

    def _command_id(self, prefix):
        return f"{prefix}-{_task_token(self.state['task_id'])}"

    def _prepare_command(self, key, command_id, payload):
        commands = dict(self.state.get("commands") or {})
        commands[key] = {
            "command_id": command_id,
            "payload": payload,
            "prepared_at": now_iso(),
        }
        self._set_state(commands=commands)
        self._event(
            "COMMAND_PREPARED", key=key, command_id=command_id, payload=payload)

    @staticmethod
    def _contains_world_ids(graph):
        serialized = json.dumps(graph, ensure_ascii=False)
        return all(name in serialized for name in ("table_2", "door_1", "table_1"))

    @staticmethod
    def _goal_matches(actual, expected, tolerance=1e-9):
        if not isinstance(actual, (list, tuple)) or len(actual) != 3:
            return False
        try:
            return all(
                math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
                for left, right in zip(actual, expected)
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _goal_specs(value, path="agent_navigation_contract"):
        """Yield every contract object containing goal_xyt, regardless of container shape."""
        if isinstance(value, dict):
            if "goal_xyt" in value:
                yield path, value
            for key, child in value.items():
                yield from ReceptionRealRunner._goal_specs(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from ReceptionRealRunner._goal_specs(child, f"{path}[{index}]")

    @staticmethod
    def _door_node(graph):
        nodes = graph.get("nodes") or {}
        if isinstance(nodes, dict):
            return nodes.get("door_1")
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_id = node.get("object_id") or node.get("node_id") or node.get("id")
                if node_id == "door_1":
                    return node
        return None

    def _validate_leg_spec(self, label, spec, expected, *, require_identity=True):
        if not isinstance(spec, dict):
            raise ReceptionPipelineError(f"关系图合同缺少{label}对象")
        if require_identity:
            if spec.get("target_id") != expected["target_id"]:
                raise ReceptionPipelineError(
                    f"关系图{label}.target_id不一致: {spec.get('target_id')!r}")
            if str(spec.get("route_phase") or "") != expected["route_phase"]:
                raise ReceptionPipelineError(
                    f"关系图{label}.route_phase不一致: {spec.get('route_phase')!r}")
            try:
                leg_index = int(spec.get("leg_index"))
            except (TypeError, ValueError) as exc:
                raise ReceptionPipelineError(f"关系图{label}.leg_index缺失或非法") from exc
            if leg_index != expected["leg_index"]:
                raise ReceptionPipelineError(
                    f"关系图{label}.leg_index不一致: {leg_index}")
        if not self._goal_matches(spec.get("goal_xyt"), expected["goal_xyt"]):
            raise ReceptionPipelineError(
                f"关系图{label}.goal_xyt与大脑审核点不一致: {spec.get('goal_xyt')}")
        if spec.get("motion_mode") != expected["motion_mode"]:
            raise ReceptionPipelineError(
                f"关系图{label}.motion_mode不一致: {spec.get('motion_mode')!r}")
        if spec.get("require_final_orientation") is not True:
            raise ReceptionPipelineError(
                f"关系图{label}.require_final_orientation必须为true")

    def _validate_graph_contract(self, graph):
        if graph.get("contract_version") != self.contract_version:
            raise ReceptionPipelineError(
                "关系图contract_version不一致: "
                f"{graph.get('contract_version')!r} != {self.contract_version!r}")
        if graph.get("frame_id") != "map":
            raise ReceptionPipelineError(
                f"关系图frame_id必须为map: {graph.get('frame_id')!r}")

        agent_contract = graph.get("agent_navigation_contract")
        if not isinstance(agent_contract, dict):
            raise ReceptionPipelineError("关系图缺少agent_navigation_contract")
        contract_frame = agent_contract.get("frame_id", graph.get("frame_id"))
        if contract_frame != "map":
            raise ReceptionPipelineError(
                f"agent_navigation_contract.frame_id必须为map: {contract_frame!r}")
        specs = list(self._goal_specs(agent_contract))
        normalized = {}
        for name, expected in NAVIGATION_LEGS.items():
            matches = []
            for path, spec in specs:
                try:
                    leg_index = int(spec.get("leg_index"))
                except (TypeError, ValueError):
                    continue
                if leg_index == expected["leg_index"]:
                    matches.append((path, spec))
            if len(matches) != 1:
                raise ReceptionPipelineError(
                    f"agent_navigation_contract中leg_index={expected['leg_index']}"
                    f"应且仅应出现一次，实际{len(matches)}次")
            path, spec = matches[0]
            self._validate_leg_spec(name, spec, expected, require_identity=True)
            normalized[name] = {"path": path, **copy.deepcopy(spec)}

        door_node = self._door_node(graph)
        if not isinstance(door_node, dict):
            raise ReceptionPipelineError("关系图nodes中缺少door_1")
        door_contract = (door_node.get("evidence") or {}).get("navigation_contract")
        if not isinstance(door_contract, dict):
            raise ReceptionPipelineError(
                "关系图缺少nodes.door_1.evidence.navigation_contract")
        for phase, leg_name in (
            ("door_approach", "relay2"),
            ("door_lateral_exit", "relay3"),
        ):
            self._validate_leg_spec(
                f"door_1.{phase}",
                door_contract.get(phase),
                NAVIGATION_LEGS[leg_name],
                require_identity=False,
            )

        return {
            "contract_version": graph["contract_version"],
            "frame_id": graph["frame_id"],
            "agent_navigation_contract": normalized,
            "door_navigation_contract": copy.deepcopy(door_contract),
        }

    def _recommended_tracking_timeout(self):
        navigation = float(self.config.get("navigation_timeout_sec", 900))
        lateral = float(self.config.get("door_lateral_timeout_sec", 300))
        pick = float(self.config.get("vla_timeout_sec", 600))
        place = float(self.config.get("place_timeout_sec", 1800))
        inspection = (
            float(self.config.get("inspection_timeout_sec", 300))
            if self.config.get("dream_inspection_enabled") is True
            else 0.0
        )
        snapshots = 0.0
        if self.grasp_photo_verification_enabled:
            snapshots += float(self.config.get("snapshot_timeout_sec", 30))
        if self.place_photo_verification_enabled:
            snapshots += float(self.config.get("snapshot_timeout_sec", 30))
        # table2、relay2、table1 use navigation_timeout; relay3 has its own cap.
        return int(3 * navigation + lateral + pick + place + inspection + snapshots + 600)

    def _collect_preflight(self):
        """Run the complete read-only real-reception gate and return evidence."""

        health = self.dream.health()
        status = self.dream.status()
        if health.get("interface_online") is not True:
            raise ReceptionPipelineError("DREAM interface_online不为true")
        if status.get("world_state_available") is not True:
            raise ReceptionPipelineError("DREAM世界状态不可用")
        if status.get("localization_approved") is not True:
            raise ReceptionPipelineError("DREAM定位尚未Approve")
        if status.get("navigation_transport_ready") is not True:
            raise ReceptionPipelineError("DREAM导航通路未就绪")
        if status.get("motion_ready") is not True:
            blockers = {str(value) for value in (status.get("motion_blockers") or [])}
            # An idle DREAM transaction is deliberately disarmed: Gateway and
            # Token become ready only after a reviewed Agent goal is created
            # and g1_agent_navigation_service performs its bounded Arm loop.
            # Accept only that exact standby condition. Localization approval,
            # transport ownership and the absence of an active command remain
            # mandatory above; every other blocker remains fail-closed.
            idle_disarmed = {"GATEWAY_NOT_READY", "TOKEN_NOT_READY"}
            unexpected = blockers - idle_disarmed
            if not blockers or unexpected:
                detail = ", ".join(sorted(blockers)) or "UNKNOWN"
                raise ReceptionPipelineError(
                    f"DREAM motion_ready=false，阻塞项: {detail}"
                )
        if status.get("active_command_id"):
            raise ReceptionPipelineError(
                f"DREAM存在活动命令: {status.get('active_command_id')}")

        world = self.dream.world()
        graph = self.dream.relation_graph(world)
        vla_health = self.vla.health()
        control = self.vla.control_status()
        any_photo = (
            self.grasp_photo_verification_enabled
            or self.place_photo_verification_enabled
        )
        camera = self.vla.camera_status() if any_photo else None
        port = control.get("action_port") or {}
        if not self._contains_world_ids(graph):
            raise ReceptionPipelineError("关系图缺少table_2、door_1或table_1")
        contract_snapshot = self._validate_graph_contract(graph)
        if str(vla_health.get("status") or "").lower() not in {"ok", "healthy", "ready"}:
            raise ReceptionPipelineError("VLA /health未返回可用状态")
        if control.get("service_ready") is not True:
            raise ReceptionPipelineError("VLA service_ready不为true")
        if control.get("policy_running") is True:
            raise ReceptionPipelineError("VLA策略仍在运行")
        if control.get("active_command_id"):
            raise ReceptionPipelineError(
                f"VLA存在活动命令: {control.get('active_command_id')}")
        if control.get("recovery_required") is True:
            raise ReceptionPipelineError("VLA处于recovery_required，禁止新任务")
        if control.get("holding") is not None:
            raise ReceptionPipelineError(
                f"VLA持久状态仍在持有物体: {control.get('holding')}")
        nav_ready = control.get("navigation_port_ready")
        if nav_ready is None:
            nav_ready = port.get("navigation_port_ready")
        if nav_ready is not True:
            raise ReceptionPipelineError("VLA导航通路未就绪")
        if any_photo:
            if str(camera.get("owner") or "").lower() != "vla" or camera.get("ready") is not True:
                raise ReceptionPipelineError("RealSense未由VLA持有或相机未就绪")
        return {
            "contract_version": self.contract_version,
            "dream": {
                "interface_online": health.get("interface_online"),
                "world_state_available": status.get("world_state_available"),
                "localization_approved": status.get("localization_approved"),
                "navigation_transport_ready": status.get("navigation_transport_ready"),
                "motion_ready": status.get("motion_ready"),
                "motion_blockers": status.get("motion_blockers") or [],
                "idle_disarmed_before_first_command": (
                    status.get("motion_ready") is not True
                ),
                "active_command_id": status.get("active_command_id"),
            },
            "vla": {
                "service_ready": control.get("service_ready"),
                "policy_running": control.get("policy_running"),
                "active_command_id": control.get("active_command_id"),
                "recovery_required": control.get("recovery_required"),
                "holding": control.get("holding"),
                "navigation_port_ready": nav_ready,
            },
            "navigation_contract": contract_snapshot,
            "camera": camera,
            "recommended_tracking_timeout_sec": self._recommended_tracking_timeout(),
        }

    def preflight_report(self):
        """Public read-only preflight used before a task is accepted."""

        try:
            snapshot = self._collect_preflight()
        except Exception as exc:
            return {
                "ready": False,
                "required": True,
                "task_type": "reception_real",
                "contract_version": self.contract_version,
                "blockers": [str(exc)],
                "error_type": type(exc).__name__,
                "recommended_tracking_timeout_sec": self._recommended_tracking_timeout(),
            }
        return {
            "ready": True,
            "required": True,
            "task_type": "reception_real",
            "blockers": [],
            **snapshot,
        }

    def _preflight(self):
        snapshot = self._collect_preflight()
        self._set_state(
            navigation_contract=snapshot["navigation_contract"])
        self._event(
            "PREFLIGHT_SUCCEEDED",
            dream_status=snapshot["dream"],
            vla_control=snapshot["vla"],
            vla_camera=snapshot["camera"],
            grasp_photo_verification_enabled=self.grasp_photo_verification_enabled,
            place_photo_verification_enabled=self.place_photo_verification_enabled,
        )
        return snapshot

    def _navigate(self, key, runtime_phase):
        leg = NAVIGATION_LEGS[key]
        command_id = self._command_id(f"nav-{key}")
        payload = {
            "contract_version": self.contract_version,
            "command_id": command_id,
            "task_id": self.state["task_id"],
            "target_id": leg["target_id"],
            "route_phase": leg["route_phase"],
            "leg_index": leg["leg_index"],
            "frame_id": "map",
            "goal_xyt": list(leg["goal_xyt"]),
            "motion_mode": leg["motion_mode"],
            "require_final_orientation": True,
        }
        self._set_state(runtime_phase=runtime_phase)
        transport = self.dream.wait_navigation_transport(
            timeout_sec=float(self.config.get("navigation_transport_timeout_sec", 30)),
            poll_interval_sec=float(self.config.get("dream_poll_interval_sec", 0.5)),
        )
        self._event(
            "DREAM_NAVIGATION_TRANSPORT_READY",
            command_id=command_id,
            status=transport,
        )
        self._prepare_command(key, command_id, payload)
        accepted = self.dream.submit_navigation(payload)
        self._event("DREAM_ACCEPTED", command_id=command_id, response=accepted)
        timeout_key = "door_lateral_timeout_sec" if key == "relay3" else "navigation_timeout_sec"
        terminal = self.dream.wait_command(
            command_id,
            timeout_sec=float(self.config.get(timeout_key, 900 if key != "relay3" else 300)),
            poll_interval_sec=float(self.config.get("dream_poll_interval_sec", 0.5)),
            on_update=lambda value: self._remote_update("dream", command_id, value),
        )
        self.dream.require_success(terminal, command_id)
        self._event("DREAM_TERMINAL", command_id=command_id, response=terminal)
        return command_id, terminal

    def _vla_action(self, operation, navigation_command_id, target_area):
        command_id = self._command_id(f"vla-{operation}")
        payload = {
            "contract_version": self.contract_version,
            "command_id": command_id,
            "task_id": self.state["task_id"],
            "operation": operation,
            "object_id": OBJECT_ID,
            "target_area": target_area,
            "navigation_proof": {
                "dream_command_id": navigation_command_id,
                "target_id": target_area,
                "state": "succeeded",
            },
        }
        key = f"vla_{operation}"
        self._prepare_command(key, command_id, payload)
        accepted = self.vla.submit_task(payload)
        self._event("VLA_ACCEPTED", command_id=command_id, response=accepted)
        timeout_key = "place_timeout_sec" if operation == "place" else "vla_timeout_sec"
        terminal = self.vla.wait_task(
            command_id,
            timeout_sec=float(self.config.get(timeout_key, 1800 if operation == "place" else 600)),
            poll_interval_sec=float(self.config.get("vla_poll_interval_sec", 1.0)),
            on_update=lambda value: self._remote_update("vla", command_id, value),
        )
        if operation == "pick":
            self.vla.require_pick_success(
                terminal,
                task_id=self.state["task_id"],
                command_id=command_id,
                object_id=OBJECT_ID,
                result_policy=self.vla_result_policy,
            )
        else:
            self.vla.require_place_success(
                terminal,
                task_id=self.state["task_id"],
                command_id=command_id,
                object_id=OBJECT_ID,
                result_policy=self.vla_result_policy,
            )
        self._event("VLA_TERMINAL", command_id=command_id, response=terminal)
        return command_id, terminal

    def _inspect_table2_and_handoff_camera(self, navigation_command_id):
        if self.config.get("dream_inspection_enabled") is not True:
            raise ReceptionPipelineError("DREAM细检被禁用，不能进入VLA相机阶段")
        command_id = self._command_id("inspect-table2")
        payload = {
            "contract_version": self.contract_version,
            "command_id": command_id,
            "task_id": self.state["task_id"],
            "navigation_command_id": navigation_command_id,
            "target_object_id": OBJECT_ID,
        }
        self._set_state(runtime_phase="DREAM_INSPECTING_TABLE2")
        self._prepare_command("inspection_table2", command_id, payload)
        accepted = self.dream.submit_inspection(payload)
        self._event("DREAM_INSPECTION_ACCEPTED", command_id=command_id, response=accepted)
        terminal = self.dream.wait_command(
            command_id,
            timeout_sec=float(self.config.get("inspection_timeout_sec", 300)),
            poll_interval_sec=float(self.config.get("dream_poll_interval_sec", 0.5)),
            on_update=lambda value: self._remote_update("dream", command_id, value),
        )
        if str(terminal.get("state") or "").lower() != "succeeded":
            raise ReceptionPipelineError(
                f"DREAM table2细检未成功: {terminal.get('state')}")
        world = self.dream.world()
        graph = self.dream.relation_graph(world)
        contract_snapshot = self._validate_graph_contract(graph)
        camera = self.dream.wait_camera_owner(
            "vla",
            timeout_sec=float(self.config.get("camera_handoff_timeout_sec", 60)),
            poll_interval_sec=float(self.config.get("dream_poll_interval_sec", 0.5)),
        )
        self._set_state(
            navigation_contract=contract_snapshot,
            camera_owner="vla",
            inspection_command_id=command_id,
        )
        self._event(
            "DREAM_CAMERA_HANDOFF_SUCCEEDED",
            command_id=command_id,
            camera=camera,
        )
        return terminal

    def _verify(self, operation, vla_command_id, vla_terminal, target_id):
        completed_at = vla_terminal.get("completed_at")
        snapshot_request = {
            "contract_version": self.contract_version,
            "task_id": self.state["task_id"],
            "source_command_id": vla_command_id,
            "purpose": f"verify_{operation}",
            "captured_after": completed_at,
            "camera_key": self.config.get(
                "grasp_camera_view" if operation == "pick" else "place_camera_view",
                "left_wrist" if operation == "pick" else "ego_view",
            ),
        }
        snapshot_timeout = float(self.config.get("snapshot_timeout_sec", 30))
        snapshot = self.vla.create_snapshot(
            snapshot_request, timeout_sec=snapshot_timeout)
        if _parse_time(snapshot.get("captured_at")) <= _parse_time(completed_at):
            raise ReceptionPipelineError("VLA返回的判真图片不是动作完成后的新帧")
        image_bytes, headers = self.vla.download_snapshot(
            snapshot, timeout_sec=snapshot_timeout)
        content_type = str(
            snapshot.get("content_type") or headers.get("Content-Type") or "image/jpeg"
        ).split(";", 1)[0]
        suffix = ".png" if content_type == "image/png" else ".jpg"
        image_path = self.store.save_image(f"{operation}_after{suffix}", image_bytes)
        verification = self.verifier.verify(
            operation,
            object_id=OBJECT_ID,
            target_id=target_id,
            vla_terminal=vla_terminal,
            snapshot=snapshot,
            image_bytes=image_bytes,
            content_type=content_type,
        )
        record = {
            "task_id": self.state["task_id"],
            "vla_command_id": vla_command_id,
            "image_path": image_path,
            "snapshot": snapshot,
            "verification": verification,
        }
        self.store.append_verification(record)
        self._event("VERIFICATION_COMPLETED", **record)
        if verification.get("verified") is not True:
            raise ReceptionPipelineError(f"{operation}的VLM+LLM判真未通过")
        return verification

    def _start_state(self, task_id):
        existing = self.store.load_state()
        if existing and str(existing.get("state")) not in TERMINAL_TASK_STATES:
            existing.update({
                "state": "RECOVERY_REQUIRED",
                "runtime_phase": "RECOVERY_REQUIRED",
                "failure_reason": "Master重启后发现未完成真机任务，禁止自动补发动作",
            })
            self.state = existing
            self._publish_state()
            raise ReceptionPipelineError(
                "存在未完成真机任务，已标记RECOVERY_REQUIRED")
        self.state = {
            "contract_version": self.contract_version,
            "task_id": task_id,
            "state": "RUNNING",
            "runtime_phase": "FETCHING_WORLD",
            "verified_state": "INITIALIZED",
            "holding": None,
            "object_location": "table_2",
            "vla_result_policy": self.vla_result_policy,
            "grasp_photo_verification_enabled": self.grasp_photo_verification_enabled,
            "place_photo_verification_enabled": self.place_photo_verification_enabled,
            "evidence_level": "vla_only",
            "commands": {},
            "remote_states": {},
            "started_at": now_iso(),
        }
        self._publish_state()
        self._event("TASK_STARTED")

    def run(self, task_id):
        failed_phase = "INITIALIZING"
        try:
            self._start_state(task_id)
            failed_phase = "FETCHING_WORLD"
            self._preflight()
            preflight_detail = "DREAM、VLA和关系图均已就绪"
            if (
                self.grasp_photo_verification_enabled
                or self.place_photo_verification_enabled
            ):
                preflight_detail += "，动作后相机快照可用"
            else:
                preflight_detail += "，照片判真开关已关闭"
            self._step(1, "世界与服务预检", preflight_detail)

            failed_phase = "NAVIGATING_TO_TABLE2"
            nav_table2, _ = self._navigate("table2", failed_phase)
            self._set_state(verified_state="AT_TABLE2")
            self._step(2, "导航到table2", "DREAM真实终态succeeded")

            if self.config.get("dream_inspection_enabled") is True:
                failed_phase = "DREAM_INSPECTING_TABLE2"
                self._inspect_table2_and_handoff_camera(nav_table2)
                self._step(
                    3,
                    "DREAM细检与相机交接",
                    "细检成功，相机控制权已交给VLA",
                )
            else:
                self._set_state(camera_owner="vla")
                self._event(
                    "DREAM_INSPECTION_SKIPPED",
                    navigation_command_id=nav_table2,
                    reason="DREAM navigation-only contract",
                )
                self._step(
                    3,
                    "DREAM细检跳过",
                    "DREAM仅负责导航；大脑直接调用VLA，不调用inspection接口",
                )

            failed_phase = "VLA_PICKING"
            self._set_state(runtime_phase=failed_phase)
            vla_pick, pick_terminal = self._vla_action("pick", nav_table2, "table_2")
            self._step(4, "VLA抓取", "VLA抓取终态成功，策略停止且导航通路恢复")

            if self.grasp_photo_verification_enabled:
                failed_phase = "VERIFYING_GRASP"
                self._set_state(
                    runtime_phase=failed_phase,
                    verified_state="AT_TABLE2",
                    holding=None,
                )
                self._verify("pick", vla_pick, pick_terminal, "table_2")
                grasp_state = "GRASP_VERIFIED"
                grasp_detail = "VLM+LLM确认目标已在夹爪中"
            else:
                grasp_state = "GRASP_CONFIRMED_BY_VLA"
                grasp_detail = "照片判真关闭，按VLA真实抓取终态确认"
                self._event(
                    "PHOTO_VERIFICATION_SKIPPED",
                    operation="pick",
                    vla_command_id=vla_pick,
                )
            self._set_state(
                runtime_phase="READY_FOR_RELAY2",
                verified_state=grasp_state,
                holding=OBJECT_ID,
                object_location="in_gripper",
                evidence_level=(
                    "vla_plus_grasp_photo"
                    if self.grasp_photo_verification_enabled else "vla_only"
                ),
            )
            self._step(5, "抓取结果确认", grasp_detail)

            failed_phase = "NAVIGATING_TO_RELAY2"
            self._navigate("relay2", failed_phase)
            self._set_state(verified_state="AT_RELAY2")
            self._step(6, "导航到relay2", "门外接近点真实到达")

            failed_phase = "LATERAL_TO_RELAY3"
            self._navigate("relay3", failed_phase)
            self._set_state(verified_state="DOOR_PASSED")
            self._step(7, "横移到relay3", "横移穿门真实成功")

            failed_phase = "NAVIGATING_TO_TABLE1"
            nav_table1, _ = self._navigate("table1", failed_phase)
            self._set_state(verified_state="AT_TABLE1")
            self._step(8, "导航到table1", "DREAM真实终态succeeded")

            failed_phase = "VLA_PLACING"
            self._set_state(runtime_phase=failed_phase)
            vla_place, place_terminal = self._vla_action("place", nav_table1, "table_1")
            self._step(9, "VLA放置", "VLA放置终态成功，策略停止且导航通路恢复")

            if self.place_photo_verification_enabled:
                failed_phase = "VERIFYING_PLACE"
                self._set_state(
                    runtime_phase=failed_phase,
                    verified_state="AT_TABLE1",
                    holding=OBJECT_ID,
                    object_location="in_gripper",
                )
                self._verify("place", vla_place, place_terminal, "table_1")
                place_state = "PLACE_VERIFIED"
                place_detail = "VLM+LLM确认目标已放置在table1"
            else:
                place_state = "PLACE_CONFIRMED_BY_VLA"
                place_detail = "照片判真关闭，按VLA真实释放终态确认"
                self._event(
                    "PHOTO_VERIFICATION_SKIPPED",
                    operation="place",
                    vla_command_id=vla_place,
                )
            final_state = (
                "SUCCEEDED"
                if self.vla_result_policy == "strict_object_evidence"
                or self.place_photo_verification_enabled
                else "COMPLETED_HAND_STATE_ONLY"
            )
            self._set_state(
                state=final_state,
                runtime_phase=final_state,
                verified_state=place_state,
                holding=None,
                object_location="table_1",
                evidence_level=(
                    "vla_plus_place_photo"
                    if self.place_photo_verification_enabled else self.state.get("evidence_level", "vla_only")
                ),
                completed_at=now_iso(),
            )
            self._step(10, "放置结果确认", place_detail)
            self._step(11, "接待任务完成", "单罐取放全链路完成")
            self._event("TASK_SUCCEEDED")
            return copy.deepcopy(self.state)
        except Exception as exc:
            if isinstance(exc, (DreamRecoveryRequired, VlaRecoveryRequired)):
                if not self.state:
                    self.state = {
                        "task_id": task_id,
                        "contract_version": self.contract_version,
                    }
                self.state.update({
                    "state": "RECOVERY_REQUIRED",
                    "runtime_phase": "RECOVERY_REQUIRED",
                    "failed_phase": failed_phase,
                    "failure_reason": str(exc),
                    "recovery_command": (getattr(exc, "payload", None) or {}).get("command_id"),
                    "updated_at": now_iso(),
                })
                self._publish_state()
                self._event(
                    "RECOVERY_REQUIRED",
                    failed_phase=failed_phase,
                    error=str(exc),
                    error_payload=getattr(exc, "payload", None),
                )
                self._step(99, "任务进入人工恢复", str(exc), "failure")
                return copy.deepcopy(self.state)
            if self.state.get("state") == "RECOVERY_REQUIRED":
                self._event(
                    "RECOVERY_BLOCKED",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                self._step(99, "任务需要人工恢复", str(exc), "failure")
                return copy.deepcopy(self.state)
            if not self.state:
                self.state = {
                    "task_id": task_id,
                    "contract_version": self.contract_version,
                }
            self.state.update({
                "state": "FAILED",
                "runtime_phase": "FAILED",
                "failed_phase": failed_phase,
                "failure_reason": str(exc),
                "completed_at": now_iso(),
            })
            self._publish_state()
            self._event(
                "TASK_FAILED",
                failed_phase=failed_phase,
                error_type=type(exc).__name__,
                error=str(exc),
                error_payload=getattr(exc, "payload", None),
            )
            self._step(99, f"任务失败：{failed_phase}", str(exc), "failure")
            return copy.deepcopy(self.state)


def run_reception_real(task_id, config, *, on_step=None, on_state=None, **dependencies):
    runner = ReceptionRealRunner(
        config,
        on_step=on_step,
        on_state=on_state,
        **dependencies,
    )
    return runner.run(task_id)


def check_reception_real_preflight(config, **dependencies):
    """Run the same gates as the real pipeline without creating task state."""

    runner = ReceptionRealRunner(
        config,
        store=_ReadOnlyPreflightStore(),
        **dependencies,
    )
    return runner.preflight_report()
