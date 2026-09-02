"""Deterministic safety classification for incoming task text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    MOTION = "motion"
    AMBIGUOUS = "ambiguous"


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
)


@dataclass(frozen=True)
class Classification:
    risk: RiskLevel
    read_only_matches: tuple[str, ...] = ()
    motion_matches: tuple[str, ...] = ()

    @property
    def requires_confirmation(self) -> bool:
        return self.risk is not RiskLevel.READ_ONLY


def classify_task(text: str) -> Classification:
    """Classify using the confirmed fail-safe keyword rules.

    Motion always wins. Text that cannot be proven read-only is ambiguous and
    therefore also requires confirmation.
    """

    normalized = (text or "").strip().lower()
    read_matches = tuple(word for word in READ_ONLY_KEYWORDS if word in normalized)
    motion_matches = tuple(word for word in MOTION_KEYWORDS if word in normalized)

    if motion_matches:
        risk = RiskLevel.MOTION
    elif read_matches:
        risk = RiskLevel.READ_ONLY
    else:
        risk = RiskLevel.AMBIGUOUS
    return Classification(risk, read_matches, motion_matches)
