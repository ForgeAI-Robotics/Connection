"""Configuration loading for the Feishu bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _path_from_env(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default).strip())
    return value if value.is_absolute() else PROJECT_ROOT / value


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    task_mode: str
    brain_url: str
    confirm_timeout: int
    task_timeout: int
    poll_interval: float
    db_path: Path
    log_path: Path
    retention_days: int
    http_timeout: float = 8.0

    @property
    def active(self) -> bool:
        return self.task_mode == "active"

    def validate_credentials(self) -> None:
        missing = []
        if not self.app_id:
            missing.append("LARK_APP_ID")
        if not self.app_secret:
            missing.append("LARK_APP_SECRET")
        if missing:
            raise ValueError(f"缺少飞书凭证：{', '.join(missing)}")


def load_settings(*, require_credentials: bool = True) -> Settings:
    """Load the project-root ``.env`` and validate bridge settings."""

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    mode = os.getenv("LARK_TASK_MODE", "dry_run").strip().lower()
    if mode not in {"dry_run", "active"}:
        raise ValueError("LARK_TASK_MODE 只能是 dry_run 或 active")

    brain_url = os.getenv("LARK_BRAIN_URL", "http://127.0.0.1:8888").strip().rstrip("/")
    if not brain_url.startswith(("http://", "https://")):
        raise ValueError("LARK_BRAIN_URL 必须以 http:// 或 https:// 开头")

    settings = Settings(
        app_id=os.getenv("LARK_APP_ID", "").strip(),
        app_secret=os.getenv("LARK_APP_SECRET", "").strip(),
        task_mode=mode,
        brain_url=brain_url,
        confirm_timeout=_positive_int("LARK_CONFIRM_TIMEOUT", 120),
        task_timeout=_positive_int("LARK_TASK_TIMEOUT", 1800),
        poll_interval=_positive_float("LARK_POLL_INTERVAL", 2.0),
        db_path=_path_from_env("LARK_DB_PATH", "integrations/feishu/runtime/feishu.db"),
        log_path=_path_from_env(
            "LARK_LOG_PATH", "integrations/feishu/runtime/feishu.log"
        ),
        retention_days=_positive_int("LARK_RETENTION_DAYS", 30),
        http_timeout=_positive_float("LARK_HTTP_TIMEOUT", 8.0),
    )
    if require_credentials:
        settings.validate_credentials()
    return settings
