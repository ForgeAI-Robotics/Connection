#!/usr/bin/env python3
"""Preview/apply one global profile with optional per-module overrides."""
import argparse
import json
import sys
from pathlib import Path

from ops.execution import Switcher, read_yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'preview', 'apply'])
    parser.add_argument('--file', type=Path)
    parser.add_argument('--mode', choices=['simulation', 'real'])
    parser.add_argument('--sim', choices=['desk', 'mujoco', 'mujoco_3dgs'])
    parser.add_argument('--reset-overrides', action='store_true', help='Clear module overrides for a whole-environment switch')
    for module in ('reception', 'execution', 'observation'):
        parser.add_argument('--' + module, choices=['inherit', 'simulation', 'real', 'disabled'])
    parser.add_argument('--execution-sim', choices=['inherit', 'desk', 'mujoco', 'mujoco_3dgs'])
    parser.add_argument('--observation-sim', choices=['inherit', 'desk', 'mujoco', 'mujoco_3dgs'])
    args = parser.parse_args()
    switcher = Switcher()
    try:
        if args.action == 'status':
            result = switcher.status()
        else:
            config = read_yaml(args.file) if args.file else switcher.status()['draft']
            if args.mode: config['mode'] = args.mode
            if args.sim: config['simulation_backend'] = args.sim
            if args.reset_overrides: config['modules'] = {}
            modules = config.setdefault('modules', {})
            for name in ('reception', 'execution', 'observation'):
                mode = getattr(args, name)
                if mode: modules.setdefault(name, {})['mode'] = mode
                backend = getattr(args, name + '_sim', None)
                if backend: modules.setdefault(name, {})['simulation_backend'] = backend
            result = switcher.apply(config) if args.action == 'apply' else switcher.preview(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
