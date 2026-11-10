#!/usr/bin/env python3
"""Explicit Tianji atomic NAV I/O. Does not register or start a business workflow."""
import argparse
import json
from pathlib import Path

from brain.skills.tianji_navigation import TianjiNavigationSkill


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'submit', 'execute', 'query', 'cancel'))
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--request', help='Previously persisted fq/reception-lan/v1 JSON body')
    parser.add_argument('--token-env', default='TIANJI_NAV_TOKEN')
    parser.add_argument('--request-timeout', type=float, default=10)
    parser.add_argument('--deadline', type=float, default=900)
    parser.add_argument('--poll', type=float, default=.5)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--allow-motion', action='store_true', help='Explicitly permit real goal submission')
    mode.add_argument('--offline', action='store_true', help='Require an isolated nonphysical contract fixture')
    args = parser.parse_args(argv)
    if args.action != 'check' and not args.request:
        parser.error('此操作要求 --request；使用原请求文件保留任务与命令身份')
    try:
        skill = TianjiNavigationSkill(args.base_url, token_env=args.token_env,
            request_timeout_sec=args.request_timeout, allow_motion=args.allow_motion, offline=args.offline)
        if args.action == 'check':
            result = skill.check()
        else:
            body = json.loads(Path(args.request).read_text(encoding='utf-8'))
            if args.action in {'execute', 'query', 'submit'}:
                result = getattr(skill, args.action)(body, deadline_sec=args.deadline, poll_interval_sec=args.poll)
            else:
                result = skill.cancel(body)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if 'verdict' in result:
            return {'PASS': 0, 'FAIL': 1, 'UNKNOWN': 2}[result['verdict']]
        return 0 if result.get('accepted', True) else 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({'error': str(exc), 'success': False}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
