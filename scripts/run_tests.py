#!/usr/bin/env python3
"""Run every offline test suite from one stable entry point."""

from __future__ import annotations

import os
import subprocess
import tempfile
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SUITES = (
    ("connection", ("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v")),
    ("master", ("-m", "unittest", "discover", "-s", "master/tests", "-p", "test_*.py", "-v")),
    ("web", ("-m", "unittest", "discover", "-s", "web/tests", "-p", "test_*.py", "-v")),
    (
        "feishu",
        (
            "-m",
            "unittest",
            "discover",
            "-s",
            "integrations/feishu/tests",
            "-p",
            "test_*.py",
            "-v",
        ),
    ),
    ("robot_api", ("-m", "unittest", "discover", "-s", "robot_api", "-p", "test_*.py", "-v")),
    ("dream_http", ("-m", "unittest", "serve_dream.test_agent_http", "-v")),
    ("dream_selftest", ("serve_dream/selftest.py",)),
)


def main() -> int:
    failures: list[str] = []
    # Offline tests must not inherit the operator's live environment selection or locks.
    with tempfile.TemporaryDirectory(prefix="connection-tests-") as scratch:
        env = os.environ.copy()
        for key, filename in [('FQ_EXECUTION_STATE', 'execution.json'),
                              ('FQ_EXECUTION_LOCK', 'execution.lock'),
                              ('FQ_EXECUTION_BLOCK', 'execution.blocked')]:
            env[key] = str(Path(scratch) / filename)
        for name, arguments in SUITES:
            print(f"\n{'=' * 72}\nTEST SUITE: {name}\n{'=' * 72}", flush=True)
            result = subprocess.run((sys.executable, *arguments), cwd=ROOT, env=env, check=False)
            if result.returncode:
                failures.append(name)

    if failures:
        print(f"\nFAILED SUITES: {', '.join(failures)}", file=sys.stderr)
        return 1

    print("\nALL OFFLINE TEST SUITES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
