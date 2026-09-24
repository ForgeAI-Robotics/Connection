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
    ("unit_and_integration", ("-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_*.py", "-v")),
    ("dream_selftest", ("-m", "tests.dream.selftest")),
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
