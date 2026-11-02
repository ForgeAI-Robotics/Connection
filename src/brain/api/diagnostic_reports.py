"""Small, read-only views of task-bound robot diagnostic summaries."""
import json
import re

from flask import jsonify, request

from shared.diagnostic_io import clip_text, redact


MAX_REPORT_BYTES = 64 * 1024
_TASK_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_REPORT_FIELDS = ('schema', 'task_id', 'run_id', 'report_id', 'status', 'state', 'generated_at',
                  'updated_at', 'summary', 'coverage', 'missing_evidence', 'findings',
                  'evidence_refs', 'limitations', 'analysis_state', 'window', 'quality',
                  'versions', 'state_authority', 'replayed')


def report_query(arguments):
    """Require original task identity; never substitute the current/latest task."""
    if set(arguments) - {'task_id', 'refresh'} or any(len(arguments.getlist(k)) != 1 for k in arguments):
        raise ValueError('仅支持 task_id 和 refresh 查询参数')
    task_id = arguments.get('task_id')
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValueError('必须指定原 task_id：1—128 位 ASCII 标识符，首字符为字母或数字')
    refresh = arguments.get('refresh', '0')
    if refresh not in {'0', '1'}:
        raise ValueError('refresh 只能为 0 或 1')
    return task_id, refresh == '1'


def report_error(task_id, code, message):
    return {'enabled': True, 'task_id': task_id, 'run_id': None, 'tracking': 'query_only',
            'refreshing': False, 'report': None, 'error': {'code': code, 'message': clip_text(redact(str(message)), 1000)},
            'incomplete_reasons': [code], 'dropped_events': 0, 'updated_at': None,
            'read_only': True, 'task_authority': False, 'raw_artifacts_included': False}


def compact_report(value, task_id):
    """Defense in depth over the coordinator's validated summary; no raw bag payloads."""
    if not isinstance(value, dict) or value.get('task_id') != task_id:
        raise ValueError('诊断摘要不属于请求的原任务')
    if value.get('run_id') is not None and (not isinstance(value['run_id'], str) or not _TASK_ID.fullmatch(value['run_id'])):
        raise ValueError('诊断摘要录制身份格式无效')
    error = value.get('error')
    if error is not None and not isinstance(error, dict):
        raise ValueError('诊断摘要错误字段格式无效')
    reasons = value.get('incomplete_reasons') or []
    if not isinstance(reasons, list):
        raise ValueError('诊断摘要缺失证据字段格式无效')
    result = {'enabled': value.get('enabled') is True, 'task_id': task_id,
              'run_id': clip_text(str(value['run_id']), 128) if value.get('run_id') is not None else None,
              'tracking': clip_text(str(value.get('tracking') or 'unknown'), 64),
              'refreshing': value.get('refreshing') is True,
              'error': {'code': clip_text(str(error.get('code') or 'UNKNOWN'), 128),
                        'message': clip_text(str(error.get('message') or ''), 1000)} if error else None,
              'incomplete_reasons': [clip_text(str(reason), 256) for reason in reasons[:32]],
              'dropped_events': value.get('dropped_events') if type(value.get('dropped_events')) is int else 0,
              'updated_at': clip_text(str(value['updated_at']), 64) if value.get('updated_at') is not None else None}
    body = value.get('report')
    if body is not None:
        if not isinstance(body, dict):
            raise ValueError('诊断报告格式无效')
        if body.get('schema') != 'fq/robot-diagnostic-report/v1':
            raise ValueError('不支持的诊断报告版本')
        if body.get('state_authority') is not False:
            raise ValueError('诊断报告不能作为任务状态权威')
        if body.get('task_id') != task_id:
            raise ValueError('诊断报告任务身份不匹配')
        if body.get('run_id') is not None and value.get('run_id') is not None and body['run_id'] != value['run_id']:
            raise ValueError('诊断报告录制身份不匹配')
        result['report'] = {key: body[key] for key in _REPORT_FIELDS if key in body}
        for key in ('findings', 'evidence_refs', 'limitations'):
            if not isinstance(result['report'].get(key, []), list):
                raise ValueError('诊断报告列表字段格式无效')
    else:
        result['report'] = None
    # The client validates the full schema; the presentation budget is smaller.
    result = redact(result, max_depth=12, max_items=4096)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_REPORT_BYTES - 256:
        result['report'] = None
        result['error'] = {'code': 'REPORT_VIEW_TOO_LARGE', 'message': '报告摘要超出展示上限，未展示正文'}
        result['incomplete_reasons'] = ['REPORT_VIEW_TOO_LARGE']
    result.update(read_only=True, task_authority=False, raw_artifacts_included=False)
    return result


def report_status(value):
    if value.get('refreshing'):
        return 202
    error = value.get('error') or {}
    if value.get('report') is not None:
        return 200  # A cached report remains viewable, with refresh errors visibly attached.
    if value.get('tracking') == 'not_tracked' or error.get('code') in {
            'NOT_FOUND', 'RUN_NOT_FOUND', 'DIAGNOSTIC_RUN_NOT_FOUND', 'TASK_NOT_FOUND', 'HTTP_404'}:
        return 404
    return 503 if error else 200


def register_diagnostic_reports(app, application):
    @app.get('/api/task_diagnostics')
    def task_diagnostics():
        try:
            task_id, refresh = report_query(request.args)
        except ValueError as exc:
            response = jsonify(error=str(exc))
            response.status_code = 400
        else:
            coordinator = getattr(application, 'diagnostic_runs', None)
            if coordinator is None:
                payload, status = report_error(task_id, 'DIAGNOSTICS_UNAVAILABLE', '大脑未提供任务诊断摘要'), 503
            else:
                try:
                    # Refresh queues only the coordinator's existing GET path.
                    # HEAD stays cache-only; neither path begins or finishes recording.
                    value = coordinator.refresh(task_id) if refresh and request.method == 'GET' else coordinator.summary(task_id)
                    payload = compact_report(value, task_id)
                    status = report_status(payload)
                except ValueError as exc:
                    payload, status = report_error(task_id, 'DIAGNOSTIC_IDENTITY_OR_FORMAT_ERROR', exc), 502
                except Exception as exc:
                    payload, status = report_error(task_id, 'DIAGNOSTICS_UNAVAILABLE', exc), 503
            response = jsonify(payload)
            response.status_code = status
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
