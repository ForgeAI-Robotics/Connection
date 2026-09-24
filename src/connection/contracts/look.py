"""Detect on-site look/describe tasks shared by Feishu, Master and Slaver."""

from __future__ import annotations


MOTION_HINTS = (
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

SCENE_NOUNS = (
    "桌上",
    "桌子",
    "桌面",
    "台面",
    "台子",
    "前面",
    "面前",
    "眼前",
    "视野",
    "镜头",
    "相机",
    "场景",
    "现场",
    "柜台上",
    "周围",
    "环境",
)

SCENE_ASKS = (
    "有什么",
    "有啥",
    "有哪些",
    "是什么",
    "看到",
    "看见",
    "识别",
    "哪几",
    "哪些东西",
    "什么东西",
    "什么物体",
    "哪些物体",
)

LOOK_VERBS = (
    "看一下",
    "看看",
    "看下",
    "瞧一眼",
    "拍一下",
    "拍张",
    "拍照",
    "截图",
    "观察",
)

FRONT_WORDS = ("前面", "面前", "眼前", "视野", "镜头")
DESK_WORDS = ("桌上", "桌子", "桌面", "台面", "台子")


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def has_motion(text: str) -> bool:
    return any(word in _text(text) for word in MOTION_HINTS)


def is_look_task(text) -> bool:
    """True when the user only wants a scene description, not motion."""
    normalized = _text(text)
    if not normalized or has_motion(normalized):
        return False
    nouns = any(word in normalized for word in SCENE_NOUNS)
    asks = any(word in normalized for word in SCENE_ASKS)
    if nouns and asks:
        return True
    verbs = any(word in normalized for word in LOOK_VERBS)
    return bool(verbs and nouns)


def preferred_camera(text) -> str:
    normalized = _text(text)
    if any(word in normalized for word in FRONT_WORDS):
        return "head_cam"
    if any(word in normalized for word in DESK_WORDS):
        return "overhead_cam"
    return "overhead_cam"


def cameras_for(question: str) -> list[str]:
    preferred = preferred_camera(question)
    extras = ["head_cam", "robot0_frontview", "overhead_cam"]
    ordered = [preferred]
    for name in extras:
        if name not in ordered:
            ordered.append(name)
    return ordered
