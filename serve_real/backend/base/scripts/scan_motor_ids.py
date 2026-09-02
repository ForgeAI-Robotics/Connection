#!/usr/bin/env python3
"""
Scan Feetech motor IDs on a serial bus.

Example:
    python3 scripts/scan_motor_ids.py --port /dev/ttyACM0
"""

import argparse
import os
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
REALBASE_DIR = PROJECT_DIR / "RealBase"
sys.path.insert(0, str(REALBASE_DIR))

from motors.feetech.feetech import FeetechMotorsBus  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan Feetech motor IDs.")
    parser.add_argument(
        "--port",
        default="/dev/ttyACM0",
        help="Serial port connected to the Feetech bus. Default: /dev/ttyACM0",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print(f"Scanning Feetech motors on {args.port}...")
    try:
        baudrate_ids = FeetechMotorsBus.scan_port(args.port)
    except Exception as exc:
        print(f"Scan failed: {exc}")
        if exc.__cause__ is not None:
            print(f"Cause: {type(exc.__cause__).__name__}: {exc.__cause__}")

        port_path = Path(args.port)
        if port_path.exists():
            stat = port_path.stat()
            print(
                "Port exists: "
                f"mode={oct(stat.st_mode & 0o777)}, "
                f"uid={stat.st_uid}, gid={stat.st_gid}, "
                f"user_groups={os.getgroups()}"
            )
        else:
            print(f"Port does not exist: {args.port}")
        return 1

    if not baudrate_ids:
        print("No motors found.")
        return 1

    print("\nFound motors:")
    for baudrate, ids in baudrate_ids.items():
        ids_text = ", ".join(str(id_) for id_ in ids)
        print(f"  baudrate={baudrate}: ids=[{ids_text}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
