#!/usr/bin/env python3
"""Manage only our pre-created container; refuse to interrupt unresolved commands."""
import fcntl
import subprocess
import sys
from pathlib import Path

if __package__:
    from .service.store import Journal
    from .service.reception_store import ReceptionStore
else:
    from service.store import Journal
    from service.reception_store import ReceptionStore


def manage(action, root=None, run=subprocess.run):
    if action not in {"start", "stop"}:
        raise ValueError("unsupported action")
    root = Path(root or Path(__file__).resolve().parent) / "runtime"
    journal = Journal(root)
    with (root / "management.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "stop":
            journal.close_admission()
            try:
                ReceptionStore(root).close_admission()
            except Exception:
                (root / "admission.closed").unlink(missing_ok=True)
                raise
        run(["docker", action, "connection-simple-o7"], check=True, timeout=30)
        if action == "start":
            (root / "admission.closed").unlink(missing_ok=True)
            (root / "reception.closed").unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        manage(sys.argv[1])
    except Exception as exc:
        raise SystemExit(str(exc))
