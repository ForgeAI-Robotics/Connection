"""Wrap DreamClient and VlaClient. Missing handoff queries stay unavailable."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta

from kernel.contracts import CONTRACT_VERSION
from kernel.memory import DEFAULT_EVENT_TTL_SEC
from integrations.http_client import HttpContractError


class BodyAdapter:
    """Real DREAM/VLA wrapper. It does not invent an NX start or stop call."""

    contract_version = CONTRACT_VERSION

    def __init__(self, dream, vla):
        self.dream = dream
        self.vla = vla

    @classmethod
    def from_config(cls, config):
        from integrations.dream_client import DreamClient
        from integrations.vla_client import VlaClient

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
            return _unconfirmed(command_id, str(exc))
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
            return _unconfirmed(command_id, str(exc))
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


def _unconfirmed(command_id, error: str) -> dict:
    return {
        "command_id": command_id,
        "terminal": None,
        "timed_out": True,
        "started": None,
        "stopped": False,
        "resources_released": False,
        "evidence": {},
        "error": error,
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
    if skill == "pick":
        return (
            result.get("success") is True
            and result.get("object_grasped") is True
            and result.get("holding") == object_id
        )
    if skill == "place":
        return (
            result.get("success") is True
            and result.get("object_grasped") is False
            and result.get("released") is True
            and result.get("holding") is None
            and result.get("object_at_target") is True
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
    return {
        "command_id": command_id,
        "terminal": terminal,
        "timed_out": False,
        "started": _started(raw, result),
        "stopped": bool(stopped),
        "resources_released": bool(resources),
        "evidence": {
            "supports": supports,
            "contradicts": _contradicts(skill, result),
            "identity_ok": True,
            "time_ok": _timestamp_has_timezone(raw.get("completed_at")),
            "grade": grade,
            "identity": command_id,
        },
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
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self.capture, task)
        try:
            return future.result(timeout=deadline)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)


def load_capture_scene():
    """Import the slaver camera function without importing it at kernel startup."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "slaver" / "robot" / "module" / "camera.py"
    spec = importlib.util.spec_from_file_location("slaver_camera_scene", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到 camera.capture_scene")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.capture_scene
