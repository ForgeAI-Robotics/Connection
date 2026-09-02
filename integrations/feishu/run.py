"""Executable entry point for the Feishu task bridge."""

# ruff: noqa: E402 -- direct script execution needs PROJECT_ROOT on sys.path first.

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from integrations.feishu.brain_client import BrainClient
from integrations.feishu.bridge import FeishuBridge
from integrations.feishu.config import Settings, load_settings
from integrations.feishu.feishu_transport import (
    ChannelMessenger,
    adapt_card_action,
    adapt_message,
)
from integrations.feishu.sdk_compat import enable_websocket_card_callbacks
from integrations.feishu.store import TaskStore


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


def _configure_logging(settings: Settings) -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


def _safe_error(error: object) -> str:
    text = str(error)
    return re.sub(
        r"(?i)(access_key|ticket|token|app_secret)=([^&\s]+)", r"\1=<redacted>", text
    )[:1000]


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
        settings = load_settings(require_credentials=True)
        _configure_logging(settings)
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
