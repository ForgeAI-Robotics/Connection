"""General-knowledge replies for Feishu chat. Does not command the robot."""

from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from contracts.intent import ROUTE_SYSTEM




CHAT_SYSTEM = """你是 FQPlanner 飞书助手，只回答通用知识、闲聊和常识问题。
不要声称正在控制机器人，也不要假装已经看到现场画面。
如果用户其实是在下机器人任务（接待、抓取、导航、整理桌面、看桌上/前面有什么），请让他们改口发送具体任务，例如「开始接待」或「桌上有什么」。
天气、路况等实时信息若没有实时数据，必须明确说这是模型估计，不是当天实况。
用简体中文，简洁直接。"""


class DeepSeekChat:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        timeout: float = 45.0,
    ) -> None:
        self.api_key = (api_key or os.getenv("CLOUD_API_KEY") or "").strip()
        base = (api_base or os.getenv("CLOUD_API_BASE") or "https://api.deepseek.com").strip()
        self.api_base = base.rstrip("/")
        self.model = (model or os.getenv("CLOUD_CHAT_MODEL") or "deepseek-chat").strip()
        self.timeout = timeout

    def _endpoint(self) -> str:
        if self.api_base.endswith("/v1"):
            return f"{self.api_base}/chat/completions"
        return f"{self.api_base}/chat/completions"

    def _complete(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        timeout: float | None = None,
        empty_error: str,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 CLOUD_API_KEY，无法回答通用问题")
        response = requests.post(
            self._endpoint(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=timeout or self.timeout,
        )
        if not response.ok:
            raise RuntimeError(f"DeepSeek 接口返回 HTTP {response.status_code}")
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("DeepSeek 没有返回内容")
        message = (choices[0].get("message") or {}).get("content")
        answer = str(message or "").strip()
        if not answer:
            raise RuntimeError(empty_error)
        return answer

    def _reply(self, text: str) -> str:
        return self._complete(
            [
                {"role": "system", "content": CHAT_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=800,
            empty_error="DeepSeek 返回空内容",
        )

    def _route(self, text: str) -> str:
        return self._complete(
            [
                {"role": "system", "content": ROUTE_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=16,
            timeout=min(15.0, self.timeout),
            empty_error="DeepSeek 分流返回空内容",
        )

    async def reply(self, text: str) -> str:
        import asyncio

        return await asyncio.to_thread(self._reply, text)

    async def route(self, text: str) -> str:
        import asyncio

        return await asyncio.to_thread(self._route, text)
