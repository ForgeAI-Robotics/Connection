"""Atomic JSON snapshot and append-only evidence logs for one reception task."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class ReceptionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.images_dir = self.root / "images"
        self.root.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "current_task.json"
        self.events_path = self.root / "events.jsonl"
        self.verification_path = self.root / "verification.jsonl"
        self._lock = threading.RLock()

    def load_state(self):
        with self._lock:
            if not self.state_path.exists():
                return None
            with self.state_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else None

    def save_state(self, state):
        payload = dict(state)
        payload["updated_at"] = now_iso()
        tmp_path = self.state_path.with_suffix(".json.tmp")
        with self._lock:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_path)
        return payload

    def _append(self, path, record):
        entry = {"time": now_iso(), **dict(record)}
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return entry

    def append_event(self, event, **data):
        return self._append(self.events_path, {"event": event, **data})

    def append_verification(self, record):
        return self._append(self.verification_path, record)

    def save_image(self, name, image_bytes):
        safe_name = "".join(
            char for char in str(name) if char.isalnum() or char in {"-", "_", "."}
        )
        if not safe_name:
            raise ValueError("图片文件名不能为空")
        path = self.images_dir / safe_name
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            with tmp_path.open("wb") as handle:
                handle.write(image_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        return str(path)
