#!/usr/bin/env python3
"""
Move the differential base forward for one second.

Example:
    python3 scripts/move_forward_1s.py --port /dev/ttyACM0 --speed 0.1
"""

import argparse
import sys
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
REALBASE_DIR = PROJECT_DIR / "RealBase"
sys.path.insert(0, str(REALBASE_DIR))

from motor_controller import OmniWheelController  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move the base forward for one second.")
    parser.add_argument(
        "--port",
        default="/dev/ttyACM0",
        help="Serial port connected to the Feetech bus. Default: /dev/ttyACM0",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=0.2,
        help="Forward speed in m/s. Default: 0.1",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=3.0,
        help="Move duration in seconds. Default: 1.0",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    controller = OmniWheelController(port=args.port)

    if args.duration <= 0:
        print("Duration must be greater than 0.")
        return 1

    if args.speed <= 0:
        print("Speed must be greater than 0.")
        return 1

    try:
        if not controller.connect():
            return 1

        print(f"Moving forward: speed={args.speed} m/s, duration={args.duration} s")
        if not controller.set_velocity_raw(vx=args.speed, vy=0.0, omega=0.0):
            return 1

        time.sleep(args.duration)
        return 0
    finally:
        if controller.base_bus is not None:
            controller.stop()
            controller.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
