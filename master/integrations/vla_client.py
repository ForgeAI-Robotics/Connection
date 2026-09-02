"""VLA task and VLA-owned RealSense HTTP client."""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from datetime import datetime

from .http_client import HttpClient, HttpContractError


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


class VlaRecoveryRequired(HttpContractError):
    """The VLA action may already exist remotely; automatic resubmission is forbidden."""


class VlaClient:
    def __init__(
        self, base_url, *, contract_version="fq/reception-lan/v1",
        request_timeout_sec=10.0,
    ):
        self.http = HttpClient(
            base_url,
            timeout_sec=request_timeout_sec,
            contract_version=contract_version,
        )
        self.contract_version = contract_version

    def health(self):
        return self.http.request_json("GET", "/health")[0]

    def control_status(self):
        return self.http.request_json(
            "GET", "/v1/vla/control/status")[0]

    def camera_status(self):
        return self.http.request_json("GET", "/v1/camera/status")[0]

    @staticmethod
    def _definitely_not_started(error):
        payload = getattr(error, "payload", None)
        if not isinstance(payload, dict):
            return False
        details = ((payload.get("error") or {}).get("details") or {})
        return (
            details.get("action_started") is False
            and details.get("action_state_uncertain") is False
        )

    def submit_task(
        self, payload, *, recovery_attempts=4, recovery_interval_sec=1.0
    ):
        try:
            return self.http.request_json(
                "POST",
                "/v1/vla/tasks",
                payload,
                accepted_statuses=(200, 202),
            )[0]
        except HttpContractError as exc:
            # 409 and an explicit "action_started=false" response are definite.
            # Network loss/504 may occur after the physical action was accepted.
            if exc.status_code == 409 or self._definitely_not_started(exc):
                raise
            if exc.status_code not in {None, 504}:
                raise
            command_id = payload.get("command_id")
            if not command_id:
                raise VlaRecoveryRequired(
                    "VLA POST状态不确定且缺少command_id，禁止自动重发",
                    payload={"cause": str(exc)},
                ) from exc
            recovered = self._bounded_lookup(
                command_id,
                attempts=recovery_attempts,
                interval_sec=recovery_interval_sec,
            )
            if recovered is not None:
                return {
                    "accepted": True,
                    "command_id": command_id,
                    "state": recovered.get("state"),
                    "recovered_after_post_error": True,
                    "detail": recovered,
                }
            raise VlaRecoveryRequired(
                f"VLA POST结果不确定，原command_id有界重查仍无法确认: {command_id}",
                payload={"command_id": command_id, "cause": str(exc)},
            ) from exc

    def task(self, command_id):
        quoted = urllib.parse.quote(str(command_id), safe="")
        return self.http.request_json(
            "GET", f"/v1/vla/tasks/{quoted}")[0]

    def _bounded_lookup(self, command_id, *, attempts=4, interval_sec=1.0):
        for attempt in range(max(1, int(attempts))):
            try:
                return self.task(command_id)
            except HttpContractError as exc:
                if exc.status_code not in {None, 404}:
                    raise
                if attempt + 1 < max(1, int(attempts)):
                    time.sleep(float(interval_sec))
        return None

    def wait_task(
        self, command_id, *, timeout_sec, poll_interval_sec=1.0, on_update=None,
        uncertain_attempts=4,
    ):
        deadline = time.monotonic() + float(timeout_sec)
        last = None
        uncertain_count = 0
        while time.monotonic() < deadline:
            try:
                last = self.task(command_id)
                uncertain_count = 0
            except HttpContractError as exc:
                if exc.status_code not in {None, 404, 504}:
                    raise
                uncertain_count += 1
                if uncertain_count >= max(1, int(uncertain_attempts)):
                    raise VlaRecoveryRequired(
                        f"VLA原任务连续无法确认，禁止补发动作: {command_id}",
                        payload={
                            "command_id": command_id,
                            "attempts": uncertain_count,
                            "cause": str(exc),
                            "last": last,
                        },
                    ) from exc
                time.sleep(float(poll_interval_sec))
                continue
            if on_update:
                on_update(last)
            state = str(last.get("state") or "").lower()
            if state in TERMINAL_STATES:
                return last
            time.sleep(float(poll_interval_sec))
        raise HttpContractError(
            f"VLA任务等待终态超时: {command_id}",
            payload=last,
        )

    def cancel(self, task_id, command_id, reason="operator_cancelled"):
        quoted = urllib.parse.quote(str(command_id), safe="")
        return self.http.request_json(
            "POST",
            f"/v1/vla/tasks/{quoted}/cancel",
            {"task_id": task_id, "reason": reason},
            accepted_statuses=(200, 202),
        )[0]

    def create_snapshot(self, payload, *, timeout_sec=None):
        return self.http.request_json(
            "POST",
            "/v1/camera/snapshots",
            payload,
            accepted_statuses=(200, 201),
            timeout_sec=timeout_sec,
        )[0]

    def download_snapshot(self, snapshot, *, timeout_sec=None):
        path = snapshot.get("rgb_url")
        if not path:
            raise HttpContractError("VLA快照响应缺少rgb_url", payload=snapshot)
        raw, status, headers = self.http.request_bytes(
            "GET", path, timeout_sec=timeout_sec)
        if status != 200 or not raw:
            raise HttpContractError(
                f"下载VLA快照失败: HTTP {status}", payload=snapshot)
        expected = str(snapshot.get("sha256") or "").lower()
        actual = hashlib.sha256(raw).hexdigest()
        if expected and expected != actual:
            raise HttpContractError(
                "VLA快照SHA256不匹配",
                payload={"expected": expected, "actual": actual},
            )
        normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
        content_type = normalized_headers.get("content-type", "").split(";", 1)[0]
        if content_type not in {"image/jpeg", "image/png"}:
            raise HttpContractError(
                f"VLA快照Content-Type非法: {content_type!r}", payload=snapshot)
        header_snapshot = normalized_headers.get("x-snapshot-id")
        header_captured = normalized_headers.get("x-captured-at")
        header_sha = normalized_headers.get("x-content-sha256", "").lower()
        if header_snapshot != str(snapshot.get("snapshot_id") or ""):
            raise HttpContractError("VLA快照X-Snapshot-Id不匹配", payload=snapshot)
        if header_captured != str(snapshot.get("captured_at") or ""):
            raise HttpContractError("VLA快照X-Captured-At不匹配", payload=snapshot)
        if header_sha != actual:
            raise HttpContractError("VLA快照X-Content-SHA256不匹配", payload=snapshot)
        return raw, headers

    @staticmethod
    def _timestamp_has_timezone(value):
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return False
        return parsed.tzinfo is not None

    def _require_identity(self, terminal, *, task_id, command_id, operation, object_id):
        result = terminal.get("result") or {}
        checks = (
            terminal.get("contract_version") == self.contract_version,
            terminal.get("task_id") == task_id,
            terminal.get("command_id") == command_id,
            terminal.get("operation") == operation,
            result.get("object_id") == object_id,
            self._timestamp_has_timezone(terminal.get("completed_at")),
        )
        if not all(checks):
            raise HttpContractError(
                f"VLA {operation}终态身份或时间字段不匹配",
                payload=terminal,
            )
        return result

    def require_pick_success(
        self, terminal, *, task_id, command_id, object_id,
        result_policy="strict_object_evidence",
    ):
        state = str(terminal.get("state") or "").lower()
        result = self._require_identity(
            terminal,
            task_id=task_id,
            command_id=command_id,
            operation="pick",
            object_id=object_id,
        )
        checks = (
            state == "succeeded",
            result.get("success") is True,
            result.get("object_grasped") is True,
            result.get("holding") == object_id,
            result.get("policy_stopped") is True,
            result.get("navigation_port_ready") is True,
        )
        if result_policy == "hand_state_only":
            evidence = result.get("evidence") or {}
            checks += (
                result.get("evidence_level") == "hand_state_only",
                evidence.get("hand") == "left",
                evidence.get("hand_closed_confirmed") is True,
                int(evidence.get("confirmed_frames") or 0) >= 3,
            )
        elif result_policy != "strict_object_evidence":
            raise HttpContractError(
                f"不支持的VLA结果策略: {result_policy}", payload=terminal)
        if not all(checks):
            raise HttpContractError(
                f"VLA抓取终态不满足推进条件: state={state}",
                payload=terminal,
            )
        return terminal

    def require_place_success(
        self, terminal, *, task_id, command_id, object_id,
        result_policy="strict_object_evidence",
    ):
        state = str(terminal.get("state") or "").lower()
        result = self._require_identity(
            terminal,
            task_id=task_id,
            command_id=command_id,
            operation="place",
            object_id=object_id,
        )
        checks = (
            state == "succeeded",
            result.get("success") is True,
            result.get("object_grasped") is False,
            result.get("released") is True,
            result.get("holding") is None,
            result.get("policy_stopped") is True,
            result.get("navigation_port_ready") is True,
        )
        if result_policy == "hand_state_only":
            evidence = result.get("evidence") or {}
            checks += (
                result.get("object_at_target") in {None, True},
                result.get("evidence_level") == "hand_state_only",
                evidence.get("hand") == "left",
                evidence.get("hand_open_confirmed") is True,
                int(evidence.get("confirmed_frames") or 0) >= 3,
            )
        elif result_policy == "strict_object_evidence":
            checks += (result.get("object_at_target") is True,)
        else:
            raise HttpContractError(
                f"不支持的VLA结果策略: {result_policy}", payload=terminal)
        if not all(checks):
            raise HttpContractError(
                f"VLA放置终态不满足推进条件: state={state}, object={object_id}",
                payload=terminal,
            )
        return terminal
