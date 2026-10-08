"""Silence successful read-only polling, retain failures and recovery."""
import logging
import json
import re
import threading
from urllib.parse import urlsplit


POLL_PATHS = {
    '/health', '/system_status', '/robot_status', '/api/task_status',
    '/api/execution_profile', '/api/scene_state', '/api/belief',
    '/api/robot_status', '/api/quad_latest', '/api/task_events', '/api/timeline',
    '/api/demo/status', '/api/success_pending', '/api/failure_pending',
    '/api/status', '/api/execution', '/api/brain/overview', '/api/auto_tools',
}
POLL_NAMES = {
    '/health': '健康检查（health）',
    '/system_status': '系统状态（system_status）',
    '/robot_status': '机器人状态（robot_status）',
    '/api/task_status': '任务状态（task_status）',
    '/api/execution_profile': '执行配置（execution_profile）',
    '/api/scene_state': '场景状态（scene_state）',
    '/api/belief': '场景认知状态（belief）',
    '/api/robot_status': '机器人现场状态（robot_status）',
    '/api/quad_latest': '相机画面（quad_latest）',
    '/api/task_events': '任务事件（task_events）',
    '/api/timeline': '时间线（timeline）',
    '/api/demo/status': '示教状态（demo_status）',
    '/api/success_pending': '成功经验待处理（success_pending）',
    '/api/failure_pending': '失败经验待处理（failure_pending）',
    '/api/status': '状态（status）',
    '/api/execution': '执行（execution）',
    '/api/brain/overview': '大脑概览（brain_overview）',
    '/api/auto_tools': '工具列表（auto_tools）',
}
_ACCESS = re.compile(r'"(GET|HEAD) (\S+) HTTP/[\d.]+"\s+(\d{3})\b')
_ANY_ACCESS = re.compile(r'"(GET|HEAD|POST|PUT|PATCH|DELETE) \S+ HTTP/')
_ANSI = re.compile(r'\x1b\[[0-9;]*m')
_STARTUP = ('development server', 'Press CTRL+C', 'Running on', 'Serving Flask', 'Debug mode')
DETAILED_POLLS = {'/api/robot_status': '机器人现场状态',
                  '/api/quad_latest': '相机画面', '/api/belief': '场景认知状态'}


def _poll_label(path):
    return POLL_NAMES.get(path, path)


class BeijingFormatter(logging.Formatter):
    """One RoboAgent-style line. Handled exceptions keep the error text, not the stack."""

    def format(self, record):
        from shared.task_text import beijing_block
        message = _ANSI.sub('', record.getMessage())
        if len(message) >= 24 and message[4] == '-' and ' - master - ' in message[:40]:
            rendered = message
        else:
            level = {'WARNING': 'WARN', 'CRITICAL': 'ERROR'}.get(record.levelname, record.levelname)
            if record.name == 'brain.http_monitor' and record.levelno >= logging.WARNING:
                level = 'INFO' if message.startswith('监测恢复') else 'ERROR'
            rendered = beijing_block(level, message, name='master')
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None:
            summary = f'{type(exc).__name__}: {exc}'
            if summary not in rendered:
                rendered += ' ' + summary
        return rendered


class ProcessNoiseFilter(logging.Filter):
    """Keep Flask banners, raw access lines and httpx one-liners out of the process log."""

    def filter(self, record):
        message = record.getMessage()
        name = record.name or ''
        if name == 'httpx' or name == 'httpcore' or name.startswith('httpx.') or name.startswith('httpcore.'):
            if 'HTTP Request' in message or 'HTTP Response' in message:
                return False
        if name == 'werkzeug':
            if any(piece in message for piece in _STARTUP) or message.lstrip().startswith('*'):
                return False
            if _ANY_ACCESS.search(_ANSI.sub('', message)):
                return False
        return True


class QuietPollFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.failures = {}
        self.lock = threading.Lock()
        self.handled_paths = set()

    def filter(self, record):
        text = record.getMessage()
        match = _ACCESS.search(_ANSI.sub('', text))
        if not match:
            return True
        path = urlsplit(match[2]).path
        if path in self.handled_paths:
            return False  # The response-aware hook already logged this poll.
        if path not in POLL_PATHS and not (
                path.startswith('/api/services/') and path.endswith('/logs')):
            return True
        code = int(match[3])
        with self.lock:
            previous = self.failures.get(path)
            label = _poll_label(path)
            if code < 400:
                if previous is None:
                    return False
                del self.failures[path]
                record.msg = f'监测恢复：{label} {match[1]} {path}\n  HTTP {code}'
                record.args = ()
                return True
            if previous == code:
                return False
            self.failures[path] = code
        record.msg = f'监测失败：{label} {match[1]} {path}\n  HTTP {code}'
        record.args = ()
        return True


def install_poll_diagnostics(app):
    """Log actual response errors; the access-log IP is only the immediate caller."""
    from flask import g, request
    failures = {}
    lock = threading.Lock()
    logger = logging.getLogger('brain.http_monitor')
    access = logging.getLogger('werkzeug')
    quiet = next((f for f in access.filters if isinstance(f, QuietPollFilter)), None)
    if quiet is None:
        quiet = QuietPollFilter()
        access.addFilter(quiet)
    quiet.handled_paths.update(DETAILED_POLLS)

    @app.after_request
    def log_poll_response(response):
        path = request.path
        if request.method not in {'GET', 'HEAD'} or path not in DETAILED_POLLS:
            return response
        code = response.status_code
        detail = getattr(g, 'poll_diagnostic', {})
        error = ''
        if code >= 400:
            body = response.get_json(silent=True) if response.is_json else None
            if isinstance(body, dict):
                error = body.get('error') or body.get('message') or body.get('reason') or body
            else:
                error = response.get_data().decode('utf-8', errors='replace')
            if not isinstance(error, str):
                error = json.dumps(error, ensure_ascii=False, default=str)
        signature = (code, detail.get('target'), detail.get('reason'), error)
        with lock:
            previous = failures.get(path)
            if code < 400:
                if previous is None:
                    return response
                del failures[path]
            else:
                if previous == signature:
                    return response
                failures[path] = signature
        label = f'{DETAILED_POLLS[path]}（{path.rsplit("/", 1)[-1]}）'
        caller = request.remote_addr or '未知'
        if code < 400:
            logger.info('监测恢复：%s %s %s\n  HTTP %s\n  来源 IP=%s（caller）',
                        label, request.method, path, code, caller)
        else:
            logger.error('监测失败：%s %s %s\n  来源 IP=%s（caller）\n  target=%s\n  原因=%s\n  HTTP %s\n  error=%s',
                         label, request.method, path, caller,
                         detail.get('target') or '未解析到下游地址',
                         detail.get('reason') or '大脑接口返回异常', code, error)
        return response


def configure_process_logging(service=None):
    """Called after stdout/stderr are attached to the process log."""
    import sys
    root = logging.getLogger()
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # Bind a console sink to the tee even when logging was configured earlier.
    streams = [h for h in root.handlers
               if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
    if not streams:
        handler = logging.StreamHandler(sys.stderr)
        if service == 'master':
            handler.setFormatter(BeijingFormatter())
        else:
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        root.addHandler(handler)
        streams = [handler]
    else:
        for handler in streams:
            if handler.stream in (sys.__stdout__, sys.__stderr__):
                handler.setStream(sys.stderr)
            if service == 'master':
                handler.setFormatter(BeijingFormatter())
    if service == 'master':
        for handler in streams:
            if not any(isinstance(item, ProcessNoiseFilter) for item in handler.filters):
                handler.addFilter(ProcessNoiseFilter())
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)
    logger = logging.getLogger('werkzeug')
    if not any(isinstance(f, QuietPollFilter) for f in logger.filters):
        logger.addFilter(QuietPollFilter())
