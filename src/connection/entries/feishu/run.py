"""Executable entry point for the Feishu task bridge."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path


from connection.paths import workspace_root
PROJECT_ROOT = Path(os.environ.get("LARK_ENTRY_HOME") or workspace_root())

from connection.clients.brain import BrainClient
from connection.entries.feishu.bridge import FeishuBridge
from connection.entries.feishu.config import Settings, load_settings
from connection.entries.feishu.feishu_transport import (
    ChannelMessenger,
    adapt_card_action,
    adapt_message,
)
from connection.entries.feishu.sdk_compat import enable_websocket_card_callbacks
from connection.entries.feishu.store import TaskStore


LOGGER = logging.getLogger("feishu_bridge")


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.seek(0)
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("已有一个飞书桥接进程正在运行") from exc
        return self

    def __exit__(self, exc_type, exc, traceback):
        if not self.handle:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _configure_logging() -> None:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console_handler])


def _safe_error(error: object) -> str:
    text = str(error)
    return re.sub(
        r"(?i)(access_key|ticket|token|app_secret)=([^&\s]+)", r"\1=<redacted>", text
    )[:1000]


def _preload_lark_sdk() -> None:
    """Import the SDK before asyncio.run() owns the event loop.

    lark-channel-sdk 1.0.0 captures asyncio.get_event_loop() at import time and
    later calls loop.run_until_complete() from a worker thread. Importing it
    from inside asyncio.run() binds that module loop to the running loop and
    fails with "This event loop is already running".
    """

    import lark_channel.ws.client  # noqa: F401
    from lark_channel import FeishuChannel  # noqa: F401


async def _run(settings: Settings) -> None:
    from lark_channel import FeishuChannel
    from lark_channel.channel.config import PolicyConfig, SafetyConfig, TextBatchConfig
    from lark_channel.core.enum import LogLevel

    patched = enable_websocket_card_callbacks()
    if patched:
        LOGGER.warning("已启用 lark-channel-sdk 1.0.0 卡片长连接兼容修复")

    store = TaskStore(settings.db_path)
    deleted = store.cleanup(settings.retention_days)
    if deleted:
        LOGGER.info("已清理 %d 条过期终态记录", deleted)

    channel = FeishuChannel(
        app_id=settings.app_id,
        app_secret=settings.app_secret,
        transport="ws",
        log_level=LogLevel.WARNING,
        policy=PolicyConfig(
            dm_policy="open",
            group_policy="open",
            require_mention=True,
            respond_to_mention_all=False,
        ),
        safety=SafetyConfig(
            text_batch=TextBatchConfig(
                delay_ms=0,
                long_threshold_chars=4000,
                long_delay_ms=0,
                max_messages=1,
                max_chars=4000,
            )
        ),
    )
    bridge = FeishuBridge(
        settings,
        store,
        BrainClient(settings.brain_url, timeout=settings.http_timeout),
        ChannelMessenger(channel),
    )

    async def on_message(message) -> None:
        await bridge.handle_message(adapt_message(message))

    async def on_card_action(event) -> None:
        await bridge.handle_card_action(adapt_card_action(event))

    async def on_error(error) -> None:
        LOGGER.error("飞书通道错误：%s", _safe_error(error))

    channel.on("message", on_message)
    channel.on("cardAction", on_card_action)
    channel.on("error", on_error)

    LOGGER.info(
        "正在连接飞书，任务模式=%s，大脑=%s", settings.task_mode, settings.brain_url
    )
    await channel.start_background(timeout=30)
    LOGGER.info("飞书长连接已就绪")
    await bridge.resume_open_tasks()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await bridge.shutdown()
        await channel.disconnect()


def main() -> int:
    try:
        from common.log_setup import attach_process_log

        attach_process_log("feishu")
        settings = load_settings(require_credentials=True)
        _configure_logging()
        _preload_lark_sdk()
        lock_path = settings.db_path.parent / "feishu.lock"
        with SingleInstanceLock(lock_path):
            asyncio.run(_run(settings))
    except KeyboardInterrupt:
        LOGGER.info("飞书桥接已停止")
        return 0
    except Exception as exc:
        LOGGER.error("飞书桥接启动失败：%s", _safe_error(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
