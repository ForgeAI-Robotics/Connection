"""Durable command identities. This is an executor journal, not a brain task ledger."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

VERSION = "connection/simple-o7/v1"
TERMINAL = {"succeeded", "failed", "cancelled"}


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class Journal:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "commands.sqlite3"
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, task TEXT UNIQUE NOT NULL, record TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, command_id):
        with self.transaction() as db:
            row = db.execute("SELECT record FROM commands WHERE id=?", (command_id,)).fetchone()
            if not row:
                raise KeyError(command_id)
            return json.loads(row[0])

    def create(self, request):
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute("SELECT record FROM commands WHERE id=?", (request["command_id"],)).fetchone()
            if row:
                record = json.loads(row[0])
                if record["request_hash"] != digest:
                    raise Conflict("command_id_payload_conflict")
                return record, False
            if (self.root / "admission.closed").exists():
                raise Conflict("executor_stopping")
            rows = db.execute("SELECT record FROM commands").fetchall()
            for row in rows:
                old = json.loads(row[0])
                if old["task_id"] == request["task_id"]:
                    raise Conflict("episode_already_used: a task cannot reset its scene for another physical attempt")
                if old["state"] not in TERMINAL:
                    raise Conflict("executor_busy_or_original_command_unresolved")
            record = dict(request, request_hash=digest, state="queued", stage="queued", started=False,
                          stopped=False, resources_released=False, cancel_requested=False,
                          created_at=now(), updated_at=now(), episode_id=hashlib.sha256(
                              request["command_id"].encode()).hexdigest()[:24], result={}, observation={}, error="")
            db.execute("INSERT INTO commands VALUES (?, ?, ?)", (
                record["command_id"], record["task_id"], json.dumps(record)))
            return record, True

    def records(self):
        with self.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT record FROM commands").fetchall()]

    def close_admission(self):
        with self.transaction() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT record FROM commands").fetchall()]
            if any(r["state"] not in TERMINAL for r in records):
                raise Conflict("原命令尚未结束，拒绝停止仿真服务；请先在任务入口取消并核清")
            (self.root / "admission.closed").touch()

    def update(self, command_id, **changes):
        with self.transaction() as db:
            row = db.execute("SELECT record FROM commands WHERE id=?", (command_id,)).fetchone()
            if not row:
                raise KeyError(command_id)
            record = json.loads(row[0])
            # Completed evidence cannot be overwritten by late cancellation requests.
            if record["state"] in TERMINAL:
                return record
            if record["cancel_requested"] and changes.get("state") in TERMINAL:
                changes["state"] = "cancelled"
            record.update(changes, updated_at=now())
            db.execute("UPDATE commands SET record=? WHERE id=?", (json.dumps(record), command_id))
            return record

    def cancel(self, command_id):
        with self.transaction() as db:
            row = db.execute("SELECT record FROM commands WHERE id=?", (command_id,)).fetchone()
            if not row:
                raise KeyError(command_id)
            record = json.loads(row[0])
            if record["state"] not in TERMINAL:
                record.update(cancel_requested=True, state="cancelling", updated_at=now())
                if record.get("stopped") is True and record.get("resources_released") is True:
                    record["state"] = "cancelled"
                db.execute("UPDATE commands SET record=? WHERE id=?", (json.dumps(record), command_id))
            return record


def public(record):
    return {key: value for key, value in record.items() if key not in {"request_hash", "worker_pid"}}
