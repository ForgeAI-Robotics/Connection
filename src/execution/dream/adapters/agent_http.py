"""DREAM G1 Agent HTTP v1 client.

The 2026-08-21 contract is asynchronous: POST only accepts a command and the
caller must poll ``/v1/commands/{command_id}`` until a terminal state.  This
module deliberately contains no real-control enable switch; callers such as
``service.server`` must enforce their own local safety gate before invoking a
mutating method.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
ALLOWED_MOTION_MODES = {"forward_path", "lateral_path_aligned"}


class AgentHttpError(RuntimeError):
    """Raised when the DREAM Agent HTTP contract cannot be completed."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _finite(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数")
    return number


def build_navigation_payload(
    x,
    y,
    yaw,
    *,
    yaw_unit="degrees",
    command_id=None,
    task_id=None,
    target_id=None,
    leg_index=1,
    frame_id="map",
    motion_mode="forward_path",
    require_final_orientation=True,
    route_phase=None,
    contract_version="fq/reception-lan/v1",
):
    """Build and validate a ``dream/g1-agent-*/v1`` navigation command."""
    if frame_id != "map":
        raise ValueError("DREAM Agent HTTP 只接受 frame_id=map")
    if motion_mode not in ALLOWED_MOTION_MODES:
        raise ValueError(f"不支持的 motion_mode: {motion_mode}")
    yaw_value = _finite(yaw, "yaw")
    if str(yaw_unit).lower() in {"degree", "degrees", "deg"}:
        yaw_value = math.radians(yaw_value)
    elif str(yaw_unit).lower() not in {"radian", "radians", "rad"}:
        raise ValueError(f"不支持的 yaw_unit: {yaw_unit}")

    cmd = command_id or f"fq-nav-{uuid.uuid4().hex[:16]}"
    task = task_id or f"fq-agent-{uuid.uuid4().hex[:16]}"
    payload = {
        "contract_version": str(contract_version),
        "command_id": str(cmd),
        "task_id": str(task),
        "target_id": str(target_id or "agent_map_goal"),
        "leg_index": int(leg_index),
        "frame_id": "map",
        "goal_xyt": [_finite(x, "x"), _finite(y, "y"), yaw_value],
        "motion_mode": motion_mode,
        "require_final_orientation": bool(require_final_orientation),
        "route_phase": str(route_phase or ""),
    }
    return payload


