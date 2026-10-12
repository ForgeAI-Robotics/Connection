#!/usr/bin/env python3
"""Offline audit of recorded real NAV calls; never contacts any service.

Denominator: distinct CALL/start command identities in explicitly real tasks.
This is not a per-HTTP-request counter: unchanged polls are not journaled.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import statistics


def stamp(value):
    return datetime.fromisoformat(value)


def elapsed(first, last):
    return round((stamp(last['time']) - stamp(first['time'])).total_seconds() * 1000, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--days', nargs='+', default=['2026-10-08', '2026-10-09'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = []
    sources = []
    backends = defaultdict(set)
    for day in args.days:
        path = args.root / 'logs' / day / 'master/brain.jsonl'
        content = path.read_bytes()
        sources.append({'path': str(path.relative_to(args.root)),
                        'sha256': hashlib.sha256(content).hexdigest()})
        for line_no, line in enumerate(content.decode().splitlines(), 1):
            record = json.loads(line)
            record['_source'] = str(path.relative_to(args.root))
            record['_line'] = line_no
            rows.append(record)
            if record.get('backend') not in (None, '', 'unselected'):
                backends[record.get('task_id')].add(record['backend'])
    calls = defaultdict(list)
    for record in rows:
        if (record.get('kind') == 'CALL' and record.get('peer') == 'dream'
                and record.get('op') == 'navigate'):
            calls[(record.get('task_id'), record.get('cmd'))].append(record)
    included, excluded = [], []
    for (task, command), records in calls.items():
        starts = [r for r in records if r['event'] == 'start']
        if not starts:
            continue
        if backends[task] != {'reception_real'}:
            excluded.append({'task_id': task, 'command_id': command,
                             'backends': sorted(backends[task]), 'starts': len(starts)})
            continue
        if len(starts) != 1:
            raise ValueError(f'Non-unique start identity: {task} / {command}')
        start = starts[0]
        after = [r for r in records if stamp(r['time']) >= stamp(start['time'])]
        accepted = next((r for r in after if r['event'] == 'accepted'), None)
        ends = [r for r in after if r['event'] == 'end']
        item = {'date': start['time'][:10], 'task_id': task, 'command_id': command,
                'source': start['_source'], 'start_line': start['_line'],
                'start_time': start['time']}
        if accepted:
            item.update(submission='accepted', response_line=accepted['_line'],
                        response_ms=elapsed(start, accepted))
            terminal = next((r for r in after if r['event'] in ('state', 'end')
                             and r.get('state') in ('succeeded', 'failed', 'cancelled')), None)
            if terminal:
                reason = str(terminal.get('reason') or '')
                outcome = terminal['state']
                if 'cancelled_by_agent' in reason or 'cancelled by operator' in reason:
                    outcome = 'operator_cancelled'
                error = terminal.get('error')
                item.update(outcome=outcome, terminal_line=terminal['_line'],
                            terminal_reason=reason,
                            terminal_code=error.get('code') if isinstance(error, dict) else None,
                            accepted_to_terminal_observed_ms=elapsed(accepted, terminal))
            else:
                item.update(outcome='no_terminal_in_call_log')
                if ends:
                    item['end_reason'] = str(ends[-1].get('reason') or '')
        else:
            end = ends[0] if ends else None
            reason = str(end.get('reason') or '') if end else ''
            match = re.match(r'HTTP (\d{3})\b', reason)
            status = int(match.group(1)) if match else None
            if status:
                category = 'business_rejection' if status == 409 else 'http_error_response'
            elif 'Connection refused' in reason:
                category = 'connect_refused'
            elif reason:
                category = 'other_unconfirmed'
            else:
                category = 'missing_submission_result'
            item.update(submission=category, http_status=status, outcome='not_accepted',
                        response_line=end['_line'] if end else None,
                        response_ms=elapsed(start, end) if end else None,
                        response_error=end.get('error') if end else None,
                        response_reason=reason)
        included.append(item)
    summaries = {}
    for day in args.days + ['all']:
        selected = [x for x in included if day == 'all' or x['date'] == day]
        timing = [x['response_ms'] for x in selected if x['submission'] == 'accepted']
        summaries[day] = {
            'commands': len(selected), 'tasks': len({x['task_id'] for x in selected}),
            'submissions': dict(Counter(x['submission'] for x in selected)),
            'outcomes_among_accepted': dict(Counter(x['outcome'] for x in selected
                                                   if x['submission'] == 'accepted')),
            'accepted_ms': {'min': min(timing), 'median': statistics.median(timing),
                            'max': max(timing)} if timing else {},
        }
    failure_reasons = Counter(x.get('terminal_reason') for x in included if x['outcome'] == 'failed')
    failure_groups = Counter()
    for item in included:
        if item['outcome'] != 'failed':
            continue
        reason = item['terminal_reason']
        group = ('localization_map_validation' if 'inflated known-free space' in reason
                 else 'data_freshness_or_source_timing' if 'timing' in reason
                 else 'navigation_path_pose_progress_or_terminal')
        failure_groups[group] += 1
    health_records = []
    for day in args.days:
        path = args.root / 'logs' / day / 'dream/monitor.log'
        if not path.exists():
            continue
        for no, line in enumerate(path.read_text().splitlines(), 1):
            category = ('connect_refused' if 'Connection refused' in line
                        else 'no_route' if 'No route to host' in line
                        else 'timeout_stage_unknown' if 'timed out' in line
                        else 'http_200_logged' if 'HTTP 200' in line else 'other')
            health_records.append({'source': str(path.relative_to(args.root)),
                                   'line': no, 'date': day, 'category': category})
    legacy_inventory = []
    for path in sorted((args.root / 'logs').glob('*/reception/events.jsonl')):
        records = [json.loads(line) for line in path.read_text().splitlines()]
        # Event date is authoritative: one historical folder has a stale date.
        legacy_inventory.append({
            'source': str(path.relative_to(args.root)),
            'event_dates': sorted({r['time'][:10] for r in records}),
            'events': dict(Counter(r['event'] for r in records)),
            'failures': [{'line': no, 'time': r['time'], 'task_id': r.get('task_id'),
                          'phase': r.get('failed_phase'), 'error': r.get('error'),
                          'last_remote_state': (r.get('error_payload') or {}).get('state')
                          if isinstance(r.get('error_payload') or {}, dict) else None}
                         for no, r in enumerate(records, 1) if r['event'] == 'TASK_FAILED']})
    result = {'method': __doc__, 'sources': sources, 'summaries': summaries,
              'excluded': excluded, 'commands': included,
              'execution_failure_reasons': dict(failure_reasons),
              'execution_failure_groups': dict(failure_groups),
              'health_log_records': health_records,
              'health_log_record_counts_not_request_counts': dict(Counter(
                  x['category'] for x in health_records)),
              'legacy_inventory_not_pooled_with_modern_calls': legacy_inventory,
              'limits': ['Multiple software revisions and intentional debugging/retries.',
                         'No complete HTTP query/cancel denominator or server traces.',
                         'accepted event may follow recovery lookup; not proof of first POST success.',
                         'Times are client journal intervals, not pure network RTT.',
                         'Scope is available records, not all field operation.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'summaries': summaries, 'excluded_commands': len(excluded),
                      'execution_failure_groups': dict(failure_groups),
                      'health_log_record_counts': result['health_log_record_counts_not_request_counts']},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
