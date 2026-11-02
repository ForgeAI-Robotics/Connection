"""Small standard-library HTTP client shared by DREAM and VLA adapters."""

from __future__ import annotations

import json
import hashlib
import http.client
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import threading
import time
import uuid
from collections import OrderedDict
from shared.service_credentials import service_token
from shared.diagnostic_context import context_fields, diagnostic_context, diagnostic_event


class HttpContractError(RuntimeError):
    def __init__(self, message, *, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class HttpClient:
    def __init__(self, base_url, *, timeout_sec=10.0, contract_version=None, ca_file=None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"无效HTTP服务地址: {base_url!r}")
        self.timeout_sec = float(timeout_sec)
        self.contract_version = str(contract_version or "").strip()
        self.auth_token_env = None  # Optional joint-control service credential, never the token itself.
        self.ssl_context = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self._diagnostic_polls = OrderedDict()
        self._diagnostic_lock = threading.Lock()

    def url(self, path):
        value = str(path)
        if value.startswith(("http://", "https://")):
            return value
        return urllib.parse.urljoin(self.base_url + "/", value.lstrip("/"))

    def request_bytes(self, method, path, payload=None, *, timeout_sec=None, headers=None):
        started = time.monotonic()
        write = str(method).upper() not in {'GET', 'HEAD'}
        identity = {key: payload[key] for key in ('task_id', 'command_id', 'request_id')
                    if isinstance(payload, dict) and isinstance(payload.get(key), str)}
        parent = context_fields().get('call_id')
        with diagnostic_context(call_id=uuid.uuid4().hex, parent_call_id=parent, **identity):
            details = {'component': 'brain.http', 'method': str(method).upper(),
                       'endpoint': self.url(path), 'timeout_sec': float(timeout_sec or self.timeout_sec)}
            if write:
                diagnostic_event('HTTP', 'started', **details, _human_suppressed=True)
            try:
                result = self._request_bytes(method, path, payload, timeout_sec=timeout_sec, headers=headers)
            except Exception as exc:
                elapsed = time.monotonic() - started
                if write or self._poll_diagnostic(path, str(exc)):
                    diagnostic_event('HTTP', 'failed', **details, duration_s=round(elapsed, 6),
                        level='ERROR', error=str(exc), exception_type=type(exc).__name__,
                        http_status=getattr(exc, 'status_code', None))
                raise
            elapsed = time.monotonic() - started
            report = write or self._poll_diagnostic(path, None, slow=elapsed >= 2.)
            if report:
                diagnostic_event('HTTP', 'completed', **details, duration_s=round(elapsed, 6),
                    http_status=result[1], transport_only=True, _human_suppressed=write)
            return result

    def _poll_diagnostic(self, path, error, *, slow=False):
        """Bounded failure/recovery and slow-call notices; ordinary GETs stay quiet."""
        key = hashlib.sha256(str(path).encode('utf-8', 'replace')).hexdigest()
        signature = hashlib.sha256(error.encode('utf-8', 'replace')).hexdigest() if error is not None else None
        now = time.monotonic()
        with self._diagnostic_lock:
            previous = self._diagnostic_polls.pop(key, None)
            if error is None and not slow:
                return previous is not None and previous[0] is not None
            emit = previous is None or previous[0] != signature or now - previous[1] >= 30.
            self._diagnostic_polls[key] = (signature, now if emit else previous[1])
            while len(self._diagnostic_polls) > 128:
                self._diagnostic_polls.popitem(last=False)
            return emit

    def _request_bytes(self, method, path, payload=None, *, timeout_sec=None, headers=None):
        data = None
        headers = dict(headers or {})
        opener = urllib.request.urlopen
        if self.auth_token_env:
            try:
                token = service_token(self.auth_token_env)
            except ValueError as exc:
                raise HttpContractError(str(exc)) from None
            if not token:
                raise HttpContractError('服务鉴权环境变量未配置')
            def origin(url):
                parts = urllib.parse.urlsplit(url)
                if parts.username or parts.password or not parts.hostname:
                    raise HttpContractError('服务 URL 身份无效')
                return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)
            if origin(self.url(path)) != origin(self.base_url):
                raise HttpContractError('拒绝向其他服务地址发送鉴权凭据')
            headers.setdefault('Authorization', 'Bearer ' + token)
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, newheaders, newurl):
                    return None
            handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
            if self.ssl_context is not None:
                handlers.append(urllib.request.HTTPSHandler(context=self.ssl_context))
            opener = urllib.request.build_opener(*handlers).open
        elif self.ssl_context is not None:
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.ssl_context)).open
        if self.contract_version:
            headers["X-Contract-Version"] = self.contract_version
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.url(path), data=data, headers=headers, method=str(method).upper())
        try:
            with opener(
                req, timeout=float(timeout_sec or self.timeout_sec)
            ) as response:
                return (
                    response.read(),
                    int(response.status),
                    {str(k): str(v) for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw.decode("utf-8"))
            except Exception:
                detail = raw.decode("utf-8", errors="replace")
            raise HttpContractError(
                f"HTTP {exc.code} {self.url(path)}: {detail}",
                status_code=exc.code,
                payload=detail,
            ) from exc
        except urllib.error.URLError as exc:
            raise HttpContractError(
                f"服务不可达 {self.url(path)}: {exc.reason}") from exc
        except (TimeoutError, ConnectionError, http.client.HTTPException, socket.timeout, OSError) as exc:
            raise HttpContractError(
                f"HTTP连接状态不确定 {self.url(path)}: {exc}") from exc

    def request_json(
        self, method, path, payload=None, *, accepted_statuses=(200,), timeout_sec=None, headers=None
    ):
        options = {'timeout_sec': timeout_sec}
        if headers is not None:
            options['headers'] = headers
        raw, status, headers = self.request_bytes(method, path, payload, **options)
        if status not in set(accepted_statuses):
            raise HttpContractError(
                f"HTTP状态不符合契约: {status}",
                status_code=status,
                payload=raw.decode("utf-8", errors="replace"),
            )
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            raise HttpContractError(
                f"服务返回非JSON响应(HTTP {status})",
                status_code=status,
                payload=raw.decode('utf-8', errors='replace'),
            ) from exc
        if not isinstance(body, dict):
            raise HttpContractError("服务JSON响应必须是对象", status_code=status, payload=body)
        return body, status, headers
