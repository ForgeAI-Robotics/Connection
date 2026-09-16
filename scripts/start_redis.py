"""Start Redis with a daily per-start logfile under log/YYYY-MM-DD/redis/."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from log_setup import create_process_log_path


def _redis_binaries():
    extracted = ROOT / ".runtime" / "redis-root" / "usr"
    return {
        "server": extracted / "bin" / "redis-server",
        "cli": extracted / "bin" / "redis-cli",
        "lib": extracted / "lib" / "x86_64-linux-gnu",
    }


def main():
    bins = _redis_binaries()
    server = bins["server"]
    if not server.exists():
        raise SystemExit(
            "redis-server not found. Extract packages into .runtime/redis-root "
            "or install redis-server."
        )
    log_path = create_process_log_path("redis")
    data_dir = Path(os.environ.get("FQPLANNER_REDIS_DIR", "/tmp/connection-redis"))
    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    lib = str(bins["lib"])
    env["LD_LIBRARY_PATH"] = lib + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    cmd = [
        str(server),
        "--port", "6379",
        "--bind", "127.0.0.1",
        "--protected-mode", "yes",
        "--daemonize", "yes",
        "--dir", str(data_dir),
        "--dbfilename", "dump.rdb",
        "--save", "",
        "--logfile", str(log_path),
    ]
    subprocess.check_call(cmd, env=env)
    cli = bins["cli"]
    ping = subprocess.check_output(
        [str(cli), "-h", "127.0.0.1", "-p", "6379", "ping"], env=env, text=True
    ).strip()
    print(f"[log] redis -> {log_path}")
    print(ping)


if __name__ == "__main__":
    main()
