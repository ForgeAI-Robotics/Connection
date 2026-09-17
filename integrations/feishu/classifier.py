"""Deterministic safety and intent classification for incoming Feishu text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
    """Overlay LLM intent onto an ambiguous leftover. Never overrides motion.

    Observe is not a Feishu bucket; it becomes a read-only company look task
    when the wording is actually about the scene.
    """
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


def _looks_like_chat(text: str) -> bool:
    if not text:
        return False
    if text.startswith(("请问", "帮我查", "帮我问")):
        return True
    return text.endswith(("吗", "呢", "？", "?"))
