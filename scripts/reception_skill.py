#!/usr/bin/env python3
"""Validate and preview a reception definition offline. Never creates a task."""
import argparse
import json

from brain.packages.reception_skill import load_skill
from contracts.steps import step_spec_dict
from contracts.tasks import Rejected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="YAML definition; defaults to the archived single-can v1")
    parser.add_argument("--config", help="Preview the skill selected by a brain YAML config")
    parser.add_argument("--nav-only", action="store_true", help="Preview its navigation-only variant")
    args = parser.parse_args()
    if args.file and args.config:
        parser.error("--file 和 --config 不能同时使用")
    try:
        from shared.config import load_config
        skill = load_skill(load_config(args.config) if args.config else None, path=args.file)
    except Rejected as exc:
        parser.exit(2, f"Skill 校验失败：{exc}\n")
    print(json.dumps({"sop": skill.sop, "sha256": skill.snapshot()["sha256"],
                      "parameters": skill.definition["parameters"], "nav_only": args.nav_only,
                      "steps": [step_spec_dict(step) for step in skill.phases(nav_only=args.nav_only)]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
