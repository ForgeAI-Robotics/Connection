#!/usr/bin/env python3
"""Read-only local log query/export, with explicit optional peer/attachment imports."""
import argparse
import json
import sys

from brain.observability.diagnostics import query_diagnostics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('task-id', 'command-id', 'request-id', 'date-from', 'date-to'):
        parser.add_argument('--' + name)
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--peer-events', action='append', default=[], metavar='SERVICE=FILE.jsonl',
                        help='显式导入 nav/vla/gateway 的已保存日志（.jsonl 或 .jsonl.N）；不连接远端')
    parser.add_argument('--attachment', action='append', default=[], metavar='MANIFEST.json',
                        help='仅导入 rosbag／证据清单元数据，不读取其引用文件')
    args = parser.parse_args(argv)
    try:
        peers = []
        for value in args.peer_events:
            if '=' not in value:
                raise ValueError('--peer-events 必须为 SERVICE=FILE.jsonl')
            peers.append(tuple(value.split('=', 1)))
        result = query_diagnostics(task_id=args.task_id, command_id=args.command_id,
            request_id=args.request_id, date_from=args.date_from, date_to=args.date_to,
            limit=args.limit, peer_events=peers, attachments=args.attachment)
    except (ValueError, OSError) as exc:
        print('诊断查询失败：' + str(exc), file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
