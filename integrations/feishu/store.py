"""SQLite-backed deduplication, audit, and state transitions."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


FINAL_STATES = {
    "invalid",
    "duplicate",
    "brain_offline",
    "brain_busy",
    "preflight_failed",
    "dry_run",
    "canceled",
    "expired",
    "succeeded",
    "failed",
    "superseded",
    "tracking_timeout",
}

OPEN_STATES = {"pending_confirmation", "confirmed", "submitted", "running"}


@dataclass(frozen=True)
class TaskRecord:
    message_id: str
    event_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    task_text: str
    risk_level: str
    state: str
    brain_task_id: Optional[str]
    card_message_id: Optional[str]
    created_at: int
    updated_at: int
    expires_at: Optional[int]
    tracking_timeout_sec: Optional[int]
    confirmed_at: Optional[int]
    completed_at: Optional[int]
    last_error: Optional[str]
    status_signature: Optional[str]
    last_status_json: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRecord":
        return cls(**dict(row))

    @property
    def last_status(self) -> Optional[dict[str, Any]]:
        if not self.last_status_json:
            return None
        try:
            value = json.loads(self.last_status_json)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


class TaskStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    sender_open_id TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    state TEXT NOT NULL,
                    brain_task_id TEXT,
                    card_message_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    tracking_timeout_sec INTEGER,
                    confirmed_at INTEGER,
                    completed_at INTEGER,
                    last_error TEXT,
                    status_signature TEXT,
                    last_status_json TEXT
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "tracking_timeout_sec" not in columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN tracking_timeout_sec INTEGER"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_state ON messages(state)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_sender_state "
                "ON messages(sender_open_id, state)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_brain_task "
                "ON messages(brain_task_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_card "
                "ON messages(card_message_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_per_sender "
                "ON messages(sender_open_id) WHERE state = 'pending_confirmation'"
            )

    def create(
        self,
        *,
        message_id: str,
        event_id: str,
        chat_id: str,
        chat_type: str,
        sender_open_id: str,
        task_text: str,
        risk_level: str,
        state: str = "received",
        expires_at: Optional[int] = None,
        tracking_timeout_sec: Optional[int] = None,
        now: Optional[int] = None,
    ) -> bool:
        timestamp = int(now if now is not None else time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO messages (
                    message_id, event_id, chat_id, chat_type, sender_open_id,
                    task_text, risk_level, state, created_at, updated_at, expires_at,
                    tracking_timeout_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    event_id,
                    chat_id,
                    chat_type,
                    sender_open_id,
                    task_text,
                    risk_level,
                    state,
                    timestamp,
                    timestamp,
                    expires_at,
                    tracking_timeout_sec,
                ),
            )
            return cursor.rowcount == 1

    def get(self, message_id: str) -> Optional[TaskRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return TaskRecord.from_row(row) if row else None

    def get_by_card(self, card_message_id: str) -> Optional[TaskRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE card_message_id = ?", (card_message_id,)
            ).fetchone()
        return TaskRecord.from_row(row) if row else None

    def pending_for_sender(self, sender_open_id: str) -> Optional[TaskRecord]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM messages
                WHERE sender_open_id = ? AND state = 'pending_confirmation'
                ORDER BY created_at DESC LIMIT 1
                """,
                (sender_open_id,),
            ).fetchone()
        return TaskRecord.from_row(row) if row else None

    def list_open(self) -> list[TaskRecord]:
        placeholders = ",".join("?" for _ in OPEN_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM messages WHERE state IN ({placeholders}) ORDER BY created_at",
                tuple(sorted(OPEN_STATES)),
            ).fetchall()
        return [TaskRecord.from_row(row) for row in rows]

    def update(
        self,
        message_id: str,
        *,
        state: Optional[str] = None,
        brain_task_id: Optional[str] = None,
        card_message_id: Optional[str] = None,
        expires_at: Optional[int] = None,
        tracking_timeout_sec: Optional[int] = None,
        confirmed_at: Optional[int] = None,
        completed_at: Optional[int] = None,
        last_error: Optional[str] = None,
        status_signature: Optional[str] = None,
        last_status: Optional[dict[str, Any]] = None,
        now: Optional[int] = None,
    ) -> bool:
        values: dict[str, Any] = {
            "updated_at": int(now if now is not None else time.time())
        }
        optional = {
            "state": state,
            "brain_task_id": brain_task_id,
            "card_message_id": card_message_id,
            "expires_at": expires_at,
            "tracking_timeout_sec": tracking_timeout_sec,
            "confirmed_at": confirmed_at,
            "completed_at": completed_at,
            "last_error": last_error,
            "status_signature": status_signature,
        }
        values.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        if last_status is not None:
            values["last_status_json"] = json.dumps(last_status, ensure_ascii=False)
        assignments = ", ".join(f"{column} = ?" for column in values)
        params = [*values.values(), message_id]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE messages SET {assignments} WHERE message_id = ?", params
            )
            return cursor.rowcount == 1

    def transition(
        self,
        message_id: str,
        *,
        from_states: Iterable[str],
        to_state: str,
        now: Optional[int] = None,
        **fields: Any,
    ) -> bool:
        allowed_fields = {
            "brain_task_id",
            "card_message_id",
            "expires_at",
            "tracking_timeout_sec",
            "confirmed_at",
            "completed_at",
            "last_error",
            "status_signature",
            "last_status_json",
        }
        unknown = set(fields) - allowed_fields
        if unknown:
            raise ValueError(f"unsupported transition fields: {sorted(unknown)}")
        states = tuple(from_states)
        if not states:
            raise ValueError("from_states cannot be empty")
        timestamp = int(now if now is not None else time.time())
        values = {"state": to_state, "updated_at": timestamp, **fields}
        if to_state in FINAL_STATES and "completed_at" not in values:
            values["completed_at"] = timestamp
        assignments = ", ".join(f"{column} = ?" for column in values)
        placeholders = ",".join("?" for _ in states)
        params = [*values.values(), message_id, *states]
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    f"UPDATE messages SET {assignments} "
                    f"WHERE message_id = ? AND state IN ({placeholders})",
                    params,
                )
                return cursor.rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def cleanup(self, retention_days: int, *, now: Optional[int] = None) -> int:
        cutoff = int(now if now is not None else time.time()) - retention_days * 86400
        states = tuple(sorted(FINAL_STATES))
        placeholders = ",".join("?" for _ in states)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM messages WHERE state IN ({placeholders}) "
                "AND COALESCE(completed_at, updated_at) < ?",
                (*states, cutoff),
            )
            return cursor.rowcount
