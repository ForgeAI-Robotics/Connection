"""Bounded GET proxy to the configured Brain; never directly access the gateway."""
import json
import time

from brain.api.diagnostic_reports import compact_report, report_error
from clients.brain import BrainClient
from shared.networks import entry_url


MAX_RESPONSE_BYTES = 256 * 1024


def task_report(task_id, refresh=False):
    client, response = None, None
    started = time.monotonic()
    try:
        client = BrainClient(entry_url('MASTER_URL'), timeout=6, source='ops')
        response = client.request('GET', '/api/task_diagnostics',
                                  params={'task_id': task_id, 'refresh': '1' if refresh else '0'},
                                  timeout=(2, 6), stream=True, allow_redirects=False)
        if response.status_code not in {200, 202, 404, 429, 502, 503}:
            return report_error(task_id, 'BRAIN_REPORT_UNAVAILABLE', f'大脑摘要接口返回 HTTP {response.status_code}'), 502
        content = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if time.monotonic() - started > 8:
                raise TimeoutError('读取大脑诊断摘要超时')
            content.extend(chunk)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError('大脑诊断摘要响应超过 256 KiB')
        try:
            value = json.loads(content)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError('大脑诊断摘要响应格式无效') from None
        if response.status_code == 429:
            return report_error(task_id, 'BRAIN_REPORT_BUSY', '大脑诊断查询繁忙，请稍后重试'), 429
        return compact_report(value, task_id), response.status_code
    except ValueError as exc:
        return report_error(task_id, 'BRAIN_REPORT_INVALID', exc), 502
    except Exception as exc:
        return report_error(task_id, 'BRAIN_REPORT_UNAVAILABLE', exc), 503
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.session.close()
            except Exception:
                pass
