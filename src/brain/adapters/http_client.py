"""Small standard-library HTTP client shared by DREAM and VLA adapters."""

from __future__ import annotations

import json
import http.client
import socket
import urllib.error
import urllib.parse
import urllib.request


class HttpContractError(RuntimeError):
    def __init__(self, message, *, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class HttpClient:
    def __init__(self, base_url, *, timeout_sec=10.0, contract_version=None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError(f"无效HTTP服务地址: {base_url!r}")
        self.timeout_sec = float(timeout_sec)
        self.contract_version = str(contract_version or "").strip()

    def url(self, path):
        value = str(path)
        if value.startswith(("http://", "https://")):
            return value
        return urllib.parse.urljoin(self.base_url + "/", value.lstrip("/"))

    def request_bytes(self, method, path, payload=None, *, timeout_sec=None):
        data = None
        headers = {}
        if self.contract_version:
            headers["X-Contract-Version"] = self.contract_version
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.url(path), data=data, headers=headers, method=str(method).upper())
        try:
            with urllib.request.urlopen(
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
        self, method, path, payload=None, *, accepted_statuses=(200,), timeout_sec=None
    ):
        raw, status, headers = self.request_bytes(
            method, path, payload, timeout_sec=timeout_sec)
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
            ) from exc
        if not isinstance(body, dict):
            raise HttpContractError("服务JSON响应必须是对象", payload=body)
        return body, status, headers
