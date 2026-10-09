#!/usr/bin/env python3
"""Run 「开始接待」 through the existing brain against the SIMPLE physical reception episode.

Uses an isolated ledger and a process-local reception_simple profile, so the live
brain service, its ledger, the applied environment and the real-motion permit are untouched.
"""
import argparse
import json
import os
from pathlib import Path
import time
import uuid

STEP_SEGMENTS = {"NAVIGATING_TO_TABLE2": "nav_table2", "VLA_PICKING": "pick", "NAVIGATING_TO_RELAY2": "nav_relay2",
                 "LATERAL_TO_RELAY3": "nav_relay3", "NAVIGATING_TO_TABLE1": "nav_table1", "VLA_PLACING": "place"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["health", "run", "status", "continue", "cancel"])
    parser.add_argument("--url", default="http://192.168.31.69:18770")
    parser.add_argument("--ledger", type=Path, help="Experiment ledger, separate from the live brain")
    parser.add_argument("--task", default="开始接待")
    parser.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    from dotenv import load_dotenv
    load_dotenv(root / ".env")
    from shared.networks import apply_proxy_bypass
    apply_proxy_bypass()
    from urllib.parse import urlsplit
    host = urlsplit(args.url).hostname
    for key in ("NO_PROXY", "no_proxy"):
        if host not in os.environ.get(key, "").split(","):
            os.environ[key] = ",".join(filter(None, [os.environ.get(key, ""), host]))
    from shared.execution_profile import resolve
    profile = resolve({"mode": "simulation", "modules": {
        "reception": {"mode": "simulation", "simulation_backend": "reception_simple"},
        "execution": {"mode": "disabled"}, "observation": {"mode": "disabled"}}},
        {"backends": {"simple_o7": {"url": args.url}}})
    if args.action == "health":
        from shared.protocol_simulation import require_simulator_pair
        instance = require_simulator_pair(profile["routes"]["reception"]["endpoints"], kind="simple_physics")
        print(json.dumps({"endpoints": profile["routes"]["reception"]["endpoints"], "instance_id": instance}, indent=2))
        return 0
    if args.ledger is None:
        parser.error("--ledger is required; this command never uses data/tasks")
    ledger = args.ledger.resolve()
    from shared.config import load_config
    config = load_config(root / "config/brain.yaml")
    from brain.service_support import runtime_dir
    live_ledger = Path(runtime_dir(config)).resolve()
    if ledger == live_ledger or live_ledger in ledger.parents or ledger in live_ledger.parents:
        parser.error("Experiment ledger must be separate from the live task ledger")
    ledger.mkdir(parents=True, exist_ok=True)
    state = ledger / "execution.json"
    if not state.exists():
        profile["revision"] = "simple-reception-" + uuid.uuid4().hex
        state.write_text(json.dumps(profile, indent=2))
    if json.loads(state.read_text())["routes"]["reception"]["endpoints"] != profile["routes"]["reception"]["endpoints"]:
        parser.error("Use the experiment's original URL to query its original commands")
    for key, name in [("FQ_EXECUTION_STATE", "execution.json"), ("FQ_EXECUTION_LOCK", "execution.lock"),
                      ("FQ_EXECUTION_BLOCK", "execution.blocked")]:
        os.environ[key] = str(ledger / name)
    from shared.execution_profile import applied_profile
    applied_profile.cache_clear()
    # Simulation never passes the real-motion gate; the live brain.yaml permit is not read or changed.
    config.setdefault("reception_real", {}).update(kernel_runtime_dir=str(ledger), kernel_enabled=False)
    from brain.app import create_service
    service = create_service(config)
    if args.action in {"run", "continue"}:
        if args.action == "run":
            task_id = "simple-reception-" + time.strftime("%Y%m%d-%H%M%S")
            service.publish(args.task, task_id, options={"entry": "simple_reception_experiment"})
        else:
            # Same entry as the web 「继续」 button: re-check the original command, then a new attempt.
            print(json.dumps(service.control("continue"), ensure_ascii=False), flush=True)
        deadline, last = time.monotonic() + args.timeout, None
        while time.monotonic() < deadline:
            status = service.status()
            line = (status.get("state"), status.get("current_subtask") or status.get("current_step"),
                    status.get("completed"), status.get("total"))
            if line != last:
                print(time.strftime("%H:%M:%S"), *line, flush=True)
                last = line
            if status.get("state") in {"succeeded", "failed", "recovery_required", "cancelled"}:
                break
            time.sleep(1)
    elif args.action == "cancel":
        print(json.dumps(service.control("cancel"), ensure_ascii=False, indent=2))
    status = service.status()
    from brain.storage.tasks import load_existing
    record = load_existing(ledger) or {}
    commands = {}
    for step, segment in STEP_SEGMENTS.items():
        attempts = ((record.get("steps") or {}).get(step) or {}).get("attempts") or []
        if attempts:
            verify = "VERIFYING_GRASP" if segment == "pick" else "VERIFYING_PLACE" if segment == "place" else None
            done = (record["steps"].get(verify) or {}).get("status") == "done" if verify else \
                (record["steps"].get(step) or {}).get("status") == "done"
            commands[segment] = {"command_id": attempts[-1]["command_id"], "step": step,
                                 "verdict": "大脑核验 PASS" if done else None}
    (ledger / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2))
    (ledger / "commands.json").write_text(json.dumps(commands, ensure_ascii=False, indent=2))
    print(json.dumps({k: status.get(k) for k in ("task_id", "state", "completed", "total", "execution_backend")},
                     ensure_ascii=False))
    return 0 if args.action not in {"run", "continue"} or status.get("state") == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
