"""Shared HTTP client for the independent brain API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import requests


class BrainError(RuntimeError):
    """Base error for calls to the brain service."""


class BrainOffline(BrainError):
    """The service cannot be reached or explicitly reports unavailability."""


class BrainRejected(BrainError):
    """The service returned a non-success response."""


class BrainBusy(BrainRejected):
    """The service declined a task because another task won the race."""


class BrainTaskIdMismatch(BrainRejected):
    """The accepted task identity differs from the requested identity."""


@dataclass(frozen=True)
class BrainStatus:
    raw: dict[str, Any]

    @property
    def active(self) -> bool:
        return bool(self.raw.get("active"))

    @property
    def all_done(self) -> bool:
        return bool(self.raw.get("all_done"))

    @property
    def busy(self) -> bool:
        return self.active and not self.all_done

    @property
    def task_id(self) -> str:
        return str(self.raw.get("task_id") or "")


@dataclass(frozen=True)
class BrainPreflight:
    raw: dict[str, Any]

    @property
    def ready(self) -> bool:
        return self.raw.get("ready") is True

    @property
    def required(self) -> bool:
        return self.raw.get("required") is True

    @property
    def blockers(self) -> list[str]:
        value = self.raw.get("blockers") or []
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    @property
    def recommended_tracking_timeout_sec(self) -> int:
        try:
            return max(0, int(self.raw.get("recommended_tracking_timeout_sec") or 0))
        except (TypeError, ValueError):
            return 0


class BrainClient:
    def __init__(self, base_url: str, *, timeout: float = 8.0, session=None, source="feishu") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.source = source
        self.session = session or requests.Session()

    async def get_status(self) -> BrainStatus:
        return await asyncio.to_thread(self._get_status)

    def _get_status(self) -> BrainStatus:
        try:
            response = self.session.get(
                f"{self.base_url}/api/task_status", timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise BrainOffline(f"大脑状态接口不可达：{exc}") from exc
        if response.status_code == 503:
            raise BrainOffline("大脑服务未启动")
        if not response.ok:
            raise BrainRejected(f"状态接口返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrainRejected("状态接口没有返回有效 JSON") from exc
        if not isinstance(payload, dict):
            raise BrainRejected("状态接口返回格式无效")
        return BrainStatus(payload)

    def _source_headers(self, operator=None):
        headers = {"X-FQ-Source": self.source}
        if operator:
            headers["X-FQ-Operator"] = str(operator)
        return headers

    async def control_task(self, action, task_id, *, operator=None, step_id=None, expected_command_id=None):
        if action not in {'cancel', 'pause', 'continue'} or not task_id:
            raise BrainRejected('控制动作或目标任务 ID 无效')
        body = {'task_id': task_id}
        if action == 'continue':
            if step_id is not None:
                body['step_id'] = step_id
            if expected_command_id is not None:
                body['expected_command_id'] = expected_command_id
        def call():
            try:
                response = self.session.post(
                    f'{self.base_url}/api/task_{action}', json=body,
                    timeout=max(self.timeout, 30), headers=self._source_headers(operator))
            except requests.RequestException as exc:
                raise BrainOffline(f'控制响应未确认，请查询原任务状态：{exc}') from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise BrainRejected('控制接口返回无效 JSON，请查询原任务状态') from exc
            if not response.ok or not isinstance(payload, dict):
                raise BrainRejected(f'控制接口返回 HTTP {response.status_code}')
            return payload
        return await asyncio.to_thread(call)

    async def publish_task(self, task: str, task_id: str, *, operator: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._publish_task, task, task_id, operator)

    async def task_preflight(self, task: str, *, operator: str | None = None) -> BrainPreflight:
        return await asyncio.to_thread(self._task_preflight, task, operator)

    def _task_preflight(self, task: str, operator=None) -> BrainPreflight:
        try:
            response = self.session.post(
                f"{self.base_url}/api/task_preflight",
                json={"task": task},
                timeout=max(self.timeout, 45.0),
                headers=self._source_headers(operator),
            )
        except requests.RequestException as exc:
            raise BrainOffline(f"大脑预检接口不可达：{exc}") from exc
        if response.status_code == 503:
            raise BrainOffline("大脑服务未启动")
        if not response.ok:
            raise BrainRejected(f"预检接口返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrainRejected("预检接口没有返回有效 JSON") from exc
        if not isinstance(payload, dict):
            raise BrainRejected("预检接口返回格式无效")
        return BrainPreflight(payload)

    def _publish_task(self, task: str, task_id: str, operator=None) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{self.base_url}/publish_task",
                json={"task": task, "task_id": task_id, "refresh": True},
                timeout=max(self.timeout, 120.0),
                headers=self._source_headers(operator),
            )
        except requests.RequestException as exc:
            raise BrainOffline(f"大脑任务接口不可达：{exc}") from exc
        if response.status_code == 503:
            raise BrainOffline("大脑服务未启动")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrainRejected(
                f"任务接口返回 HTTP {response.status_code}，且不是有效 JSON"
            ) from exc
        if not response.ok or payload.get("status") != "success":
            detail = (
                payload.get("error") or payload.get("details") or payload.get("message")
            )
            raise BrainRejected(
                str(detail or f"任务接口返回 HTTP {response.status_code}")
            )
        if payload.get("accepted") is False:
            raise BrainBusy(str(payload.get("message") or "大脑未接受任务"))
        returned_task_id = str(payload.get("task_id") or "")
        if returned_task_id != task_id:
            raise BrainTaskIdMismatch(
                "大脑任务ID不一致："
                f"requested={task_id}, returned={returned_task_id or '<missing>'}"
            )
        return payload

    def request(self, method, path, **kwargs):
        headers = self._source_headers()
        headers.update(kwargs.pop("headers", {}))
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, self.base_url + path, headers=headers, **kwargs)

    async def chat(self, text, *, route=False):
        def call():
            response = self.request("POST", "/api/chat/route" if route else "/api/chat", json={"task": text}, timeout=60)
            response.raise_for_status()
            return response.json()["answer"]
        return await asyncio.to_thread(call)
