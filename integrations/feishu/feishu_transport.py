"""Adapters between lark-channel-sdk event models and the bridge core."""

from __future__ import annotations

from typing import Any, Optional

from .bridge import IncomingCardAction, IncomingMessage


class ChannelMessenger:
    def __init__(self, channel) -> None:
        self.channel = channel

    @staticmethod
    def _require_success(result, operation: str) -> str:
        if not getattr(result, "success", False):
            error = getattr(result, "error", None)
            raise RuntimeError(f"飞书{operation}失败：{error or '未知错误'}")
        return str(getattr(result, "message_id", None) or "")

    async def send_text(
        self, chat_id: str, text: str, *, reply_to: Optional[str] = None
    ) -> str:
        options = {"reply_to": reply_to} if reply_to else None
        result = await self.channel.send(chat_id, {"text": text}, options)
        return self._require_success(result, "发送文本")

    async def send_card(
        self, chat_id: str, card: dict[str, Any], *, reply_to: Optional[str] = None
    ) -> str:
        options = {"reply_to": reply_to} if reply_to else None
        result = await self.channel.send(chat_id, {"card": card}, options)
        message_id = self._require_success(result, "发送卡片")
        if not message_id:
            raise RuntimeError("飞书发送卡片成功但未返回 message_id")
        return message_id

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        result = await self.channel.update_card(message_id, card)
        self._require_success(result, "更新卡片")


def adapt_message(message) -> IncomingMessage:
    raw = getattr(message, "raw", None) or {}
    event_id = ""
    if isinstance(raw, dict):
        header = raw.get("header") or {}
        if isinstance(header, dict):
            event_id = str(header.get("event_id") or "")
        event_id = event_id or str(raw.get("event_id") or "")
    return IncomingMessage(
        message_id=str(getattr(message, "message_id", "") or ""),
        event_id=event_id,
        chat_id=str(getattr(message, "chat_id", "") or ""),
        chat_type=str(getattr(message, "chat_type", "unknown") or "unknown"),
        sender_open_id=str(getattr(message, "sender_id", "") or ""),
        text=str(getattr(message, "content_text", "") or ""),
        content_type=str(getattr(message, "raw_content_type", "") or ""),
        mentioned_bot=bool(getattr(message, "mentioned_bot", False)),
        mentioned_all=bool(getattr(message, "mentioned_all", False)),
    )


def adapt_card_action(event) -> IncomingCardAction:
    operator = getattr(event, "operator", None)
    action = getattr(event, "action", None)
    return IncomingCardAction(
        card_message_id=str(getattr(event, "message_id", "") or ""),
        chat_id=str(getattr(event, "chat_id", "") or ""),
        operator_open_id=str(getattr(operator, "open_id", "") or ""),
        value=getattr(action, "value", None),
    )
