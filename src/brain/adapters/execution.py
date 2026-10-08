"""Wrap DreamClient and VlaClient. Missing handoff queries stay unavailable."""

from __future__ import annotations

import json
from shared.paths import workspace_root
import threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta

from contracts.tasks import CONTRACT_VERSION
from brain.kernel.memory import DEFAULT_EVENT_TTL_SEC
from brain.skills.catalog import by_id
from brain.adapters.http_client import HttpContractError


class BodyAdapter:
    """Real DREAM/VLA wrapper. It does not invent an NX start or stop call."""

    contract_version = CONTRACT_VERSION

    def __init__(self, dream, vla):
        self.dream = dream
        self.vla = vla

    @classmethod
    def from_config(cls, config):
        from brain.adapters.dream_client import DreamClient
        from brain.adapters.vla_client import VlaClient

        real = (config or {}).get("reception_real") or {}
        version = str(real.get("contract_version") or CONTRACT_VERSION)
        timeout = float(real.get("request_timeout_sec", 10))
        dream = DreamClient(
            real.get("dream_base_url"),
            contract_version=version,
            request_timeout_sec=timeout,
        )
        vla = VlaClient(
            real.get("vla_base_url"),
            contract_version=version,
            request_timeout_sec=timeout,
        )
        return cls(dream, vla)

    @property
    def gate_open(self) -> bool:
        status = self.dream.status()
        return status.get("navigation_transport_ready") is True

    def handoff(self, step_id, kind):
        del step_id, kind
        return {"available": False, "confirmed": False, "reason": "unavailable"}

    def handoff_with_context(self, context):
        """Read the deployed status APIs. Transport readiness is not controller takeover."""
        answer = {"available": False, "confirmed": False, "kind": context["kind"],
                  "task_id": context["task_id"], "source_command_id": context.get("source_command_id"),
                  "reason": "source_command_unavailable"}
        command_id, request = context.get("source_command_id"), context.get("source_request")
        if not command_id or not request:
            return answer
        source = self.query(command_id, request)
        answer["source"] = source
        evidence = source.get("evidence") or {}
        if (evidence.get("identity_ok") is not True or evidence.get("time_ok") is not True
                or source.get("terminal") != "succeeded" or source.get("stopped") is not True
                or source.get("resources_released") is not True):
            answer["reason"] = "source_stop_unconfirmed"
            return answer
        try:
            nav, vla = self.dream.status(), self.vla.control_status()
        except HttpContractError as exc:
            answer.update(reason="control_status_unavailable", error=str(exc))
            return answer
        answer.update(navigation_status=nav, manipulation_status=vla)
        nav_idle = ("active_command_id" in nav and nav["active_command_id"] in (None, "")
                    and "active_command_state" in nav and nav["active_command_state"] is None
                    and nav.get("navigation_transport_ready") is True)
        vla_idle = ("active_command_id" in vla and vla["active_command_id"] in (None, "")
                    and vla.get("policy_running") is False
                    and (vla.get("action_port") or {}).get("navigation_port_ready") is True)
        answer["transport_ready"] = nav_idle and vla_idle
        answer["reason"] = "transport_not_ready"
        if answer["transport_ready"]:
            # The controller receipt is optional until NAV/VLA implement it: an idle
            # transport with the source stopped and 5556 returned lets the task proceed.
            # A receipt that is present must still be valid.
            from contracts.control_receipt import valid_receipt
            target = nav if context["kind"] == "to_nav" else vla
            receipt = target.get("control_receipt")
            if receipt is None:
                answer.update(available=True, confirmed=True, reason="transport_ready_without_receipt")
            else:
                answer["controller_receipt"] = receipt
                answer["reason"] = "controller_receipt_invalid"
                if valid_receipt(receipt, context, completed_at=(source.get("raw") or {}).get("completed_at")):
                    answer.update(available=True, confirmed=True, reason="controller_confirmed")
        return answer

    def submit(self, command_id, request):
        body = request["body"]
        skill = request.get("skill")
        try:
            if skill == "navigate":
                raw = self.dream.submit_navigation(body)
            elif skill == "inspect":
                raw = self.dream.submit_inspection(body)
            elif skill in {"pick", "place"}:
                raw = self.vla.submit_task(body)
            else:
                raise RuntimeError(f"未知技能: {skill}")
        except HttpContractError as exc:
            return {
                "accepted": False,
                "unclear": True,
                "completed": False,
                "command_id": command_id,
                "error": str(exc),
                "raw": exc.payload,
            }
        return {
            "accepted": True,
            "unclear": False,
            "completed": False,
            "command_id": command_id,
            "raw": raw,
        }

    def query(self, command_id, request):
        skill = (request or {}).get("skill")
        try:
            if skill in {"navigate", "inspect"}:
                raw = self.dream.command(command_id)
            else:
                raw = self.vla.task(command_id)
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)
        return interpret_observation(command_id, raw, request)

    def wait(self, command_id, request):
        skill = request.get("skill")
        deadline = float(request.get("deadline_sec") or 30)
        try:
            if skill in {"navigate", "inspect"}:
                raw = self.dream.wait_command(
                    command_id,
                    timeout_sec=deadline,
                    poll_interval_sec=float(request.get("poll_interval_sec") or 0.5),
                )
            else:
                raw = self.vla.wait_task(
                    command_id,
                    timeout_sec=deadline,
                    poll_interval_sec=float(request.get("poll_interval_sec") or 1.0),
                )
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)
        viewed = interpret_observation(command_id, raw, request)
        viewed["timed_out"] = False
        return viewed

    def cancel(self, command_id, request):
        skill = (request or {}).get("skill")
        task_id = (request or {}).get("task_id") or ""
        try:
            if skill in {"navigate", "inspect"}:
                raw = self.dream.cancel(task_id, command_id)
            else:
                raw = self.vla.cancel(task_id, command_id)
        except HttpContractError as exc:
            return {
                "accepted": False,
                "completed": False,
                "command_id": command_id,
                "error": str(exc),
            }
        if isinstance(raw, dict):
            echoed = raw.get("command_id")
            if echoed not in (None, "", command_id):
                return {
                    "accepted": False,
                    "completed": False,
                    "command_id": command_id,
                    "error": "cancel_identity_mismatch",
                }
            echoed_task = raw.get("task_id")
            if task_id and echoed_task not in (None, "", task_id):
                return {
                    "accepted": False,
                    "completed": False,
                    "command_id": command_id,
                    "error": "cancel_identity_mismatch",
                }
        return {"accepted": True, "completed": False, "command_id": command_id}


