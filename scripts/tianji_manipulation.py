#!/usr/bin/env python3
"""Call a Tianji VLA atomic command or read the legacy completion gateway."""
import argparse
import json
from pathlib import Path

from brain.skills.tianji_manipulation import TianjiManipulationSkill


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'legacy-check', 'observe-event', 'head-frame', 'submit', 'execute', 'query', 'cancel'))
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--request', help='Original saved canonical command JSON')
    parser.add_argument('--task-id')
    parser.add_argument('--run-id')
    parser.add_argument('--arm', choices=('left', 'right'))
    parser.add_argument('--receipt-ttl', type=float, default=5)
    parser.add_argument('--token-env', default='TIANJI_VLA_TOKEN')
    parser.add_argument('--request-timeout', type=float, default=10)
    parser.add_argument('--deadline', type=float, default=300)
    parser.add_argument('--poll', type=float, default=1)
    parser.add_argument('--output', help='New JPEG path for head-frame; adjacent .json stores source metadata')
    parser.add_argument('--captured-after', help='ISO8601 source-frame boundary, such as verified NAV completion time')
    parser.add_argument('--previous-frame-id', help='Reject the same source frame on a repeated observation')
    parser.add_argument('--robot-serial', help='Expected Tianji robot identity')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--allow-motion', action='store_true')
    modes.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'observe-event' and not all((args.task_id, args.run_id, args.arm)):
        parser.error('observe-event 需要 --task-id --run-id --arm')
    if args.action in {'submit', 'execute', 'query', 'cancel'} and not args.request:
        parser.error('操作需要 --request 原请求文件；未知结果不改 ID 重发')
    if args.action == 'head-frame' and (not args.output or Path(args.output).suffix.lower() not in {'.jpg', '.jpeg'}):
        parser.error('head-frame 需要 --output 新的 .jpg/.jpeg 文件')
    try:
        skill = TianjiManipulationSkill(args.base_url, token_env=args.token_env,
            request_timeout_sec=args.request_timeout, allow_motion=args.allow_motion, offline=args.offline)
        if args.action == 'head-frame':
            path = Path(args.output)
            metadata_path = path.with_suffix('.json')
            if path.exists() or metadata_path.exists():
                raise ValueError('取图输出或来源记录已存在；保留原证据并使用新文件名')
            metadata, raw = skill.capture_head(captured_after=args.captured_after,
                previous_frame_id=args.previous_frame_id, expected_robot_serial=args.robot_serial)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as handle:
                handle.write(raw)
            with metadata_path.open('x', encoding='utf-8') as handle:
                json.dump(metadata, handle, ensure_ascii=False, indent=2)
            result = dict(image_path=str(path.resolve()), metadata_path=str(metadata_path.resolve()),
                          camera_frame=metadata, observation_only=True,
                          navigation_allowed=False, workflow_registered=False)
        elif args.action == 'observe-event':
            result = skill.observe_event(args.task_id, args.run_id, args.arm, receipt_ttl_sec=args.receipt_ttl)
        elif args.action in {'check', 'legacy-check'}:
            result = skill.check() if args.action == 'check' else skill.legacy_check()
        else:
            body = json.loads(Path(args.request).read_text(encoding='utf-8'))
            result = skill.cancel(body) if args.action == 'cancel' else getattr(skill, args.action)(
                body, deadline_sec=args.deadline, poll_interval_sec=args.poll)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if 'verdict' in result:
            return {'PASS': 0, 'FAIL': 1, 'UNKNOWN': 2}[result['verdict']]
        if 'event_received' in result:
            return 0 if result['event_received'] else 2
        return 0 if result.get('accepted', True) else 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps(dict(success=False, error=str(exc)), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
