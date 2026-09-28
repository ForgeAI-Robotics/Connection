#!/usr/bin/env python3
"""Run the existing brain against SIMPLE using an isolated ledger and process-local profile."""
import argparse
import json
import os
from pathlib import Path
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["health", "run", "query", "resume", "cancel"])
    parser.add_argument("--url", default="http://192.168.5.21:18770")
    parser.add_argument("--ledger", type=Path, help="Experiment ledger, separate from the live brain")
    parser.add_argument("--task", default="抓起桌上的罐子并稳定持有")
    parser.add_argument("--deadline", type=float, default=900)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    from dotenv import load_dotenv
    load_dotenv(root / ".env")
    from brain.adapters.simple_o7 import SimpleO7Adapter
    port = SimpleO7Adapter(args.url, deadline=args.deadline)
    if args.action == "health":
        print(json.dumps({"health": port._call("/health"), "scene": port._call("/v1/scene")}, ensure_ascii=False, indent=2))
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
        from shared.execution_profile import resolve
        profile = resolve({"simulation_backend": "simple_o7", "modules": {
            "reception": {"mode": "disabled"}, "observation": {"mode": "disabled"}}},
            {"backends": {"simple_o7": {"url": args.url}}})
        profile["revision"] = "experiment-" + uuid.uuid4().hex
        state.write_text(json.dumps(profile, indent=2))
    profile = json.loads(state.read_text())
    if profile["routes"]["execution"]["url"] != args.url.rstrip("/"):
        parser.error("Use the experiment's original URL to query its original command")
    for key, name in [("FQ_EXECUTION_STATE", "execution.json"), ("FQ_EXECUTION_LOCK", "execution.lock"),
                      ("FQ_EXECUTION_BLOCK", "execution.blocked")]:
        os.environ[key] = str(ledger / name)
    from shared.execution_profile import applied_profile
    applied_profile.cache_clear()
    config.setdefault("reception_real", {}).update(kernel_runtime_dir=str(ledger), kernel_enabled=False)
    from brain.app import create_service
    service = create_service(config, port_factory=lambda backend: port)
    if args.action == "run":
        service.publish(args.task, uuid.uuid4().hex, options={"entry": "simple_o7_experiment", "reflect": False})
    elif args.action == "resume":
        service.publish("", None, resume=True)
    elif args.action == "cancel":
        print(json.dumps(service.control("cancel"), ensure_ascii=False, indent=2))
    status = service.status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    from brain.storage.tasks import load_existing
    record = load_existing(ledger) or {}
    command = record.get("open_command_id")
    if args.action == "query" and command:
        runtime = service.attach()
        print(json.dumps(port.query(command, runtime._request_of(command)), ensure_ascii=False, indent=2))
    return 0 if args.action != "run" or status.get("state") == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