def _unconfirmed(command_id, error: str, *, raw=None) -> dict:
    return {
        "command_id": command_id,
        "terminal": None,
        "timed_out": True,
        "started": None,
        "stopped": False,
        "resources_released": False,
        "evidence": {},
        "error": error,
        "raw": raw if isinstance(raw, dict) else None,
    }


def _timestamp_has_timezone(value) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _identity_ok(command_id, raw, request, result) -> bool:
    body = (request or {}).get("body") if isinstance((request or {}).get("body"), dict) else {}
    expected_task = (request or {}).get("task_id") or body.get("task_id")
    expected_version = body.get("contract_version") or CONTRACT_VERSION
    if raw.get("command_id") != command_id:
        return False
    if not expected_task or raw.get("task_id") != expected_task:
        return False
    if raw.get("contract_version") != expected_version:
        return False
    skill = (request or {}).get("skill")
    if skill == "navigate":
        if raw.get("target_id") != body.get("target_id"):
            return False
        if raw.get("route_phase") != body.get("route_phase"):
            return False
        if raw.get("leg_index") != body.get("leg_index"):
            return False
    elif skill == "inspect":
        echoed = raw.get("target_object_id") or result.get("target_object_id")
        expected_object = body.get("target_object_id")
        if expected_object and echoed not in (None, expected_object):
            return False
    elif skill in {"pick", "place"}:
        if raw.get("operation") != body.get("operation"):
            return False
        if result.get("object_id") != body.get("object_id"):
            return False
    return True


def _stop_flags(skill, result) -> tuple:
    """Publisher stop and resource release are explicit fields, never the terminal name."""
    if skill in {"navigate", "inspect"}:
        stopped = result.get("navigation_stopped") is True
        return stopped, stopped
    if skill in {"pick", "place"}:
        publisher = result.get("policy_stopped") is True
        resources = result.get("navigation_port_ready") is True
        return publisher and resources, resources and publisher
    return False, False