class AgentHttpClient:
    def __init__(self, base_url, request_timeout_sec=10.0):
        self.base_url = str(base_url).rstrip("/")
        if not self.base_url:
            raise ValueError("Agent HTTP base_url 不能为空")
        self.request_timeout_sec = float(request_timeout_sec)

    def _url(self, path):
        if str(path).startswith(("http://", "https://")):
            return str(path)
        return f"{self.base_url}/{str(path).lstrip('/')}"

    def request_bytes(self, method, path, payload=None, timeout=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self._url(path), data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(
                req, timeout=float(timeout or self.request_timeout_sec)
            ) as resp:
                return resp.read(), resp.status, resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw.decode("utf-8"))
            except Exception:
                detail = raw.decode("utf-8", errors="replace")
            raise AgentHttpError(
                f"DREAM Agent HTTP {exc.code}: {detail}", exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise AgentHttpError(f"DREAM Agent HTTP 不可达: {exc}") from exc

    def request_json(self, method, path, payload=None, timeout=None):
        raw, status, _content_type = self.request_bytes(method, path, payload, timeout)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            raise AgentHttpError(f"DREAM 返回非 JSON 响应(HTTP {status})") from exc
        return body, status

    def get_world(self):
        return self.request_json("GET", "/v1/world")[0]

    def get_status(self):
        return self.request_json("GET", "/v1/status")[0]

    def get_camera_status(self):
        return self.request_json("GET", "/v1/camera/status")[0]

    def get_command(self, command_id):
        quoted = urllib.parse.quote(str(command_id), safe="")
        return self.request_json("GET", f"/v1/commands/{quoted}")[0]

    def submit_navigation(self, payload):
        body, status = self.request_json("POST", "/v1/navigation/goals", payload)
        if status not in {200, 202}:
            raise AgentHttpError(f"导航命令未被接受: HTTP {status}", status, body)
        return body

    def submit_inspection(self, payload):
        body, status = self.request_json("POST", "/v1/inspection", payload)
        if status not in {200, 202}:
            raise AgentHttpError(f"细检命令未被接受: HTTP {status}", status, body)
        return body

    def cancel_navigation(self, command_id):
        return self.request_json(
            "POST", "/v1/navigation/cancel", {"command_id": command_id})[0]

    def wait_command(self, command_id, timeout_sec=180.0, poll_interval_sec=0.5):
        deadline = time.monotonic() + float(timeout_sec)
        last = None
        while time.monotonic() < deadline:
            last = self.get_command(command_id)
            state = str(last.get("state") or "").lower()
            if state in TERMINAL_STATES:
                return last
            time.sleep(float(poll_interval_sec))
        raise AgentHttpError(
            f"命令 {command_id} 等待终态超时({timeout_sec}s); 最后状态={last}",
            payload=last,
        )

    def navigate(self, payload, timeout_sec=180.0, poll_interval_sec=0.5):
        status = self.get_status()
        if status.get("navigation_transport_ready") is not True:
            raise AgentHttpError(
                "DREAM导航通路未就绪",
                payload=status,
            )
        accepted = self.submit_navigation(payload)
        command_id = accepted.get("command_id") or payload["command_id"]
        terminal = self.wait_command(command_id, timeout_sec, poll_interval_sec)
        state = str(terminal.get("state") or "").lower()
        result = terminal.get("result") or {}
        success = (
            state == "succeeded"
            and result.get("success") is True
            and result.get("reached") is True
            and result.get("navigation_stopped") is True
        )
        accepted_pose = result.get("accepted_pose") or result.get("final_pose_xyt_rad")
        response = {
            "success": success,
            "result": (
                f"DREAM 导航完成({command_id})"
                if success else f"DREAM 导航未成功({command_id}): state={state}"
            ),
            "command_id": command_id,
            "state": state,
            "detail": terminal,
        }
        if isinstance(accepted_pose, (list, tuple)) and len(accepted_pose) >= 2:
            response["pos"] = [float(accepted_pose[0]), float(accepted_pose[1]), 0.0]
            if len(accepted_pose) >= 3:
                response["yaw"] = math.degrees(float(accepted_pose[2]))
        return response

    def inspect(self, payload, timeout_sec=300.0, poll_interval_sec=1.0):
        accepted = self.submit_inspection(payload)
        command_id = accepted.get("command_id") or payload["command_id"]
        terminal = self.wait_command(command_id, timeout_sec, poll_interval_sec)
        state = str(terminal.get("state") or "").lower()
        result = terminal.get("result") or {}
        success = state == "succeeded"
        return {
            "success": success,
            "result": (
                f"DREAM 细检完成({command_id})"
                if success else f"DREAM 细检未成功({command_id}): state={state}"
            ),
            "command_id": command_id,
            "state": state,
            "relation_graph_url": result.get("relation_graph_url"),
            "six_d_sync_active": result.get("six_d_sync_active"),
            "detail": terminal,
        }


def extract_robot_pose(status):
    """Best-effort extraction without inventing a pose when v1/status omits it."""
    candidates = [
        status.get("robot_xyt"),
        status.get("pose_xyt"),
        status.get("current_pose"),
        (status.get("navigation") or {}).get("robot_xyt"),
        (status.get("localization") or {}).get("robot_xyt"),
    ]
    for value in candidates:
        if isinstance(value, dict):
            value = [value.get("x"), value.get("y"), value.get("yaw")]
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            try:
                x, y, yaw = (_finite(value[0], "x"), _finite(value[1], "y"),
                             _finite(value[2], "yaw"))
                return {
                    "pos": [x, y, 0.0],
                    "yaw_rad": round(yaw, 4),
                    "yaw_deg": round(math.degrees(yaw) % 360.0, 2),
                }
            except ValueError:
                continue
    return None
