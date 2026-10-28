"""Small standard-library HTTP client shared by DREAM and VLA adapters."""

from __future__ import annotations

import json
import http.client
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from shared.service_credentials import service_token


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

    def url(self, path):
        value = str(path)
        if value.startswith(("http://", "https://")):
            return value
        return urllib.parse.urljoin(self.base_url + "/", value.lstrip("/"))

    def request_bytes(self, method, path, payload=None, *, timeout_sec=None, headers=None):
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