def _effect_supported(skill, result, body) -> bool:
    if skill == "navigate":
        return result.get("success") is True and result.get("reached") is True
    if skill == "inspect":
        return result.get("success") is True
    object_id = body.get("object_id")
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    if skill == "pick":
        return (
            result.get("success") is True
            and result.get("object_grasped") is True
            and result.get("holding") == object_id
            and evidence.get("object_presence_verified") is not False
        )
    if skill == "place":
        return (
            result.get("success") is True
            and result.get("object_grasped") is False
            and result.get("released") is True
            and result.get("holding") is None
            and result.get("object_at_target") is True
            and evidence.get("object_at_target_verified") is not False
        )
    return False


def _contradicts(skill, result) -> bool:
    if result.get("success") is False:
        return True
    if skill == "navigate" and result.get("reached") is False:
        return True
    if skill == "pick" and result.get("object_grasped") is False:
        return True
    if skill == "place" and (
        result.get("released") is False or result.get("object_at_target") is False
    ):
        return True
    return False


def _started(raw, result):
    if result.get("action_started") is False or raw.get("action_started") is False:
        return False
    if result.get("action_started") is True or raw.get("action_started") is True:
        return True
    if raw.get("started_at"):
        return True
    return None


def interpret_observation(command_id, raw, request) -> dict:
    """Turn one downstream payload into evidence. A foreign or incomplete payload stays unconfirmed."""
    if not isinstance(raw, dict):
        return {
            "command_id": command_id,
            "terminal": None,
            "timed_out": False,
            "started": None,
            "stopped": False,
            "resources_released": False,
            "evidence": {
                "supports": False,
                "contradicts": False,
                "identity_ok": False,
                "time_ok": False,
                "grade": "",
                "identity": command_id,
            },
        }
    result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
    body = (request or {}).get("body") if isinstance((request or {}).get("body"), dict) else {}
    skill = (request or {}).get("skill")
    identity_ok = _identity_ok(command_id, raw, request, result)
    if not identity_ok:
        return {
            "command_id": command_id,
            "terminal": "",
            "timed_out": False,
            "started": None,
            "stopped": False,
            "resources_released": False,
            "evidence": {
                "supports": False,
                "contradicts": False,
                "identity_ok": False,
                "time_ok": False,
                "grade": "",
                "identity": raw.get("command_id") or "",
            },
            "raw": raw,
        }
    state = str(raw.get("state") or "").lower()
    terminal = state if state in {"succeeded", "failed", "cancelled"} else ""
    level = str(result.get("evidence_level") or result.get("evidence_grade") or "")
    hand_only = level == "hand_state_only"
    stopped, resources = _stop_flags(skill, result)
    effect = _effect_supported(skill, result, body)
    supports = bool(effect and stopped and not hand_only and state == "succeeded")
    if hand_only:
        grade = "hand_state_only"
    elif supports and skill in {"pick", "place"}:
        grade = "object"
    else:
        grade = level
    evidence = {
        "supports": supports,
        "contradicts": _contradicts(skill, result),
        "identity_ok": True,
        "time_ok": _timestamp_has_timezone(raw.get("completed_at")),
        "grade": grade,
        "identity": command_id,
        "reported_success": state == "succeeded" and result.get("success") is True,
        "effect_verified": supports and _timestamp_has_timezone(raw.get("completed_at")),
        "reported_evidence_level": level,
    }
    entry = by_id(str(skill or ""))
    if supports and entry is not None:
        evidence["effect"] = entry.verifies
    return {
        "command_id": command_id,
        "terminal": terminal,
        "timed_out": False,
        "started": _started(raw, result),
        "stopped": bool(stopped),
        "resources_released": bool(resources),
        "evidence": evidence,
        "raw": raw,
    }


def _observation_ttl(ttl_sec) -> float:
    try:
        number = float(ttl_sec)
    except (TypeError, ValueError):
        return float(DEFAULT_EVENT_TTL_SEC)
    if number <= 0:
        return float(DEFAULT_EVENT_TTL_SEC)
    return number


