"""Business workflow for bridging Feishu messages to the brain API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .brain_client import (
    BrainBusy,
    BrainClient,
    BrainOffline,
    BrainRejected,
    BrainStatus,
    BrainTaskIdMismatch,
)
from .cards import STATE_LABELS, confirmation_card, task_card
from .classifier import classify_task
from .config import Settings
from .store import TaskRecord, TaskStore


LOGGER = logging.getLogger(__name__)
TASK_COMMAND = re.compile(r"(?:^|\s)/task(?:\s+|$)", re.IGNORECASE)


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    event_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    text: str
    content_type: str = "text"
    mentioned_bot: bool = False
    mentioned_all: bool = False


@dataclass(frozen=True)
class IncomingCardAction:
    card_message_id: str
    chat_id: str
    operator_open_id: str
    value: Any


class Messenger(Protocol):
    async def send_text(
        self, chat_id: str, text: str, *, reply_to: Optional[str] = None
    ) -> str: ...

    async def send_card(
        self, chat_id: str, card: dict[str, Any], *, reply_to: Optional[str] = None
    ) -> str: ...

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None: ...


class FeishuBridge:
    def __init__(
        self,
        settings: Settings,
        store: TaskStore,
        brain: BrainClient,
        messenger: Messenger,
    ) -> None:
        self.settings = settings
        self.store = store
        self.brain = brain
        self.messenger = messenger
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error:
                LOGGER.error("飞书桥接后台任务失败：%s", error, exc_info=error)

        task.add_done_callback(finished)

    async def shutdown(self) -> None:
        """Cancel and drain background expiry/tracking tasks."""

        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def handle_message(self, message: IncomingMessage) -> None:
        parsed = self._parse_message(message)
        if parsed is None:
            return
        kind, task_text = parsed
        if kind == "help":
            await self.messenger.send_text(
                message.chat_id, self._help_text(), reply_to=message.message_id
            )
            return
        if kind == "status":
            await self._send_brain_status(message)
            return
        if kind == "unsupported":
            await self.messenger.send_text(
                message.chat_id,
                "第一版仅支持纯文本任务。",
                reply_to=message.message_id,
            )
            return
        if kind == "invalid" or not task_text:
            await self.messenger.send_text(
                message.chat_id,
                "任务内容不能为空。私聊可直接发送任务，群聊请使用 @机器人 /task <任务>。",
                reply_to=message.message_id,
            )
            return

        classification = classify_task(task_text)
        created = self.store.create(
            message_id=message.message_id,
            event_id=message.event_id or message.message_id,
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            sender_open_id=message.sender_open_id,
            task_text=task_text,
            risk_level=classification.risk.value,
        )
        if not created:
            LOGGER.info("忽略重复飞书消息 message_id=%s", message.message_id)
            return

        if classification.requires_confirmation:
            await self._request_confirmation(message)
        else:
            await self._submit(message.message_id, reply_to=message.message_id)

    def _parse_message(self, message: IncomingMessage) -> Optional[tuple[str, str]]:
        text = (message.text or "").strip()
        is_group = message.chat_type in {"group", "topic"}

        if is_group:
            if message.mentioned_all or not message.mentioned_bot:
                return None
            match = TASK_COMMAND.search(text)
            if not match:
                if text.lower().strip() in {"/help", "/status"}:
                    return text.lower().strip()[1:], ""
                return None
            task_text = text[match.end() :].strip()
        else:
            task_text = text
            if task_text.lower() == "/help":
                return "help", ""
            if task_text.lower() == "/status":
                return "status", ""
            command = re.match(r"^/task(?:\s+|$)", task_text, re.IGNORECASE)
            if command:
                task_text = task_text[command.end() :].strip()

        if message.content_type and message.content_type != "text":
            return "unsupported", ""
        return ("task", task_text) if task_text else ("invalid", "")

    async def _request_confirmation(self, message: IncomingMessage) -> None:
        existing = self.store.pending_for_sender(message.sender_open_id)
        if existing and existing.message_id != message.message_id:
            self.store.transition(
                message.message_id,
                from_states={"received"},
                to_state="invalid",
                last_error=f"已有待确认任务：{existing.message_id}",
            )
            await self.messenger.send_text(
                message.chat_id,
                "你已有一个待确认任务，请先确认、取消或等待其过期。",
                reply_to=message.message_id,
            )
            return

        now = int(time.time())
        expires_at = now + self.settings.confirm_timeout
        transitioned = self.store.transition(
            message.message_id,
            from_states={"received"},
            to_state="pending_confirmation",
            expires_at=expires_at,
        )
        if not transitioned:
            self.store.transition(
                message.message_id,
                from_states={"received"},
                to_state="invalid",
                last_error="同一发送者已有待确认任务",
            )
            await self.messenger.send_text(
                message.chat_id,
                "你已有一个待确认任务，请先处理后再发送。",
                reply_to=message.message_id,
            )
            return

        record = self.store.get(message.message_id)
        if not record:
            return
        try:
            card_id = await self.messenger.send_card(
                message.chat_id,
                confirmation_card(record, self.settings.confirm_timeout),
                reply_to=message.message_id,
            )
        except Exception as exc:
            LOGGER.exception("发送飞书确认卡片失败 message_id=%s", message.message_id)
            self.store.transition(
                message.message_id,
                from_states={"pending_confirmation"},
                to_state="invalid",
                last_error=f"确认卡片发送失败，任务未提交：{exc}"[:1000],
            )
            await self.messenger.send_text(
                message.chat_id,
                "确认卡片发送失败，任务未提交，请稍后重新发送。",
                reply_to=message.message_id,
            )
            return
        self.store.update(message.message_id, card_message_id=card_id)
        self._spawn(self._expire_after(message.message_id))

    async def handle_card_action(self, action: IncomingCardAction) -> None:
        value = action.value
        if not isinstance(value, dict):
            return
        operation = str(value.get("action") or "").lower()
        message_id = str(value.get("message_id") or "")
        record = self.store.get(message_id) if message_id else None
        if record is None and action.card_message_id:
            record = self.store.get_by_card(action.card_message_id)
        if record is None or operation not in {"confirm", "cancel"}:
            await self.messenger.send_text(
                action.chat_id, "该确认任务不存在或已被清理。"
            )
            return
        if action.operator_open_id != record.sender_open_id:
            await self.messenger.send_text(
                action.chat_id, "只有原任务发送者可以确认或取消。"
            )
            return

        now = int(time.time())
        if record.expires_at and now >= record.expires_at:
            changed = self.store.transition(
                record.message_id,
                from_states={"pending_confirmation"},
                to_state="expired",
                now=now,
            )
            if changed:
                await self._update_record_card(record.message_id)
            else:
                await self.messenger.send_text(action.chat_id, "该任务已不再等待确认。")
            return

        if operation == "cancel":
            changed = self.store.transition(
                record.message_id,
                from_states={"pending_confirmation"},
                to_state="canceled",
                now=now,
            )
            if changed:
                await self._update_record_card(record.message_id)
            else:
                await self.messenger.send_text(
                    action.chat_id, "该任务已处理，请勿重复操作。"
                )
            return

        changed = self.store.transition(
            record.message_id,
            from_states={"pending_confirmation"},
            to_state="confirmed",
            confirmed_at=now,
            now=now,
        )
        if not changed:
            await self.messenger.send_text(
                action.chat_id, "该任务已处理，请勿重复确认。"
            )
            return
        await self._update_record_card(record.message_id)
        await self._submit(record.message_id)

    async def _submit(self, message_id: str, *, reply_to: Optional[str] = None) -> None:
        record = self.store.get(message_id)
        if record is None or record.state not in {"received", "confirmed"}:
            return
        brain_task_id = self._brain_task_id(record.message_id)
        self.store.update(message_id, brain_task_id=brain_task_id)

        # This guard lives at the final submission boundary so even a pending
        # confirmation recovered after a mode change can never bypass dry_run.
        if not self.settings.active:
            self.store.transition(
                message_id,
                from_states={"received", "confirmed"},
                to_state="dry_run",
                brain_task_id=brain_task_id,
            )
            record = self.store.get(message_id)
            if record:
                await self._ensure_task_card(record, reply_to=reply_to)
            return

        try:
            current = await self.brain.get_status()
        except BrainOffline as exc:
            await self._finish_unsubmitted(record, "brain_offline", str(exc), reply_to)
            return
        except BrainRejected as exc:
            await self._finish_unsubmitted(record, "brain_offline", str(exc), reply_to)
            return

        if current.busy:
            task_name = str(current.raw.get("task") or "当前任务")
            completed = int(current.raw.get("completed") or 0)
            total = int(current.raw.get("total") or 0)
            detail = f"大脑忙碌：{task_name}（{completed}/{total}）"
            await self._finish_unsubmitted(record, "brain_busy", detail, reply_to)
            return

        if record.risk_level != "read_only":
            try:
                preflight = await self.brain.task_preflight(record.task_text)
            except BrainOffline as exc:
                await self._finish_unsubmitted(
                    record, "brain_offline", f"任务预检失败：{exc}", reply_to
                )
                return
            except BrainRejected as exc:
                await self._finish_unsubmitted(
                    record, "preflight_failed", f"任务预检异常：{exc}", reply_to
                )
                return
            if preflight.recommended_tracking_timeout_sec:
                self.store.update(
                    message_id,
                    tracking_timeout_sec=preflight.recommended_tracking_timeout_sec,
                )
            if preflight.required and not preflight.ready:
                detail = "；".join(preflight.blockers) or "下游服务未就绪"
                await self._finish_unsubmitted(
                    record, "preflight_failed", detail, reply_to
                )
                return

        try:
            await self.brain.publish_task(record.task_text, brain_task_id)
        except BrainBusy as exc:
            await self._finish_unsubmitted(record, "brain_busy", str(exc), reply_to)
            return
        except BrainTaskIdMismatch as exc:
            await self._finish_unsubmitted(record, "superseded", str(exc), reply_to)
            return
        except BrainOffline as exc:
            await self._finish_unsubmitted(record, "brain_offline", str(exc), reply_to)
            return
        except BrainRejected as exc:
            await self._finish_unsubmitted(record, "failed", str(exc), reply_to)
            return

        self.store.transition(
            message_id,
            from_states={"received", "confirmed"},
            to_state="submitted",
            brain_task_id=brain_task_id,
        )
        record = self.store.get(message_id)
        if record:
            await self._ensure_task_card(record, reply_to=reply_to)
            self._spawn(self._track(message_id))

    async def _finish_unsubmitted(
        self,
        record: TaskRecord,
        state: str,
        error: str,
        reply_to: Optional[str],
    ) -> None:
        self.store.transition(
            record.message_id,
            from_states={"received", "confirmed"},
            to_state=state,
            last_error=error[:1000],
        )
        updated = self.store.get(record.message_id)
        if updated:
            await self._ensure_task_card(updated, reply_to=reply_to)

    async def _track(self, message_id: str) -> None:
        record = self.store.get(message_id)
        if not record or not record.brain_task_id:
            return
        tracking_timeout = max(
            self.settings.task_timeout,
            int(record.tracking_timeout_sec or 0),
        )
        deadline = record.created_at + tracking_timeout
        seen_expected = False

        while time.time() < deadline:
            latest = self.store.get(message_id)
            if not latest or latest.state not in {"confirmed", "submitted", "running"}:
                return
            try:
                status = await self.brain.get_status()
            except (BrainOffline, BrainRejected) as exc:
                self.store.update(
                    message_id, last_error=f"状态轮询暂时失败：{exc}"[:1000]
                )
                await asyncio.sleep(self.settings.poll_interval)
                continue

            raw = status.raw
            if status.active and status.task_id == latest.brain_task_id:
                seen_expected = True
                state = (
                    "failed"
                    if status.all_done and raw.get("failed")
                    else "succeeded"
                    if status.all_done
                    else "running"
                )
                signature = self._status_signature(raw)
                changed = signature != latest.status_signature or state != latest.state
                if state in {"succeeded", "failed"}:
                    self.store.transition(
                        message_id,
                        from_states={"confirmed", "submitted", "running"},
                        to_state=state,
                        status_signature=signature,
                        last_status_json=json.dumps(raw, ensure_ascii=False),
                        last_error="",
                    )
                else:
                    self.store.update(
                        message_id,
                        state=state,
                        status_signature=signature,
                        last_status=raw,
                        last_error="",
                    )
                if changed:
                    await self._update_record_card(message_id)
                if state in {"succeeded", "failed"}:
                    return
            elif status.active and status.task_id and (status.busy or seen_expected):
                changed = self.store.transition(
                    message_id,
                    from_states={"confirmed", "submitted", "running"},
                    to_state="superseded",
                    last_error=f"当前 task_id 已变为 {status.task_id}"[:1000],
                )
                if changed:
                    await self._update_record_card(message_id)
                return

            await asyncio.sleep(self.settings.poll_interval)

        changed = self.store.transition(
            message_id,
            from_states={"confirmed", "submitted", "running"},
            to_state="tracking_timeout",
            last_error=f"飞书侧跟踪超过 {tracking_timeout} 秒",
        )
        if changed:
            await self._update_record_card(message_id)

    async def _expire_after(self, message_id: str) -> None:
        record = self.store.get(message_id)
        if not record or not record.expires_at:
            return
        await asyncio.sleep(max(0, record.expires_at - time.time()))
        changed = self.store.transition(
            message_id,
            from_states={"pending_confirmation"},
            to_state="expired",
        )
        if changed:
            await self._update_record_card(message_id)

    async def resume_open_tasks(self) -> None:
        """Resume expiry/tracking after a bridge restart without resubmitting."""

        for record in self.store.list_open():
            if record.state == "pending_confirmation":
                if record.expires_at and record.expires_at <= int(time.time()):
                    self.store.transition(
                        record.message_id,
                        from_states={"pending_confirmation"},
                        to_state="expired",
                    )
                    await self._update_record_card(record.message_id)
                else:
                    self._spawn(self._expire_after(record.message_id))
            elif record.brain_task_id:
                self._spawn(self._track(record.message_id))
            else:
                self.store.transition(
                    record.message_id,
                    from_states={record.state},
                    to_state="tracking_timeout",
                    last_error="桥接重启后无法确认任务是否已提交；为避免重复执行，不自动重试",
                )
                await self._update_record_card(record.message_id)

    async def _ensure_task_card(
        self, record: TaskRecord, *, reply_to: Optional[str] = None
    ) -> None:
        if record.card_message_id:
            await self._safe_update_card(record)
            return
        try:
            card_id = await self.messenger.send_card(
                record.chat_id, task_card(record), reply_to=reply_to
            )
        except Exception:
            LOGGER.exception("发送飞书任务卡片失败 message_id=%s", record.message_id)
            await self.messenger.send_text(
                record.chat_id,
                f"任务“{record.task_text}”：{STATE_LABELS.get(record.state, record.state)}",
                reply_to=reply_to,
            )
            return
        self.store.update(record.message_id, card_message_id=card_id)

    async def _update_record_card(self, message_id: str) -> None:
        record = self.store.get(message_id)
        if record:
            await self._safe_update_card(record)

    async def _safe_update_card(self, record: TaskRecord) -> None:
        if not record.card_message_id:
            await self._ensure_task_card(record)
            return
        try:
            await self.messenger.update_card(record.card_message_id, task_card(record))
        except Exception:
            LOGGER.exception("更新飞书任务卡片失败 message_id=%s", record.message_id)
            await self.messenger.send_text(
                record.chat_id,
                f"任务“{record.task_text}”：{STATE_LABELS.get(record.state, record.state)}",
            )

    async def _send_brain_status(self, message: IncomingMessage) -> None:
        try:
            status = await self.brain.get_status()
        except BrainOffline:
            text = "大脑离线。"
        except BrainRejected as exc:
            text = f"无法读取大脑状态：{exc}"
        else:
            text = self._status_text(status)
        await self.messenger.send_text(
            message.chat_id, text, reply_to=message.message_id
        )

    @staticmethod
    def _status_text(status: BrainStatus) -> str:
        if not status.active:
            return "大脑在线，当前没有任务。"
        raw = status.raw
        task = str(raw.get("task") or "未命名任务")
        completed = int(raw.get("completed") or 0)
        total = int(raw.get("total") or 0)
        state = "已完成" if status.all_done else "执行中"
        if raw.get("failed"):
            state = "失败"
        return f"当前任务：{task}\n状态：{state}\n进度：{completed}/{total}"

    @staticmethod
    def _status_signature(status: dict[str, Any]) -> str:
        watched = {
            "reasoning": status.get("reasoning"),
            "completed": status.get("completed"),
            "total": status.get("total"),
            "failed": status.get("failed"),
            "all_done": status.get("all_done"),
            "subtask_list": status.get("subtask_list"),
            "reception_state": FeishuBridge._reception_status_signature(
                status.get("reception_state")
            ),
        }
        return hashlib.sha256(
            json.dumps(watched, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _brain_task_id(message_id: str) -> str:
        digest = hashlib.sha256(f"feishu:{message_id}".encode("utf-8")).hexdigest()
        # Match the project's established 32-character, ASCII task IDs while
        # retaining a visible source prefix. The SQLite row keeps the original
        # Feishu message_id for full traceability.
        return f"feishu{digest[:26]}"

    @staticmethod
    def _reception_status_signature(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        commands = value.get("commands") or {}
        command_ids = {
            str(key): str(item.get("command_id") or "")
            for key, item in commands.items()
            if isinstance(item, dict)
        }
        remote = value.get("remote_states") or {}
        remote_states = {
            str(key): {
                "service": item.get("service"),
                "state": item.get("state"),
            }
            for key, item in remote.items()
            if isinstance(item, dict)
        }
        return {
            "state": value.get("state"),
            "runtime_phase": value.get("runtime_phase"),
            "verified_state": value.get("verified_state"),
            "holding": value.get("holding"),
            "object_location": value.get("object_location"),
            "failed_phase": value.get("failed_phase"),
            "failure_reason": value.get("failure_reason"),
            "evidence_level": value.get("evidence_level"),
            "commands": command_ids,
            "remote_states": remote_states,
        }

    @staticmethod
    def _help_text() -> str:
        return (
            "飞书任务桥接用法：\n"
            "• 私聊：直接发送自然语言任务，或 /task <任务>\n"
            "• 群聊：@机器人 /task <任务>\n"
            "• /status：查看当前大脑任务\n"
            "• /help：查看帮助\n"
            "查询任务会直接提交；运动或模糊任务需要原发送者确认。"
        )
