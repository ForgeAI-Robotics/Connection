"""Append-only task ledger. Attempt history is not replaced by a later save."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from connection.contracts.tasks import StaleWrite
from connection.brain.kernel.memory import select_window


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


_FROZEN_ATTEMPT_FIELDS = ("contract", "request", "command_id", "attempt_id", "scene", "delta")


def _merge_attempts(previous, incoming):
    stored = [deepcopy(item) for item in (previous or [])]
    by_id = {item.get("attempt_id"): item for item in stored}
    for attempt in incoming or []:
        attempt_id = attempt.get("attempt_id")
        if attempt_id in by_id:
            current = by_id[attempt_id]
            frozen = {key: deepcopy(current.get(key)) for key in _FROZEN_ATTEMPT_FIELDS}
            current.update(deepcopy(attempt))
            current.update(frozen)
            continue
        copied = deepcopy(attempt)
        stored.append(copied)
        by_id[attempt_id] = copied
    return stored


def _merge_steps(previous, incoming):
    merged = deepcopy(previous or {})
    for step_id, bucket in (incoming or {}).items():
        dest = merged.setdefault(step_id, {"attempts": []})
        for key, value in (bucket or {}).items():
            if key == "attempts":
                continue
            dest[key] = deepcopy(value)
        dest["attempts"] = _merge_attempts(
            dest.get("attempts") or [],
            (bucket or {}).get("attempts") or [],
        )
    return merged


def load_existing(root):
    path = Path(root) / "current_task.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or not value.get('task_id'):
        raise ValueError(f"任务账本内容无效，不允许按空账本覆盖: {path}")
    return value


class _DirectoryGate:
    def __init__(self):
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.fd = None


_GATES = {}
_GATES_GUARD = threading.Lock()


def _gate_for(root: Path) -> _DirectoryGate:
    key = str(root.resolve())
    with _GATES_GUARD:
        gate = _GATES.get(key)
        if gate is None:
            gate = _DirectoryGate()
            _GATES[key] = gate
        return gate


class KernelStore:
    """One ledger directory has one writer across Runtime instances."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.images_dir = self.root / "images"
        self.root.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "current_task.json"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "writer.lock"

    @contextmanager
    def writer(self):
        gate = _gate_for(self.root)
        with gate.thread_lock:
            if gate.depth == 0:
                self.root.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except Exception:
                    os.close(fd)
                    raise
                gate.fd = fd
            gate.depth += 1
            try:
                yield
            finally:
                gate.depth -= 1
                if gate.depth == 0 and gate.fd is not None:
                    fcntl.flock(gate.fd, fcntl.LOCK_UN)
                    os.close(gate.fd)
                    gate.fd = None

    def _read_unlocked(self):
        return load_existing(self.root)

    def load_state(self):
        with self.writer():
            return self._read_unlocked()

    def save_state(self, state):
        incoming = deepcopy(state)
        expected = int(incoming.get("revision") or 0)
        with self.writer():
            previous = self._read_unlocked()
            prev_rev = int(previous.get("revision") or 0) if previous else 0
            if expected != prev_rev:
                raise StaleWrite(f"账本版本已变化: 期望 {expected}，实际 {prev_rev}")
            if previous and previous.get("task_id") == incoming.get("task_id"):
                incoming["steps"] = _merge_steps(
                    previous.get("steps") or {},
                    incoming.get("steps") or {},
                )
            incoming["revision"] = prev_rev + 1
            incoming["updated_at"] = now_iso()
            tmp_path = self.state_path.with_suffix(".json.tmp")
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(incoming, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_path)
        return incoming

    def read_events(self) -> list:
        with self.writer():
            if not self.events_path.is_file():
                return []
            rows = []
            with self.events_path.open(encoding="utf-8") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    rows.append(json.loads(text))
            return rows

    def recent_events(self, *, limit: int, ttl_sec: float, now=None) -> list:
        """Window over the event file. Reading it does not rewrite the file or the ledger."""
        return select_window(self.read_events(), limit=limit, ttl_sec=ttl_sec, now=now)

    def append_event(self, event, **data):
        entry = {"time": now_iso(), "event": event, **data}
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self.writer():
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return entry

    def save_image(self, name, image_bytes: bytes):
        safe_name = "".join(
            char for char in str(name) if char.isalnum() or char in {"-", "_", "."}
        )
        if not safe_name or safe_name != str(name):
            raise ValueError("证据文件名必须只含尝试编号")
        path = self.images_dir / safe_name
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with self.writer():
            with tmp_path.open("wb") as handle:
                handle.write(image_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        return str(path)