def parse_capture_scene(raw, *, ttl_sec=None) -> dict | None:
    """Read the JSON string returned by camera.capture_scene / capture_image."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], dict):
        return None
    if data[1].get("_status") != "success":
        return None
    text = str(data[0] or "")
    marker = "视野描述（"
    if not text.startswith(marker) or "）：" not in text:
        return None
    source, body = text[len(marker):].split("）：", 1)
    source = source.strip()
    body = body.strip()
    if not source or not body:
        return None
    observed = datetime.now().astimezone()
    observed_at = observed.isoformat(timespec="milliseconds")
    valid_until = (observed + timedelta(seconds=_observation_ttl(ttl_sec))).isoformat(
        timespec="milliseconds"
    )
    return {
        "supports": True,
        "contradicts": False,
        "identity_ok": True,
        "time_ok": True,
        "grade": "description",
        "text": body,
        "source": source,
        "completed_at": observed_at,
        "observation": {
            "subject": "scene_description",
            "value": body,
            "source": source,
            "observed_at": observed_at,
            "valid_until": valid_until,
        },
    }


class LookAdapter:
    """Call camera.capture_scene. This port does not submit DREAM or VLA commands."""

    contract_version = CONTRACT_VERSION

    def __init__(self, capture=None):
        self.capture = capture or load_capture_scene()
        self.gate_open = True
        self._pending = None
        self._error = ""
        self._timed_out = False
        self.executor = None

    def handoff(self, step_id, kind):
        del step_id, kind
        return {"available": False, "confirmed": False, "reason": "unavailable"}

    def submit(self, command_id, request):
        del command_id
        skill = (request or {}).get("skill")
        if skill != "describe":
            raise RuntimeError(f"未知技能: {skill}")
        deadline = float((request or {}).get("deadline_sec") or 30)
        task = str(((request or {}).get("body") or {}).get("task") or "")
        self._pending = None
        self._error = ""
        self._timed_out = False
        try:
            self._pending = self._run(task, deadline)
        except FuturesTimeout:
            self._timed_out = True
            self._error = "timeout"
        except TimeoutError:
            self._timed_out = True
            self._error = "timeout"
        except Exception as exc:
            self._error = str(exc) or "describe_failed"
        return {
            "accepted": True,
            "unclear": False,
            "completed": False,
            "command_id": "",
        }

    def wait(self, command_id, request):
        del command_id
        if self._timed_out or self._error:
            return {
                "command_id": "",
                "terminal": None,
                "timed_out": self._timed_out,
                "started": True,
                "stopped": False,
                "evidence": {},
                "error": self._error or "describe_failed",
            }
        evidence = parse_capture_scene(
            self._pending,
            ttl_sec=(request or {}).get("observation_ttl_sec"),
        )
        if not evidence:
            return {
                "command_id": "",
                "terminal": None,
                "timed_out": False,
                "started": True,
                "stopped": False,
                "evidence": {},
                "error": "describe_incomplete",
            }
        return {
            "command_id": "",
            "terminal": "succeeded",
            "timed_out": False,
            "started": True,
            "stopped": False,
            "evidence": evidence,
        }

    def query(self, command_id, request):
        del command_id, request
        return {
            "command_id": "",
            "terminal": None,
            "timed_out": True,
            "started": None,
            "stopped": False,
            "evidence": {},
            "error": "no_command",
        }

    def _run(self, task: str, deadline: float):
        if self.executor is not None:
            return self.executor.submit(self.capture, task).result(timeout=deadline)
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self.capture, task)
        try:
            return future.result(timeout=deadline)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)


def parse_sim_action(subtask: str):
    """Map a planner sentence onto the existing sim actions. Unknown text is refused."""
    text = str(subtask or "").strip()
    if not text:
        return None
    labels = {"clean_trash": "清理垃圾", "tidy_milk": "整理牛奶", "tidy_cola": "整理可乐", "tidy_penholder": "整理笔筒"}
    for skill_name, label in labels.items():
        if text in {skill_name, label}:
            return ("skill", skill_name)
    import re

    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    coordinate = re.fullmatch(
        rf"(?:导航到|前往|走到|移动到|到达)\s*(?:坐标\s*)?[（(]?\s*({number})\s*[,，]\s*({number})"
        rf"(?:\s*[,，]\s*({number}))?\s*[）)]?\s*(?:[，,]?\s*(?:并|然后|到达后)?\s*停止)?[。.]?", text)
    if coordinate:
        return ("navigate", ",".join(part for part in coordinate.groups() if part is not None))

    placed = re.fullmatch(r"(?:将|把)\s*(\S+)\s*(?:放置|放|搁)(?:到|至)\s*(\S+?)(?:\s*上)?[，。,.；;]?", text)
    if placed:
        return ("place", _sim_token(placed.group(1)), _sim_token(placed.group(2)))
    placed = re.fullmatch(r"(?:放置|放到|搁到)\s*(\S+?)\s*(?:到|至)\s*(\S+?)(?:\s*上)?[，。,.；;]?", text)
    if placed:
        return ("place", _sim_token(placed.group(1)), _sim_token(placed.group(2)))
    grasped = re.search(r"(?:抓取|拿起|取走|拾起|捡起)\s*(\S+(?: \d+)?)", text)
    if grasped:
        return ("grasp", _sim_token(grasped.group(1)))
    moved = re.search(r"(?:导航到|前往|走到|移动到|到达|靠近)\s*([^\s（()）]+(?: \d+)?)", text)
    if moved:
        return ("navigate", _sim_token(moved.group(1)))
    return None


def _sim_token(value: str) -> str:
    return str(value or "").strip().strip("，。,.、；;")


def _effect_holds(action, world, zones) -> bool | None:
    if not action or not isinstance(world, dict) or world.get("success") is False:
        return None
    kind = action[0]
    if kind == "navigate":
        return None
    if kind == "grasp":
        item = world.get(action[1])
        if not isinstance(item, dict) or "grasped" not in item:
            return None
        return item["grasped"] is True
    if kind == "place":
        item = world.get(action[1])
        zone = (zones or {}).get(action[2]) if isinstance(zones, dict) else None
        if not isinstance(item, dict) or not isinstance(zone, dict):
            return None
        pos, center = item.get("pos"), zone.get("pos")
        if "grasped" not in item or not pos or not center or len(pos) < 2 or len(center) < 2 or "radius" not in zone:
            return None
        dx, dy = float(pos[0]) - float(center[0]), float(pos[1]) - float(center[1])
        return item["grasped"] is False and (dx * dx + dy * dy) ** 0.5 <= float(zone["radius"]) + 0.05
    return None


def _sim_view(command_id: str, action, raw, world, zones) -> dict:
    detail = str((raw or {}).get("result") or "") if isinstance(raw, dict) else ""
    if any(marker in detail for marker in ("连接失败:", "请求错误:", "动作未被任何后端处理")):
        return _unconfirmed(command_id, detail)
    reported = isinstance(raw, dict) and raw.get("success") is True
    holds = _effect_holds(action, world, zones)
    if action is None or (isinstance(raw, dict) and raw.get("success") is False):
        terminal, supports, contradicts = "failed", False, True
    elif holds is None or not reported:
        terminal, supports, contradicts = "", False, False
    elif holds:
        terminal, supports, contradicts = "succeeded", True, False
    else:
        terminal, supports, contradicts = "failed", False, True
    detail = ""
    if isinstance(raw, dict):
        detail = str(raw.get("result") or raw.get("fail_reason") or "")
    return {
        "command_id": command_id,
        "terminal": terminal,
        "timed_out": False,
        "started": True,
        "stopped": True,
        "resources_released": True,
        "evidence": {
            "supports": supports,
            "contradicts": contradicts,
            "identity_ok": True,
            "time_ok": True,
            "grade": "object",
            "identity": command_id,
            "detail": detail,
        },
    }


class SimAdapter:
    """Run a generic step on the existing simulation client. It does not call DREAM or VLA."""

    contract_version = CONTRACT_VERSION

    def __init__(self, perform=None, world=None, zones=None, api=None):
        self._perform = perform
        self._world = world
        self._zones = zones
        self.api = api
        self.commands = {}
        self.cancelled = set()
        self.gate_open = True
        self._commands_lock = threading.RLock()
        self.executor = None

    def handoff(self, step_id, kind):
        del step_id, kind
        return {"available": True, "confirmed": True, "reason": "sim_same_source"}

    def submit(self, command_id, request):
        with self._commands_lock:
            stored = self.commands.get(command_id)
            if stored and stored["request"] != request:
                raise RuntimeError("command_id 请求不一致")
            self.commands.setdefault(command_id, {
                "request": deepcopy(request), "view": None, "started": False,
                "done": threading.Event(),
            })
        return {"accepted": True, "unclear": False, "completed": False, "command_id": command_id}

    def wait(self, command_id, request):
        with self._commands_lock:
            stored = self.commands.get(command_id)
            if stored is None:
                return _unconfirmed(command_id, "unknown_command")
            if command_id in self.cancelled and not stored["started"]:
                stored["view"] = _cancelled_view(command_id)
                stored["done"].set()
            if not stored["started"] and not stored["done"].is_set():
                stored["started"] = True
                if self.executor is None:
                    threading.Thread(target=self._perform_once, args=(command_id, request), daemon=True).start()
                else:
                    self.executor.submit(self._perform_once, command_id, request)
        if not stored["done"].wait(float(request.get("deadline_sec") or 120)):
            return _unconfirmed(command_id, "timeout")
        return deepcopy(stored["view"])

    def _perform_once(self, command_id, request):
        try:
            viewed = self._execute_view(command_id, request)
        except Exception as exc:
            viewed = _unconfirmed(command_id, str(exc))
        with self._commands_lock:
            stored = self.commands[command_id]
            stored["view"] = viewed
            stored["done"].set()

    def _execute_view(self, command_id, request):
        subtask = ((request or {}).get("body") or {}).get("subtask") or ""
        action = parse_sim_action(subtask)
        raw = self._call(action, subtask)
        return _sim_view(command_id, action, raw, self._read_world(), self._read_zones())

    def query(self, command_id, request):
        with self._commands_lock:
            stored = self.commands.get(command_id)
            if stored and stored["request"] != request:
                return _unconfirmed(command_id, "command_request_mismatch")
            if stored and stored.get("view"):
                return deepcopy(stored["view"])
            if stored and not stored["started"]:
                if command_id in self.cancelled:
                    return _cancelled_view(command_id)
                return {"command_id": command_id, "terminal": "", "started": False,
                        "stopped": True, "resources_released": True, "evidence": {}}
        # An empty process-local cache after restart says nothing about remote execution.
        return _unconfirmed(command_id, "original_command_unavailable")

    def cancel(self, command_id, request):
        with self._commands_lock:
            stored = self.commands.get(command_id)
            if stored and stored["request"] != request:
                return {"accepted": False, "completed": False, "command_id": command_id,
                        "error": "command_request_mismatch"}
            if stored and not stored["started"]:
                self.cancelled.add(command_id)
                stored["view"] = _cancelled_view(command_id)
                stored["done"].set()
                return {"accepted": True, "completed": True, "command_id": command_id}
            viewed = (stored or {}).get("view") or {}
            if stored and stored["done"].is_set() and viewed.get("stopped") is True and viewed.get("resources_released") is True:
                # A completed synchronous call can have unknown task effect. Its confirmed
                # stop still permits cancellation; do not turn the effect into a success.
                if not viewed.get("terminal"):
                    stored["view"] = dict(viewed, terminal="cancelled")
                return {"accepted": True, "completed": True, "command_id": command_id}
        return {"accepted": False, "completed": False, "command_id": command_id,
                "error": "stop_unavailable"}

    def _call(self, action, subtask: str):
        if self._perform is not None:
            return self._perform(subtask)
        if action is None:
            return {"success": False, "result": f"仿真执行不认识这一步: {subtask}"}
        from execution.robot_api import client
        api = self.api or client

        kind = action[0]
        if kind == "grasp":
            return api.grasp_object(action[1])
        if kind == "place":
            return api.place_object(action[1], action[2])
        if kind == "navigate":
            return api.navigate_to(action[1])
        raise RuntimeError("复合桌面技能必须先展开为 Runtime 步骤")

    def _read_world(self):
        if self._world is not None:
            return self._world()
        from execution.robot_api import client
        api = self.api or client

        world = api.get_objects()
        return world if isinstance(world, dict) else {}

    def _read_zones(self):
        if self._zones is not None:
            return self._zones() if callable(self._zones) else self._zones
        if self.api is not None:
            return self.api.get_zones()
        try:
            from execution.robot_api.desk import get_zones

            return get_zones()
        except Exception:
            return {}


def _cancelled_view(command_id: str) -> dict:
    return {
        "command_id": command_id,
        "terminal": "cancelled",
        "timed_out": False,
        "started": False,
        "stopped": True,
        "resources_released": True,
        "evidence": {
            "supports": False,
            "contradicts": False,
            "identity_ok": True,
            "time_ok": True,
        },
    }


def load_capture_scene():
    from brain.adapters.camera import capture_scene
    return capture_scene
