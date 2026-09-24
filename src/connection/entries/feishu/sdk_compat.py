"""Narrow compatibility fixes for pinned Feishu Channel SDK versions."""

from __future__ import annotations

import base64
import http
import inspect
import time


def enable_websocket_card_callbacks() -> bool:
    """Patch lark-channel-sdk 1.0.0 to dispatch CARD frames.

    The official 1.0.0 WebSocket client returns immediately for CARD frames,
    even though FeishuChannel registers ``card.action.trigger``. The fix is the
    one-line behavior expected by the dispatcher: EVENT and CARD frames share
    the same dispatch-and-response path. It is applied only when that exact
    unfixed source pattern is present, so a future fixed SDK is left untouched.
    """

    from lark_channel.ws import client as ws

    try:
        source = inspect.getsource(ws.Client._handle_data_frame)
    except (OSError, TypeError):
        source = ""
    unfixed = (
        "elif message_type == MessageType.CARD:" in source
        and "elif message_type == MessageType.CARD:\n                return" in source
    )
    if not unfixed:
        return False

    async def _handle_data_frame(self, frame):
        headers = frame.headers
        message_id = ws._get_by_key(headers, ws.HEADER_MESSAGE_ID)
        trace_id = ws._get_by_key(headers, ws.HEADER_TRACE_ID)
        packet_count = ws._get_by_key(headers, ws.HEADER_SUM)
        sequence = ws._get_by_key(headers, ws.HEADER_SEQ)
        message_type_raw = ws._get_by_key(headers, ws.HEADER_TYPE)

        payload = frame.payload
        if int(packet_count) > 1:
            payload = self._combine(
                message_id, int(packet_count), int(sequence), payload
            )
            if payload is None:
                return

        message_type = ws.MessageType(message_type_raw)
        ws.logger.debug(
            self._fmt_log(
                "receive message, message_type: {}, message_id: {}, trace_id: {}, payload_len: {}",
                message_type.value,
                message_id,
                trace_id,
                len(payload),
            )
        )
        response = ws.Response(code=http.HTTPStatus.OK)
        try:
            started = int(round(time.time() * 1000))
            if message_type not in (ws.MessageType.EVENT, ws.MessageType.CARD):
                return
            result = self._event_handler._do_without_validation(payload)
            finished = int(round(time.time() * 1000))
            header = headers.add()
            header.key = ws.HEADER_BIZ_RT
            header.value = str(finished - started)
            if result is not None:
                response.data = base64.b64encode(
                    ws.JSON.marshal(result).encode(ws.UTF_8)
                )
        except Exception as exc:
            ws.logger.error(
                self._fmt_log(
                    "handle message failed, message_type: {}, message_id: {}, trace_id: {}, err: {}",
                    message_type.value,
                    message_id,
                    trace_id,
                    exc,
                )
            )
            response = ws.Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = ws.JSON.marshal(response).encode(ws.UTF_8)
        await self._write_message(frame.SerializeToString())

    ws.Client._handle_data_frame = _handle_data_frame
    return True
