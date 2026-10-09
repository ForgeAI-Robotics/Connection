"""DREAM Agent HTTP client used by the real reception orchestrator."""

from __future__ import annotations

import time
import threading
import urllib.parse

from .http_client import HttpClient, HttpContractError

from shared.brain_journal import CallBook, OutboundCall, call_reason, summarize_nav_request


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
        self.calls = CallBook("dream")
        self._wait_abandoned = threading.Event()

    def abandon_waits(self):
        self._wait_abandoned.set()

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
        call = self.calls.start(
            "navigate",
            command_id=payload.get("command_id"),
            task_id=payload.get("task_id"),
            **summarize_nav_request(payload),
        )
        try:
            body, _status, _headers = self.http.request_json(
                "POST",
                "/v1/navigation/goals",
                payload,
                accepted_statuses=(200, 202),
            )
            call.accepted(body)
            return body
        except HttpContractError as exc:
            # A definite HTTP error response is not ambiguous.  Connection-level
            # failure may have happened after DREAM accepted the command, so only
            # query the original command_id; never create or submit a new one.
            if exc.status_code is not None:
                self.calls.complete(
                    payload.get("command_id"), False, payload=exc.payload, error=exc
                )
                raise
            command_id = payload.get("command_id")
            if not command_id:
                self.calls.complete(
                    payload.get("command_id"), False, payload=exc.payload, error=exc
                )
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
                call.accepted(recovered)
                return {
                    "accepted": True,
                    "command_id": command_id,
                    "state": recovered.get("state"),
                    "recovered_after_post_error": True,
                    "detail": recovered,
                }
            self.calls.complete(command_id, False, payload=exc.payload, error=exc)
            raise DreamRecoveryRequired(
                f"导航POST结果不确定，原command_id有界重查仍无法确认: {command_id}",
                payload={"command_id": command_id, "cause": str(exc)},
            ) from exc

    def submit_inspection(self, payload):
        call = self.calls.start(
            "inspect",
            command_id=payload.get("command_id"),
            task_id=payload.get("task_id"),
            method="POST",
            path="/v1/inspection",
            target=payload.get("target_object_id"),
        )
        try:
            body = self.http.request_json(
                "POST", "/v1/inspection", payload,
                accepted_statuses=(200, 202),
            )[0]
            call.accepted(body)
            return body
        except Exception as exc:
            self.calls.complete(
                payload.get("command_id"), False,
                payload=getattr(exc, "payload", None), error=exc,
            )
            raise

    def wait_camera_owner(
        self, owner, *, timeout_sec=60.0, poll_interval_sec=0.5
    ):
        call = OutboundCall("dream", "camera_handoff", note=owner)
        deadline = time.monotonic() + float(timeout_sec)
        last = None
        while time.monotonic() < deadline:
            last = self.camera_status()
            ready = (
                last.get("driver_enabled") is False
                and str(last.get("external_owner") or "").lower() == str(owner).lower()
            )
            call.update({
                "state": "ready" if ready else "waiting",
                "wait_reason": (
                    f"owner={last.get('external_owner')}"
                    f" driver={last.get('driver_enabled')}"
                ),
            })
            if ready:
                call.end(True, payload=last)
                return last
            time.sleep(float(poll_interval_sec))
        error = HttpContractError(
            f"DREAM相机未在限定时间内交给{owner}", payload=last)
        call.end(False, payload=last, error=error)
        raise error

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
        call = self.calls.ensure("wait", command_id)
        while time.monotonic() < deadline:
            if self._wait_abandoned.is_set():
                raise HttpContractError("本轮大脑任务已取消，结束本地轮询")
            try:
                last = self.command(command_id)
                uncertain_count = 0
                last_uncertain_error = None
            except HttpContractError as exc:
                if exc.status_code not in {None, 404}:
                    self.calls.complete(
                        command_id, False, payload=exc.payload, error=exc
                    )
                    raise
                uncertain_count += 1
                last_uncertain_error = exc
                if uncertain_count >= max(1, int(uncertain_attempts)):
                    self.calls.complete(
                        command_id, False, payload=last, error=exc
                    )
                    raise DreamRecoveryRequired(
                        f"DREAM原命令连续无法确认，禁止补发动作: {command_id}",
                        payload={
                            "command_id": command_id,
                            "attempts": uncertain_count,
                            "cause": str(last_uncertain_error),
                            "last": last,
                        },
                    ) from exc
                self._wait_abandoned.wait(float(poll_interval_sec))
                continue
            if on_update:
                on_update(last)
            call.update(last)
            state = str(last.get("state") or "").lower()
            if state in TERMINAL_STATES:
                if state != "succeeded":
                    self.calls.complete(command_id, False, payload=last)
                return last
            self._wait_abandoned.wait(float(poll_interval_sec))
        error = HttpContractError(
            f"DREAM命令等待终态超时: {command_id}",
            payload=last,
        )
        self.calls.complete(command_id, False, payload=last, error=error)
        raise error

    def wait_navigation_transport(
        self, *, timeout_sec=30.0, poll_interval_sec=0.5, on_update=None
    ):
        call = OutboundCall("dream", "wait_transport")
        deadline = time.monotonic() + float(timeout_sec)
        last = None
        while time.monotonic() < deadline:
            last = self.status()
            if on_update:
                on_update(last)
            blockers = last.get("motion_blockers") or []
            ready = last.get("navigation_transport_ready") is True
            call.update({
                "state": "ready" if ready else "waiting",
                "wait_reason": "；".join(str(item) for item in blockers),
            })
            if ready:
                call.end(True, payload=last)
                return last
            time.sleep(float(poll_interval_sec))
        call.end(False, payload=last)
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

    def require_success(self, terminal, command_id):
        state = str(terminal.get("state") or "").lower()
        result = terminal.get("result") or {}
        unmet = [
            name
            for name, satisfied in (
                ("state=succeeded", state == "succeeded"),
                ("success=true", result.get("success") is True),
                ("reached=true", result.get("reached") is True),
                ("navigation_stopped=true", result.get("navigation_stopped") is True),
            )
            if not satisfied
        ]
        if unmet:
            parts = [
                f"DREAM导航未达成: {command_id}",
                f"state={state or '未知'}",
                f"未满足: {'、'.join(unmet)}",
            ]
            error_body = (
                terminal.get("error")
                if isinstance(terminal.get("error"), dict) else {}
            )
            code = error_body.get("code")
            if code:
                parts.append(f"code={code}")
            reason = call_reason(terminal)
            if reason:
                parts.append(f"reason={reason}")
            error = HttpContractError(", ".join(parts), payload=terminal)
            self.calls.complete(command_id, False, payload=terminal, error=error)
            raise error
        self.calls.complete(command_id, True, payload=terminal)
        return terminal
