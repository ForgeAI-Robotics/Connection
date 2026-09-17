"""Shared entry classifier for Feishu, the 8888 console, and Master.

Coarse gate only: chat vs company task, and whether motion needs confirm.
Skill choice (reception / look / planner) stays in Master.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

from robot_api.look import is_look_task


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    MOTION = "motion"
    AMBIGUOUS = "ambiguous"


class Intent(str, Enum):
    CHAT = "chat"
    OBSERVE = "observe"
    TASK = "task"


READ_ONLY_KEYWORDS = (
    "查看",
    "查询",
    "获取",
    "读取",
    "统计",
    "多少",
    "状态",
    "位置",
    "列表",
)

MOTION_KEYWORDS = (
    "导航",
    "前往",
    "移动",
    "抓取",
    "拿",
    "取",
    "放置",
    "放到",
    "整理",
    "清理",
    "打开",
    "关闭",
    "启动",
    "执行",
    "接待",
    "补货",
    "迎宾",
)

CHAT_KEYWORDS = (
    "天气",
    "气温",
    "下雨",
    "下雪",
    "好吃",
    "美食",
    "旅游",
    "景点",
    "你好",
    "您好",
    "你是谁",
    "你能做",
    "介绍一下你",
    "讲个笑话",
    "百科",
)

ROUTE_SYSTEM = """你是消息分流器。只根据用户这句话判断意图，只输出一个英文词：chat 或 task。

chat：通用知识、闲聊、地点、公司介绍、百科、天气、美食等，不需要看机器人现场，也不需要机器人动手。
task：公司任务。包括让机器人执行动作，以及看现场/桌上/镜头/前面现在有什么。

拿不准、可能动手或看现场 → task。不要解释，不要标点。"""


@dataclass(frozen=True)
class Classification:
    intent: Intent
    risk: RiskLevel
    read_only_matches: tuple[str, ...] = ()
    motion_matches: tuple[str, ...] = ()

    @property
    def requires_confirmation(self) -> bool:
        if self.intent is not Intent.TASK:
            return False
        return self.risk is not RiskLevel.READ_ONLY


def classify_task(text: str) -> Classification:
    """Two buckets: chat vs company task.

    Motion always wins. Looking at the desk/front is a read-only company task
    for the brain. Remaining knowledge questions go to chat. Text that cannot
    be proven read-only is an ambiguous robot task and requires confirmation.
    """

    normalized = (text or "").strip().lower()
    read_matches = tuple(word for word in READ_ONLY_KEYWORDS if word in normalized)
    motion_matches = tuple(word for word in MOTION_KEYWORDS if word in normalized)

    if motion_matches:
        return Classification(
            Intent.TASK, RiskLevel.MOTION, read_matches, motion_matches
        )

    if is_look_task(normalized):
        return Classification(
            Intent.TASK, RiskLevel.READ_ONLY, read_matches, motion_matches
        )

    if any(word in normalized for word in CHAT_KEYWORDS) or _looks_like_chat(normalized):
        return Classification(
            Intent.CHAT, RiskLevel.READ_ONLY, read_matches, motion_matches
        )

    if read_matches:
        return Classification(
            Intent.TASK, RiskLevel.READ_ONLY, read_matches, motion_matches
        )
    return Classification(
        Intent.TASK, RiskLevel.AMBIGUOUS, read_matches, motion_matches
    )


def needs_llm_route(classification: Classification) -> bool:
    """Only leftover ambiguous text goes to the LLM."""
    return (
        classification.intent is Intent.TASK
        and classification.risk is RiskLevel.AMBIGUOUS
    )


def parse_route_intent(raw: str) -> Intent | None:
    text = (raw or "").strip().lower()
    if not text:
        return None
    token = "".join(ch for ch in text.replace("`", "").split()[0] if ch.isalnum())
    aliases = {
        "chat": Intent.CHAT,
        "问答": Intent.CHAT,
        "闲聊": Intent.CHAT,
        "observe": Intent.OBSERVE,
        "看图": Intent.OBSERVE,
        "现场": Intent.OBSERVE,
        "task": Intent.TASK,
        "任务": Intent.TASK,
    }
    if token in aliases:
        return aliases[token]
    for label, intent in (
        ("observe", Intent.OBSERVE),
        ("chat", Intent.CHAT),
        ("task", Intent.TASK),
    ):
        if label in text:
            return intent
    return None


def refine_with_llm(
    classification: Classification, raw_label: str, text: str = ""
) -> Classification:
    """Overlay LLM intent onto an ambiguous leftover. Never overrides motion."""
    if classification.risk is RiskLevel.MOTION:
        return classification
    intent = parse_route_intent(raw_label)
    if intent is Intent.CHAT:
        return Classification(
            Intent.CHAT,
            RiskLevel.READ_ONLY,
            classification.read_only_matches,
            classification.motion_matches,
        )
    if intent is Intent.OBSERVE:
        risk = (
            RiskLevel.READ_ONLY if is_look_task(text) else RiskLevel.AMBIGUOUS
        )
        return Classification(
            Intent.TASK,
            risk,
            classification.read_only_matches,
            classification.motion_matches,
        )
    return classification


def classify_entry(text: str, *, llm_label: str | None = None) -> dict[str, Any]:
    """JSON-friendly entry report for Feishu, 8888, and Master."""

    task = (text or "").strip()
    seed = classify_task(task)
    classification = (
        refine_with_llm(seed, llm_label, task) if llm_label else seed
    )
    return {
        "task": task,
        "intent": classification.intent.value,
        "risk": classification.risk.value,
        "requires_confirmation": classification.requires_confirmation,
        "needs_llm_route": needs_llm_route(seed) and not llm_label,
    }


def route_ambiguous_with_llm(text: str) -> str:
    """Return 'chat' or 'task' for leftover ambiguous wording."""

    import requests

    api_key = (os.getenv("CLOUD_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("未配置 CLOUD_API_KEY，无法做歧义分流")
    base = (os.getenv("CLOUD_API_BASE") or "https://api.deepseek.com").strip().rstrip("/")
    model = (os.getenv("CLOUD_CHAT_MODEL") or "deepseek-chat").strip()
    endpoint = f"{base}/chat/completions"
    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": ROUTE_SYSTEM},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "max_tokens": 16,
        },
        timeout=15.0,
    )
    if not response.ok:
        raise RuntimeError(f"分流接口返回 HTTP {response.status_code}")
    payload = response.json()
    choices = payload.get("choices") or []
    message = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
    label = str(message).strip()
    if not label:
        raise RuntimeError("分流接口返回空内容")
    return label


def _looks_like_chat(text: str) -> bool:
    if not text:
        return False
    if text.startswith(("请问", "帮我查", "帮我问")):
        return True
    return text.endswith(("吗", "呢", "？", "?"))
