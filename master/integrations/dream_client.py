"""DREAM Agent HTTP client used by the real reception orchestrator."""

from __future__ import annotations

import time
import urllib.parse

from .http_client import HttpClient, HttpContractError


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


class DreamRecoveryRequired(HttpContractError):
    """The original command may exist remotely; automatic resubmission is forbidden."""


class DreamClient:
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

    def status(self):
        return self.http.request_json("GET", "/v1/status")[0]

    def world(self):
        return self.http.request_json("GET", "/v1/world")[0]

    def camera_status(self):
        return self.http.request_json("GET", "/v1/camera/status")[0]

    def relation_graph(self, world=None):
        info = world or self.world()
        path = info.get("relation_graph_url") or "/total_scene_graph_latest.json"
        return self.http.request_json("GET", path)[0]

    def submit_navigation(
        self, payload, *, recovery_attempts=3, recovery_interval_sec=0.5
    ):
        try:
            body, _status, _headers = self.http.request_json(
                "POST",
                "/v1/navigation/goals",
                payload,
                accepted_statuses=(200, 202),
            )
            return body
        except HttpContractError as exc:
            # A definite HTTP error response is not ambiguous.  Connection-level
            # failure may have happened after DREAM accepted the command, so only
            # query the original command_id; never create or submit a new one.
            if exc.status_code is not None:
                raise
            command_id = payload.get("command_id")
            if not command_id:
                raise DreamRecoveryRequired(
                    "导航POST连接异常且请求缺少command_id，禁止自动重发",
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
            raise DreamRecoveryRequired(
                f"导航POST结果不确定，原command_id有界重查仍无法确认: {command_id}",
                payload={"command_id": command_id, "cause": str(exc)},
            ) from exc

    def submit_inspection(self, payload):
        return self.http.request_json(
            "POST", "/v1/inspection", payload,
            accepted_statuses=(200, 202),
        )[0]

    def wait_camera_owner(
        self, owner, *, timeout_sec=60.0, poll_interval_sec=0.5
    ):
        deadline = time.monotonic() + float(timeout_sec)
        last = None
        while time.monotonic() < deadline:
            last = self.camera_status()
            if (
                last.get("driver_enabled") is False
                and str(last.get("external_owner") or "").lower() == str(owner).lower()
            ):
                return last
            time.sleep(float(poll_interval_sec))
        raise HttpContractError(
            f"DREAM相机未在限定时间内交给{owner}", payload=last)

    def command(self, command_id):
        quoted = urllib.parse.quote(str(command_id), safe="")
        return self.http.request_json(
            "GET", f"/v1/commands/{quoted}")[0]

    def _bounded_lookup(self, command_id, *, attempts=3, interval_sec=0.5):
        for attempt in range(max(1, int(attempts))):
            try:
                return self.command(command_id)
            except HttpContractError as exc:
                if exc.status_code not in {None, 404}:
                    raise
                if attempt + 1 < max(1, int(attempts)):
                    time.sleep(float(interval_sec))
        return None

    def wait_command(
        self, command_id, *, timeout_sec, poll_interval_sec=0.5, on_update=None,
        uncertain_attempts=3,
    ):
        deadline = time.monotonic() + float(timeout_sec)
        last = None
        uncertain_count = 0
        last_uncertain_error = None
        while time.monotonic() < deadline:
            try:
                last = self.command(command_id)
                uncertain_count = 0
                last_uncertain_error = None
            except HttpContractError as exc:
                if exc.status_code not in {None, 404}:
                    raise
                uncertain_count += 1
                last_uncertain_error = exc
                if uncertain_count >= max(1, int(uncertain_attempts)):
                    raise DreamRecoveryRequired(
                        f"DREAM原命令连续无法确认，禁止补发动作: {command_id}",
                        payload={
                            "command_id": command_id,
                            "attempts": uncertain_count,
                            "cause": str(last_uncertain_error),
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
            f"DREAM命令等待终态超时: {command_id}",
            payload=last,
        )

    def wait_navigation_transport(
        self, *, timeout_sec=30.0, poll_interval_sec=0.5, on_update=None
    ):
        deadline = time.monotonic() + float(timeout_sec)
        last = None
        while time.monotonic() < deadline:
            last = self.status()
            if on_update:
                on_update(last)
            if last.get("navigation_transport_ready") is True:
                return last
            time.sleep(float(poll_interval_sec))
        raise HttpContractError(
            "DREAM导航通路在限定时间内未归还",
            payload=last,
        )

    def cancel(self, task_id, command_id, reason="operator_cancelled"):
        return self.http.request_json(
            "POST",
            "/v1/navigation/cancel",
            {
                "task_id": task_id,
                "command_id": command_id,
                "reason": reason,
            },
            accepted_statuses=(200, 202),
        )[0]

    @staticmethod
    def require_success(terminal, command_id):
        state = str(terminal.get("state") or "").lower()
        result = terminal.get("result") or {}
        if not (
            state == "succeeded"
            and result.get("success") is True
            and result.get("reached") is True
            and result.get("navigation_stopped") is True
        ):
            raise HttpContractError(
                f"DREAM导航成功证据不完整: {command_id}, state={state}",
                payload=terminal,
            )
        return terminal
