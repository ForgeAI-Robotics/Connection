"""Bounded, read-only diagnostic evidence queries; never a task-state authority."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

from shared.log_setup import log_root


SCHEMA = 'fq/brain-diagnostics/v1'
MAX_RECORDS = 2000
MAX_FILES = 128
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 512 * 1024
MAX_SCANNED_RECORDS = 50000
MAX_ENTRIES = 8192
MAX_ATTACHMENTS = 8
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ASSOCIATIONS = 4096
_IDENTITY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$')
_BUSINESS_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
_REQUEST_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_SERVICE = re.compile(r'^[a-z][a-z0-9_-]{0,47}$')
_PROCESS = re.compile(r'^\d{2}-\d{2}-\d{2}(?:_\d+)?(?:\.[A-Za-z0-9_-]+)*\.log$')
_JSONL_FILE = re.compile(r'\.jsonl(?:\.\d{1,4})?$', re.I)
PEERS = ('nav', 'vla', 'gateway')
LOCAL_SERVICES = ('master', 'deploy', 'feishu', 'voice')


def _invalid_constant(value):
    raise ValueError('Non-finite JSON number')


def _redact(value):
    from shared.diagnostic_io import redact
    return redact(value)


def _identity(value, name):
    if value is None or value == '':
        return None
    if not _valid_identity(value, name):
        raise ValueError(f'{name} 必须为 1—128 位 ASCII 身份标识，符合原业务接口规则')
    return value


def _valid_identity(value, name):
    pattern = _REQUEST_ID if name == 'request_id' else _BUSINESS_ID
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _date(value, name):
    if not value:
        return None
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise ValueError(f'{name} 必须为 YYYY-MM-DD')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f'{name} 日期无效') from None


def _directory(path):
    """Open every path component without following symlinks, including root parents."""
    parts = Path(os.path.abspath(path)).parts
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_file(path):
    path = Path(os.path.abspath(path))
    directory = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('诊断来源必须是普通文件，不能是符号链接或设备')
    return os.fdopen(fd, 'rb')


def _discover(root, first, last, issues):
    """Bound directory enumeration as well as content reads."""
    found, inspected = [], 0

    def entries(fd):
        nonlocal inspected
        with os.scandir(fd) as items:
            for item in items:
                inspected += 1
                if inspected > MAX_ENTRIES:
                    issues['directory_entry_limit'] += 1
                    break
                yield item

    try:
        root_fd = _directory(root)
    except FileNotFoundError:
        issues['log_root_missing'] += 1
        return []
    except OSError:
        issues['log_root_unreadable_or_symlink'] += 1
        return []
    try:
        days = []
        for item in entries(root_fd):
            if not _DAY.fullmatch(item.name):
                continue
            try:
                day = date.fromisoformat(item.name).isoformat()
            except ValueError:
                continue
            if (first and day < first) or (last and day > last):
                continue
            if item.is_symlink():
                issues['symlink_skipped'] += 1
            elif item.is_dir(follow_symlinks=False):
                days.append(day)
        for day in sorted(days, reverse=True):
            if inspected > MAX_ENTRIES:
                break
            try:
                day_fd = os.open(day, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except OSError:
                issues['directory_unreadable'] += 1
                continue
            try:
                services = []
                for item in entries(day_fd):
                    if item.name not in LOCAL_SERVICES:
                        continue
                    if item.is_symlink():
                        issues['symlink_skipped'] += 1
                    elif item.is_dir(follow_symlinks=False):
                        services.append(item.name)
                for service in sorted(services, key=LOCAL_SERVICES.index):
                    if inspected > MAX_ENTRIES:
                        break
                    try:
                        service_fd = os.open(service, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=day_fd)
                    except OSError:
                        issues['directory_unreadable'] += 1
                        continue
                    try:
                        for item in entries(service_fd):
                            kind = ('events' if item.name.startswith('brain') and item.name.endswith('.jsonl')
                                    else 'startup' if service == 'master' and _PROCESS.fullmatch(item.name) else None)
                            if not kind:
                                continue
                            if item.is_symlink():
                                issues['symlink_skipped'] += 1
                            elif item.is_file(follow_symlinks=False):
                                found.append((day, service, item.name, kind))
                    finally:
                        os.close(service_fd)
            finally:
                os.close(day_fd)
    finally:
        os.close(root_fd)
    # Active brain.jsonl precedes timestamped rotations; bounded newest files first.
    return sorted(found, reverse=True)


def _identities(record):
    """Match only event identities, never IDs merely mentioned in arbitrary text/raw receipts."""
    values = {
        'task_id': record.get('task_id') or (record.get('id') if record.get('kind') in {'TASK', 'CALL', 'INBOUND'} else None),
        'command_id': record.get('command_id') or record.get('cmd') or record.get('open_command_id'),
        'request_id': record.get('request_id') or record.get('source_gate_request_id'),
    }
    return {key: value if _valid_identity(value, key) else None
            for key, value in values.items()}


class _Associations:
    """Bounded correlations established by local Brain events, never peer payloads."""
    def __init__(self, issues):
        self.requests, self.commands = {}, {}
        self.issues = issues

    def observe(self, event, identity, source):
        if source['service'] != 'master' or not identity['task_id']:
            return
        if event.get('identity_truncated'):
            self.issues['association_identity_truncated'] += 1
            return
        runtime = event.get('kind') == 'TASK' and event.get('component') == 'runtime'
        selection = (event.get('kind') == 'GATEWAY' and event.get('component') == 'source_gate'
                     and event.get('event') in {'selection_intent_saved', 'selection_verified'})
        if not runtime and not selection:
            return
        receiver = event.get('receiver_instance_id') if selection else None
        receiver = receiver if isinstance(receiver, str) and _IDENTITY.fullmatch(receiver) else None
        event_id = event.get('event_id')
        provenance = {'event_id': event_id if isinstance(event_id, str) and _IDENTITY.fullmatch(event_id) else None,
                      'source': source, 'identity': identity.copy(), 'receiver_instance_id': receiver}
        for key, mapping in (('command_id', self.commands), ('request_id', self.requests)):
            value = identity[key]
            if not value or (key == 'request_id' and not selection):
                continue
            if value not in mapping:
                if len(self.requests) + len(self.commands) >= MAX_ASSOCIATIONS:
                    self.issues['association_limit'] += 1
                    continue
                mapping[value] = {'task_id': set(), 'command_id': set(), 'receiver_instance_id': set(), 'sources': []}
            bucket = mapping[value]
            # Two distinct values are enough to prove ambiguity; bound adversarial duplicates.
            for field, actual in (('task_id', identity['task_id']), ('command_id', identity['command_id']),
                                  ('receiver_instance_id', receiver)):
                if actual is not None and len(bucket[field]) < 2:
                    bucket[field].add(actual)
            if len(bucket['sources']) < 4 and provenance not in bucket['sources']:
                bucket['sources'].append(provenance)

    def correlate(self, event, identity, service, filters, allowed):
        direct = not any(v is not None and identity[k] != v for k, v in filters.items())
        if not allowed:
            if not direct:
                self.issues['peer_association_unavailable_incomplete_local_scan'] += 1
            return (identity, {'basis': 'native_identity'}) if direct else (None, None)
        mappings = []
        if identity['request_id'] in self.requests:
            mappings.append(('request_id', self.requests[identity['request_id']]))
        if identity['command_id'] in self.commands:
            mappings.append(('command_id', self.commands[identity['command_id']]))
        if not mappings:
            if not direct and any(filters[k] is not None and identity[k] is None for k in filters):
                self.issues['peer_identity_mapping_missing'] += 1
            return (identity, {'basis': 'native_identity'}) if direct else (None, None)
        resolved, evidence, expected_receivers = identity.copy(), [], set()
        for key, bucket in mappings:
            # A command can be involved in multiple selected sources, but must have one task.
            required = ('task_id', 'command_id', 'receiver_instance_id') if key == 'request_id' else ('task_id',)
            if any(len(bucket[field]) > 1 for field in required):
                self.issues['ambiguous_peer_association'] += 1
                return None, None
            inferred = ('task_id', 'command_id') if key == 'request_id' else ('task_id',)
            for field in inferred:
                values = bucket[field]
                if not values:
                    continue
                value = next(iter(values))
                if resolved[field] is not None and resolved[field] != value:
                    self.issues['conflicting_peer_identity'] += 1
                    return None, None
                # Malformed explicitly claimed identities cannot be treated as absent.
                aliases = (('task_id', 'id') if event.get('kind') in {'TASK', 'CALL', 'INBOUND'} else ('task_id',)) if field == 'task_id' else ('command_id', 'cmd', 'open_command_id')
                if resolved[field] is None and any(event.get(alias) not in (None, '') for alias in aliases):
                    self.issues['invalid_peer_identity_claim'] += 1
                    return None, None
                resolved[field] = value
            if key == 'request_id':
                expected_receivers.update(bucket['receiver_instance_id'])
            evidence.extend(bucket['sources'])
        receiver_check = 'not_applicable'
        if service == 'gateway' and expected_receivers:
            receipt = event.get('receipt') if isinstance(event.get('receipt'), dict) else {}
            claimed = [value for value in (event.get('receiver_instance_id'), receipt.get('receiver_instance_id'))
                       if value not in (None, '')]
            if any(not isinstance(value, str) or value not in expected_receivers for value in claimed):
                self.issues['conflicting_peer_receiver'] += 1
                return None, None
            receiver_check = 'matched' if claimed else 'not_provided'
        if any(v is not None and resolved[k] != v for k, v in filters.items()):
            return None, None
        return resolved, {'basis': 'explicit_local_brain_identity_mapping', 'resolved_identity': resolved,
                          'sources': evidence[:8], 'receiver_check': receiver_check}


def _lines(handle, budget, issues):
    """Read a bounded snapshot suffix; concurrent appends are left for the next query."""
    size = os.fstat(handle.fileno()).st_size
    offset = max(0, size - budget)
    if offset:
        issues['file_prefix_not_scanned'] += 1
    handle.seek(offset)
    data = handle.read(min(size, budget))
    consumed = len(data)
    if offset:
        # Drop the cut first record, unless starting exactly at a newline boundary.
        handle.seek(offset - 1)
        if handle.read(1) != b'\n':
            end = data.find(b'\n')
            data = data[end + 1:] if end >= 0 else b''
            offset += end + 1 if end >= 0 else consumed
            issues['partial_line_skipped'] += 1
    def backwards():
        end = len(data)
        if end and data[-1:] != b'\n':
            issues['partial_line_skipped'] += 1
            end = data.rfind(b'\n') + 1
        while end:
            start = data.rfind(b'\n', 0, end - 1) + 1
            if end - start > MAX_LINE_BYTES + 1:  # JSON record budget excludes the newline.
                issues['oversize_line_skipped'] += 1
            else:
                raw = data[start:end]
                if raw.strip():
                    yield offset + start, raw
            end = start
    return backwards(), consumed


def _safe_time(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('附件时间必须是带时区的 ISO 8601 字符串')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('附件时间无效') from None
    if stamp.tzinfo is None:
        raise ValueError('附件时间必须带时区')
    return value


def _event_time(event):
    """Native wall-clock aliases are observations, never causal/monotonic ordering."""
    for key in ('time', 'timestamp', 'recorded_at', 'at'):
        value = event.get(key)
        if not isinstance(value, str) or len(value) > 64:
            continue
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if moment.tzinfo is not None:
                moment.astimezone(timezone.utc)  # Reject offset/year combinations outside datetime range.
                return moment
        except (ValueError, OverflowError):
            pass
    return None


def _sort_key(row):
    moment = _event_time(row['event'])
    return (moment is None, moment.astimezone(timezone.utc) if moment else datetime.max.replace(tzinfo=timezone.utc),
            row['source']['file'], row['source']['byte_offset'])


def _attachment_match(manifest, filters):
    wanted = {k: v for k, v in filters.items() if v is not None}
    if not wanted:
        return 'no_identity_filter'
    associations = manifest.get('associations') or []
    if not associations:
        return 'unassociated'
    if any(all(row.get(k) == v for k, v in wanted.items()) for row in associations):
        return 'explicit_identity_match_metadata_only'
    return 'not_matched_to_query'


def load_attachment_manifest(path):
    """Explicit CLI-only metadata import. Never open any file referenced by the manifest."""
    if Path(path).suffix.lower() != '.json':
        raise ValueError('附件清单必须是显式指定的 JSON 文件')
    with _open_file(path) as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('附件清单超过 1 MiB')
    try:
        value = json.loads(raw, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('附件清单不是有效 JSON') from None
    if not isinstance(value, dict) or value.get('schema') not in {
            'fq/diagnostic-attachment/v1', 'g1/rosbag-evidence-manifest/v1'}:
        raise ValueError('不支持的附件清单 schema')
    # Deliberate whitelist: a GET response/current_state is not an attachment manifest.
    allowed = ('schema', 'kind', 'host_role', 'host', 'boot_id', 'session_id', 'session', 'run_id', 'session_directory', 'session_path',
               'started_at', 'ended_at', 'start_time', 'end_time', 'recording_status', 'status',
               'started_monotonic', 'ended_monotonic', 'active', 'data_available', 'resource_samples', 'bag',
               'snapshot_at', 'snapshot_consistent', 'task_authority', 'scope',
               'service_instance_id', 'code_revision', 'associations', 'files', 'limitations')
    clean = {k: value[k] for k in allowed if k in value}
    for key in ('started_at', 'ended_at', 'start_time', 'end_time', 'snapshot_at'):
        if key in clean:
            _safe_time(clean[key])
    for begin, end in (('started_at', 'ended_at'), ('start_time', 'end_time')):
        if clean.get(begin) and clean.get(end):
            if datetime.fromisoformat(clean[begin].replace('Z', '+00:00')) > datetime.fromisoformat(clean[end].replace('Z', '+00:00')):
                raise ValueError('附件结束时间早于开始时间')
    for key in ('active', 'data_available', 'snapshot_consistent', 'task_authority'):
        if key in clean and clean[key] is not None and type(clean[key]) is not bool:
            raise ValueError('附件状态字段必须为布尔值或 null')
    if clean.get('task_authority') is True:
        raise ValueError('录制清单不能声明任务状态权威')
    for key in ('started_monotonic', 'ended_monotonic', 'resource_samples'):
        if key in clean and clean[key] is not None and (type(clean[key]) not in (int, float) or clean[key] < 0):
            raise ValueError('附件计数／单调时间必须为非负数或 null')
    if 'bag' in clean:
        bag = clean['bag']
        if not isinstance(bag, dict):
            raise ValueError('bag 元数据必须为对象')
        clean['bag'] = {k: bag[k] for k in ('total_messages', 'missing_topics', 'metadata_present', 'errors') if k in bag}
        topics = bag.get('topics', {})
        if not isinstance(topics, dict) or len(topics) > 512:
            raise ValueError('bag 话题清单最多为 512 项')
        clean['bag']['topics'] = {name: {k: row[k] for k in ('type', 'messages') if k in row}
                                  for name, row in topics.items() if isinstance(row, dict)}
    files = clean.get('files', [])
    if not isinstance(files, list) or len(files) > 512:
        raise ValueError('附件文件清单必须为最多 512 项的列表')
    clean['files'] = [{k: row[k] for k in ('name', 'path', 'role', 'bytes', 'size_bytes', 'sha256', 'status') if k in row}
                      for row in files if isinstance(row, dict)]
    associations = clean.get('associations', [])
    if not isinstance(associations, list) or len(associations) > 512:
        raise ValueError('附件关联必须为最多 512 项的列表')
    clean['associations'] = []
    for row in associations:
        if not isinstance(row, dict):
            raise ValueError('附件关联条目必须为对象')
        item = {k: _identity(row[k], k) for k in ('task_id', 'command_id', 'request_id') if k in row}
        if not any(item.values()):
            raise ValueError('附件关联缺少任务、命令或请求身份')
        clean['associations'].append(item)
    clean.update(verification='metadata_only_unverified', referenced_files_read=False,
                 association_basis='operator_supplied' if associations else 'unassociated')
    return _redact(clean)


def query_diagnostics(root=None, *, task_id=None, command_id=None, request_id=None,
                      date_from=None, date_to=None, limit=500, peer_events=(), attachments=()):
    """Read local diagnostic archives; optional peer inputs are trusted operator-selected paths."""
    filters = {k: _identity(v, k) for k, v in (
        ('task_id', task_id), ('command_id', command_id), ('request_id', request_id))}
    first, last = _date(date_from, 'date_from'), _date(date_to, 'date_to')
    if first and last and first > last:
        raise ValueError('开始日期不得晚于结束日期')
    try:
        count = int(limit)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('limit 必须为整数') from None
    if isinstance(limit, bool) or str(count) != str(limit) or not 1 <= count <= MAX_RECORDS:
        raise ValueError(f'limit 必须为 1—{MAX_RECORDS} 的整数')
    if len(peer_events) > MAX_ATTACHMENTS or len(attachments) > MAX_ATTACHMENTS:
        raise ValueError('最多导入 8 份对端日志和 8 份附件清单')
    root = Path(root) if root is not None else log_root()
    issues, scanned, records, startups = Counter(), [], [], []
    peer_stats = {service: {'files_supplied': 0, 'files_read': 0, 'matching_records_scanned': 0,
                            'records_returned': 0} for service in PEERS}
    sources = []
    for peer_index, (service, path) in enumerate(peer_events):
        if service not in PEERS or not _JSONL_FILE.search(Path(path).name):
            raise ValueError('对端日志须使用 nav/vla/gateway 与显式 JSONL 文件')
        peer_stats[service]['files_supplied'] += 1
        sources.append((Path(path), f'peer/{peer_index}/{service}/{Path(path).name}', service, 'events', True))
    local_sources = [(root / day / service / filename, f'{day}/{service}/{filename}', service, kind, False)
                     for day, service, filename, kind in _discover(root, first, last, issues)]
    sources = local_sources + sources
    if len(sources) > MAX_FILES:
        issues['file_limit'] += len(sources) - MAX_FILES
    scanned_bytes, result_bytes, matched, line_count = 0, 0, 0, 0
    associations = _Associations(issues)
    association_gaps = None
    map_gaps = {'directory_entry_limit', 'log_root_missing', 'log_root_unreadable_or_symlink',
                'directory_unreadable', 'symlink_skipped', 'file_limit', 'scan_byte_limit',
                'scan_record_limit', 'file_unreadable_or_unsafe', 'file_prefix_not_scanned',
                'partial_line_skipped', 'oversize_line_skipped', 'invalid_json_line',
                'non_object_line', 'unsupported_log_schema', 'association_limit', 'association_identity_truncated'}
    for path, label, service, kind, imported in sources[:MAX_FILES]:
        if imported and association_gaps is None:
            association_gaps = sorted(key for key in map_gaps if issues[key])
        if line_count >= MAX_SCANNED_RECORDS:
            issues['scan_record_limit'] += 1
            break
        if scanned_bytes >= MAX_SCAN_BYTES:
            issues['scan_byte_limit'] += 1
            break
        try:
            with _open_file(path) as handle:
                if kind == 'startup':
                    data = handle.read(min(2048, MAX_SCAN_BYTES - scanned_bytes))
                    scanned_bytes += len(data)
                    meta = {}
                    for line in data.decode('utf-8', 'replace').splitlines():
                        match = re.match(r'^# (service|started_at|pid|service_instance_id|code_revision)=(.*)$', line)
                        if match:
                            meta[match[1]] = match[2][:256]
                    if meta:
                        startups.append({'source': label, 'metadata': _redact(meta),
                                         'association': 'log_directory_only_not_task_proof'})
                    continue
                lines, used = _lines(handle, MAX_SCAN_BYTES - scanned_bytes, issues)
                scanned_bytes += used
                scanned.append(label)
                if imported:
                    peer_stats[service]['files_read'] += 1
                for offset, raw in lines:
                    line_count += 1
                    if line_count > MAX_SCANNED_RECORDS:
                        issues['scan_record_limit'] += 1
                        break
                    try:
                        event = json.loads(raw, parse_constant=_invalid_constant)
                    except (ValueError, UnicodeError, RecursionError):
                        issues['invalid_json_line'] += 1
                        continue
                    if not isinstance(event, dict):
                        issues['non_object_line'] += 1
                        continue
                    version = event.get('log_schema_version')
                    if version is not None and (type(version) is not int or version != 1):
                        issues['unsupported_log_schema'] += 1
                        continue
                    identity = _identities(event)
                    source = {'file': label, 'byte_offset': offset, 'service': service, 'imported': imported}
                    correlation = {'basis': 'native_identity'}
                    if imported:
                        effective_identity, correlation = associations.correlate(
                            event, identity, service, filters, not association_gaps)
                        if effective_identity is None:
                            continue
                    else:
                        associations.observe(event, identity, source)
                        if any(v is not None and identity[k] != v for k, v in filters.items()):
                            continue
                    # Imported logs may live outside date-named folders; apply their event date.
                    if imported and (first or last):
                        moment = _event_time(event)
                        if moment is None:
                            issues['peer_event_date_unknown'] += 1
                            continue
                        day = moment.date().isoformat()
                        if (first and day < first) or (last and day > last):
                            continue
                    matched += 1
                    if imported:
                        peer_stats[service]['matching_records_scanned'] += 1
                    if len(records) >= count:
                        issues['record_limit'] += 1
                        continue
                    item = {'event': _redact(event), 'source': source, 'identity': identity,
                            'correlation': correlation,
                            'format': 'legacy' if version is None else 'v1'}
                    encoded = json.dumps(item, ensure_ascii=False).encode('utf-8')
                    if event.get('diagnostic_truncated') or b'diagnostic truncated' in encoded or b'_diagnostic_truncated' in encoded:
                        issues['record_content_truncated'] += 1
                    if result_bytes + len(encoded) > MAX_RESULT_BYTES:
                        issues['result_byte_limit'] += 1
                        continue
                    records.append(item)
                    if _event_time(item['event']) is None:
                        issues['event_time_unknown'] += 1
                    if imported:
                        peer_stats[service]['records_returned'] += 1
                    result_bytes += len(encoded)
        except (OSError, ValueError):
            issues['file_unreadable_or_unsafe'] += 1
    records.sort(key=_sort_key)
    instance_keys, instances, links = set(), [], []
    for row in records:
        e = row['event']
        key = (row['source']['service'], e.get('service_instance_id'), e.get('code_revision'))
        # Untrusted values must not become hash keys or arbitrary HTML.
        key = tuple(str(v)[:256] if v is not None else None for v in key)
        if key not in instance_keys:
            instance_keys.add(key)
            instances.append(dict(zip(('service', 'service_instance_id', 'code_revision'), key)))
        if any(row['identity'].values()):
            links.append({**row['identity'], 'event_id': e.get('event_id'), 'source': row['source'],
                          'correlation': row['correlation']})
    manifests = [load_attachment_manifest(p) for p in attachments]
    for manifest in manifests:
        manifest['query_association'] = _attachment_match(manifest, filters)
    if len(json.dumps(manifests, ensure_ascii=False).encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('附件清单元数据合计超过 2 MiB，请分批导出')
    return {'schema': SCHEMA, 'generated_at': datetime.now().astimezone().isoformat(),
            'read_only': True, 'state_authority': False,
            'query': {**filters, 'date_from': first, 'date_to': last, 'limit': count},
            'records': records, 'identity_links': links, 'service_instances': instances,
            'startup_sources': startups, 'attachments': manifests,
            'coverage': {'scope': 'local_archives_and_explicit_imports', 'local_services': list(LOCAL_SERVICES),
                         'files_scanned': scanned,
                         'bytes_scanned': scanned_bytes, 'matched_in_scanned_data': matched,
                         'returned': len(records), 'incomplete': bool(issues), 'issues': dict(issues),
                         'max_files': MAX_FILES, 'max_scan_bytes': MAX_SCAN_BYTES,
                         'max_result_bytes': MAX_RESULT_BYTES, 'max_scanned_records': MAX_SCANNED_RECORDS,
                         'associations': {'request_keys': len(associations.requests), 'command_keys': len(associations.commands),
                                          'max_keys': MAX_ASSOCIATIONS, 'local_scan_gaps': association_gaps if association_gaps is not None else sorted(k for k in map_gaps if issues[k]),
                                          'scope': 'explicit_local_brain_events_in_selected_archive_range'},
                         'date_basis': 'local_log_directory_date_and_peer_event_date_in_its_recorded_timezone',
                         'order': 'bounded_newest_files_then_utc_time_unknown_last_not_causality'},
            'peer_evidence': peer_stats,
            'missing_evidence': [{'service': p, 'reason': ('peer_logs_not_imported' if not counts['files_supplied']
                                    else 'peer_logs_unreadable_or_not_scanned' if not counts['files_read']
                                    else 'no_matching_peer_evidence' if not counts['matching_records_scanned']
                                    else 'matching_peer_evidence_not_returned_within_limits')}
                                 for p, counts in peer_stats.items() if not counts['records_returned']],
            'limitations': ['归档记录不代表当前任务或机器人物理状态。',
                            '时间显示顺序不证明跨机器因果顺序；未校准的墙钟不能计算网络延迟。',
                            '扫描有文件、字节和条数上限；损坏、截断、未支持格式与缺失证据在 coverage 中列出。',
                            '附件仅导入显式提供的清单元数据，不读取 bag、图片或清单引用文件。']}
