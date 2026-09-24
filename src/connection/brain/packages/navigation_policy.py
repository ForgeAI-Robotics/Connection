"""Safety-critical post-processing for reviewed DREAM multi-leg routes."""

from __future__ import annotations

import copy
import re


TABLE2_ALIASES = ("table2", "table_2", "桌子2", "二号桌", "2号桌")
TABLE1_ALIASES = ("table1", "table_1", "桌子1", "一号桌", "1号桌")
MANIPULATION_MARKERS = (
    "抓", "拿", "取", "放", "搬", "pick", "grasp", "place", "vla",
)


def _norm(value):
    return re.sub(r"[\s_\-（）()，,。]+", "", str(value or "").lower())


def _first_alias_index(text, aliases):
    positions = [text.find(_norm(alias)) for alias in aliases]
    valid = [position for position in positions if position >= 0]
    return min(valid) if valid else -1


def requires_table2_to_table1_route(task):
    text = _norm(" ".join(map(str, task)) if isinstance(task, list) else task)
    if any(_norm(marker) in text for marker in MANIPULATION_MARKERS):
        return False
    table2_index = _first_alias_index(text, TABLE2_ALIASES)
    table1_index = _first_alias_index(text, TABLE1_ALIASES)
    return table2_index >= 0 and table1_index > table2_index


def enforce_dream_route_plan(task, plan):
    """Expand table2→table1 navigation into the reviewed four-leg safety route."""
    if not isinstance(plan, dict) or not requires_table2_to_table1_route(task):
        return plan
    result = copy.deepcopy(plan)
    result["subtask_list"] = [
        {
            "robot_name": "FQrobot",
            "subtask": "导航到table2（2号桌）",
            "subtask_order": 1,
        },
        {
            "robot_name": "FQrobot",
            "subtask": "导航到relay2门外接近点（door_approach）",
            "subtask_order": 2,
        },
        {
            "robot_name": "FQrobot",
            "subtask": "横移到relay3进门点（door_lateral_exit）",
            "subtask_order": 3,
        },
        {
            "robot_name": "FQrobot",
            "subtask": "导航到table1（1号桌）",
            "subtask_order": 4,
        },
    ]
    original = str(result.get("reasoning_explanation") or "").strip()
    safety_reason = (
        "DREAM安全路由策略已补齐门段：table2到table1不得直达，"
        "必须依次经过relay2门外接近和relay3横移进门；四段复用同一task_id，"
        "任一前置段失败立即停止。"
    )
    result["reasoning_explanation"] = (
        f"{original} {safety_reason}".strip() if original else safety_reason
    )
    result["dream_route_policy_applied"] = "table2_to_relay2_to_relay3_to_table1"
    return result
