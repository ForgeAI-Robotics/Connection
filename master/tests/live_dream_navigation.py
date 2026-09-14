#!/usr/bin/env python3
"""Agent-host live DREAM contract/navigation test.

This file is deployed to the Agent repository.  Its default mode is read-only.
Real navigation requires an explicit command-line acknowledgement and still
goes through DREAM 8001, 9882 approval, Gateway, Token and collision gates.
It never calls VLA and never connects to the SONIC action port directly.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
import uuid


MASTER_ROOT = Path(__file__).resolve().parents[1]
if str(MASTER_ROOT) not in sys.path:
    sys.path.insert(0, str(MASTER_ROOT))

from integrations.dream_client import DreamClient  # noqa: E402
from sop.reception_real import (  # noqa: E402
    NAVIGATION_LEGS,
    ReceptionRealRunner,
)


CONTRACT_VERSION = "fq/reception-lan/v1"
REAL_ACK = "AGENT_DREAM_REAL_MOTION_AUTHORIZED"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_event(path: Path, event: str, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"recorded_at": now_iso(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def validate_world(client: DreamClient) -> dict:
    health = client.health()
    status = client.status()
    world = client.world()
    graph = client.relation_graph(world)
    if health.get("contract_version") != CONTRACT_VERSION:
        raise RuntimeError("DREAM /health contract_version mismatch")
    if status.get("world_state_available") is not True:
        raise RuntimeError("DREAM world_state_available is not true")
    if status.get("active_command_id"):
        raise RuntimeError(
            f"DREAM already has active command: {status.get('active_command_id')}"
        )
    validator = object.__new__(ReceptionRealRunner)
    validator.contract_version = CONTRACT_VERSION
    contract = validator._validate_graph_contract(graph)
    return {
        "health": health,
        "status": status,
        "world": world,
        "contract": contract,
    }


def navigation_payload(task_id: str, command_id: str, key: str) -> dict:
    leg = NAVIGATION_LEGS[key]
    return {
        "contract_version": CONTRACT_VERSION,
        "command_id": command_id,
        "task_id": task_id,
        "target_id": leg["target_id"],
        "route_phase": leg["route_phase"],
        "leg_index": leg["leg_index"],
        "frame_id": "map",
        "goal_xyt": list(leg["goal_xyt"]),
        "motion_mode": leg["motion_mode"],
        "require_final_orientation": True,
    }


def run(args: argparse.Namespace) -> int:
    client = DreamClient(
        args.base_url,
        contract_version=CONTRACT_VERSION,
        request_timeout_sec=args.request_timeout_sec,
    )
    checked = validate_world(client)
    leg_indexes = [
        item["leg_index"]
        for item in checked["contract"]["agent_navigation_contract"].values()
    ]
    print(
        "AGENT_REAL_DREAM_READONLY_PASS",
        checked["health"]["contract_version"],
        leg_indexes,
        flush=True,
    )
    if args.mode == "readonly":
        return 0
    if args.real_motion_ack != REAL_ACK:
        raise SystemExit(
            f"real navigation requires --real-motion-ack {REAL_ACK}"
        )

    task_id = args.task_id or (
        "agent-dream-live-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    run_id = uuid.uuid4().hex[:8]
    output_dir = args.output_dir.expanduser().resolve() / task_id
    state_path = output_dir / "state.json"
    events_path = output_dir / "events.jsonl"
    keys = ["table2"] if args.mode == "table2" else [
        "table2", "relay2", "relay3", "table1"
    ]
    state = {
        "schema_version": "agent/dream-live-navigation-test/v1",
        "contract_version": CONTRACT_VERSION,
        "task_id": task_id,
        "run_id": run_id,
        "mode": args.mode,
        "state": "RUNNING",
        "current_leg": None,
        "commands": {},
        "started_at": now_iso(),
    }
    atomic_json(state_path, state)
    append_event(events_path, "TEST_STARTED", task_id=task_id, mode=args.mode)

    try:
        for key in keys:
            leg = NAVIGATION_LEGS[key]
            command_id = f"nav-{key}-{run_id}"
            payload = navigation_payload(task_id, command_id, key)
            state["current_leg"] = key
            state["commands"][key] = {
                "command_id": command_id,
                "payload": copy.deepcopy(payload),
                "prepared_at": now_iso(),
            }
            atomic_json(state_path, state)
            append_event(
                events_path,
                "COMMAND_PREPARED",
                key=key,
                command_id=command_id,
                payload=payload,
            )

            transport = client.wait_navigation_transport(
                timeout_sec=args.transport_timeout_sec,
                poll_interval_sec=args.poll_interval_sec,
            )
            append_event(
                events_path,
                "TRANSPORT_READY",
                key=key,
                command_id=command_id,
                status=transport,
            )
            accepted = client.submit_navigation(payload)
            append_event(
                events_path,
                "COMMAND_ACCEPTED",
                key=key,
                command_id=command_id,
                response=accepted,
            )
            timeout_sec = (
                args.lateral_timeout_sec
                if leg["motion_mode"] == "lateral_path_aligned"
                else args.navigation_timeout_sec
            )
            terminal = client.wait_command(
                command_id,
                timeout_sec=timeout_sec,
                poll_interval_sec=args.poll_interval_sec,
                on_update=lambda value, current_key=key: append_event(
                    events_path,
                    "COMMAND_UPDATE",
                    key=current_key,
                    command_id=command_id,
                    response=value,
                ),
            )
            client.require_success(terminal, command_id)
            state["commands"][key]["terminal"] = copy.deepcopy(terminal)
            state["commands"][key]["completed_at"] = now_iso()
            atomic_json(state_path, state)
            append_event(
                events_path,
                "COMMAND_SUCCEEDED",
                key=key,
                command_id=command_id,
                response=terminal,
            )
            print(f"{key}: succeeded ({command_id})", flush=True)

        state.update(
            state="SUCCEEDED",
            current_leg=None,
            completed_at=now_iso(),
        )
        atomic_json(state_path, state)
        append_event(events_path, "TEST_SUCCEEDED", task_id=task_id)
        print(f"AGENT_DREAM_LIVE_NAVIGATION_PASS output={output_dir}", flush=True)
        return 0
    except BaseException as exc:
        state.update(
            state="RECOVERY_REQUIRED",
            failure_reason=str(exc),
            failed_at=now_iso(),
        )
        atomic_json(state_path, state)
        append_event(
            events_path,
            "RECOVERY_REQUIRED",
            task_id=task_id,
            current_leg=state.get("current_leg"),
            error=str(exc),
        )
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DREAM_BASE_URL", "http://192.168.0.185:8001"),
    )
    parser.add_argument(
        "--mode", choices=("readonly", "table2", "four-leg"), default="readonly"
    )
    parser.add_argument("--real-motion-ack", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=MASTER_ROOT / "sop" / "runtime" / "agent_dream_live_test",
    )
    parser.add_argument("--request-timeout-sec", type=float, default=10.0)
    parser.add_argument("--transport-timeout-sec", type=float, default=30.0)
    parser.add_argument("--poll-interval-sec", type=float, default=0.5)
    parser.add_argument("--navigation-timeout-sec", type=float, default=900.0)
    parser.add_argument("--lateral-timeout-sec", type=float, default=300.0)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
