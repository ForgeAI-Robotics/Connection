"""Start Redis with a daily per-start logfile under log/YYYY-MM-DD/redis/."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.log_setup import create_process_log_path


def _redis_binaries():
    extracted = ROOT / ".runtime" / "redis-root" / "usr"
    return {
        "server": extracted / "bin" / "redis-server",
        "cli": extracted / "bin" / "redis-cli",
        "lib": extracted / "lib" / "x86_64-linux-gnu",
    }


def redis_env():
    bins = _redis_binaries()
    env = os.environ.copy()
    lib = str(bins["lib"])
    env["LD_LIBRARY_PATH"] = lib + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def redis_cli_bin() -> Path:
    return _redis_binaries()["cli"]


def build_redis_command(
    *,
    daemonize: bool,
    log_path: Path | None = None,
    log_to_stdout: bool = False,
) -> list[str]:
    """Build redis-server argv. Foreground mode keeps a tmux pane alive."""

    bins = _redis_binaries()
    server = bins["server"]
    if not server.exists():
        raise FileNotFoundError(
            "redis-server not found. Extract packages into .runtime/redis-root "
            "or install redis-server."
        )
    data_dir = Path(os.environ.get("FQPLANNER_REDIS_DIR", "/tmp/connection-redis"))
    data_dir.mkdir(parents=True, exist_ok=True)
    path = log_path or create_process_log_path("redis")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Empty --logfile means stdout, so a tmux pane can show the same lines.
    return [
        str(server),
        "--port",
        "6379",
        "--bind",
        "127.0.0.1",
        "--protected-mode",
        "yes",
        "--daemonize",
        "yes" if daemonize else "no",
        "--dir",
        str(data_dir),
        "--dbfilename",
        "dump.rdb",
        "--save",
        "",
        "--logfile",
        "" if log_to_stdout else str(path),
        "--loglevel",
        "notice",
    ]


def main():
    env = redis_env()
    log_path = create_process_log_path("redis")
    cmd = build_redis_command(daemonize=True, log_path=log_path)
    subprocess.check_call(cmd, env=env)
    ping = subprocess.check_output(
        [str(redis_cli_bin()), "-h", "127.0.0.1", "-p", "6379", "ping"],
        env=env,
        text=True,
    ).strip()
    print(f"[log] redis -> {log_path}")
    print(ping)


if __name__ == "__main__":
    main()
