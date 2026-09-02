"""Feishu CardKit 2.0 payload builders."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .store import TaskRecord


STATE_LABELS = {
    "received": "已接收",
    "pending_confirmation": "等待确认",
    "confirmed": "已确认",
    "submitted": "已提交",
    "running": "执行中",
    "succeeded": "已完成",
    "failed": "执行失败",
    "canceled": "已取消",
    "expired": "确认已过期",
    "brain_offline": "大脑离线，未提交",
    "brain_busy": "大脑忙碌，未提交",
    "preflight_failed": "下游预检未通过，未提交",
    "dry_run": "演练模式，未提交",
    "superseded": "任务已被其他入口替换",
    "tracking_timeout": "飞书跟踪超时",
    "invalid": "无效任务",
}

STATE_COLORS = {
    "pending_confirmation": "orange",
    "submitted": "blue",
    "running": "blue",
    "succeeded": "green",
    "failed": "red",
    "canceled": "grey",
    "expired": "grey",
    "brain_offline": "red",
    "brain_busy": "orange",
    "preflight_failed": "orange",
    "dry_run": "turquoise",
    "superseded": "orange",
    "tracking_timeout": "orange",
}


def _plain(content: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {"tag": "plain_text", "content": str(content)[:3000]},
    }


def _markdown(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": content[:6000]}


def _format_time(timestamp: Optional[int]) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _base_card(
    title: str, state: str, elements: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": STATE_COLORS.get(state, "blue"),
        },
        "body": {"elements": elements},
    }


def confirmation_card(record: TaskRecord, timeout_seconds: int) -> dict[str, Any]:
    risk = "运动任务" if record.risk_level == "motion" else "无法证明为只读"
    elements = [
        _plain(f"任务：{record.task_text}"),
        _plain(f"任务编号：{record.message_id}"),
        _plain(f"风险类型：{risk}"),
        _plain(f"只有原发送者可操作；{timeout_seconds} 秒内有效。"),
        {
            "tag": "column_set",
            "columns": [
                {
                    "tag": "column",
                    "elements": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "确认执行"},
                            "type": "primary",
                            "value": {
                                "action": "confirm",
                                "message_id": record.message_id,
                            },
                            "confirm": {
                                "title": {"tag": "plain_text", "content": "确认执行"},
                                "text": {
                                    "tag": "plain_text",
                                    "content": "确认将该任务提交给大脑？",
                                },
                            },
                        }
                    ],
                },
                {
                    "tag": "column",
                    "elements": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "取消"},
                            "type": "default",
                            "value": {
                                "action": "cancel",
                                "message_id": record.message_id,
                            },
                        }
                    ],
                },
            ],
        },
    ]
    return _base_card("机器人任务待确认", "pending_confirmation", elements)


def task_card(
    record: TaskRecord, status: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    status = status or record.last_status or {}
    state_label = STATE_LABELS.get(record.state, record.state)
    elements: list[dict[str, Any]] = [
        _plain(f"任务：{record.task_text}"),
        _plain(f"任务 ID：{record.brain_task_id or '-'}"),
        _plain(f"状态：{state_label}"),
    ]

    reasoning = str(status.get("reasoning") or "").strip()
    if reasoning:
        elements.append(_plain(f"Master 规划：{reasoning}"))

    total = int(status.get("total") or 0)
    completed = int(status.get("completed") or 0)
    if total:
        elements.append(_plain(f"进度：{completed} / {total}"))

    reception = status.get("reception_state") or {}
    if isinstance(reception, dict) and reception:
        pipeline_state = str(reception.get("state") or "-")
        runtime_phase = str(reception.get("runtime_phase") or "-")
        verified_state = str(reception.get("verified_state") or "-")
        elements.append(
            _plain(
                f"接待链路：{pipeline_state} / {runtime_phase}\n"
                f"验证状态：{verified_state}"
            )
        )
        holding = reception.get("holding")
        object_location = reception.get("object_location")
        if holding is not None or object_location:
            elements.append(
                _plain(
                    f"持有物：{holding if holding is not None else '无'}；"
                    f"目标位置：{object_location or '-'}"
                )
            )
        commands = reception.get("commands") or {}
        remote_states = reception.get("remote_states") or {}
        command_lines = []
        if isinstance(commands, dict):
            for key, item in list(commands.items())[:10]:
                if not isinstance(item, dict):
                    continue
                command_id = str(item.get("command_id") or "")
                remote = (
                    remote_states.get(command_id)
                    if isinstance(remote_states, dict)
                    else None
                )
                remote_state = remote.get("state") if isinstance(remote, dict) else None
                command_lines.append(
                    f"• {key}: {command_id or '-'} ({remote_state or 'prepared'})"
                )
        if command_lines:
            elements.append(_plain("下游命令：\n" + "\n".join(command_lines)))
        failure_reason = str(reception.get("failure_reason") or "").strip()
        if failure_reason:
            failed_phase = str(reception.get("failed_phase") or runtime_phase)
            elements.append(_plain(f"失败阶段：{failed_phase}\n原因：{failure_reason}"))

    subtasks = status.get("subtask_list") or []
    if isinstance(subtasks, list) and subtasks:
        lines = []
        for item in subtasks[:20]:
            if not isinstance(item, dict):
                continue
            done = bool(item.get("done"))
            sub_status = str(item.get("status") or "")
            icon = (
                "✅"
                if done and sub_status not in {"failure", "exception", "timeout"}
                else "❌"
                if done
                else "⏳"
            )
            text = str(item.get("subtask") or "未命名子任务").replace("\n", " ")
            lines.append(f"{icon} {text[:200]}")
        if lines:
            elements.append(_markdown("**子任务**\n" + "\n".join(lines)))

    if record.last_error:
        elements.append(_plain(f"说明：{record.last_error}"))
    elements.extend(
        [
            _plain(f"开始时间：{_format_time(record.created_at)}"),
            _plain(f"最后更新：{_format_time(record.updated_at)}"),
        ]
    )
    if record.state == "tracking_timeout":
        elements.append(_plain("仅停止飞书侧跟踪，不代表机器人任务已取消。"))
    return _base_card("机器人任务", record.state, elements)
