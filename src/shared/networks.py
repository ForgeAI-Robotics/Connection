"""当前 WiFi 下各端地址。唯一来源是 config/networks.yaml。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from shared.paths import workspace_root

ROLES = ("brain", "nav", "control", "robot")


class Lan:
    def __init__(self, data: dict):
        if not isinstance(data, dict):
            raise ValueError("networks.yaml 必须是映射")
        self.wifi = str(data.get("active") or "").strip()
        wifis = data.get("wifis") or {}
        roles = data.get("roles") or {}
        if self.wifi not in wifis:
            known = "、".join(str(name) for name in wifis)
            raise ValueError(f"未知 WiFi: {self.wifi or '(空)'}；可选 {known}")
        self._roles = roles
        self._ips = {}
        chosen = wifis[self.wifi] or {}
        for role in ROLES:
            ip = str(chosen.get(role) or "").strip()
            if not ip:
                raise ValueError(f"{self.wifi} 缺少 {role} IP")
            self._ips[role] = ip

    def ip(self, role: str) -> str:
        if role not in self._ips:
            raise ValueError(f"未知端: {role}")
        return self._ips[role]

    def ssh_target(self, role: str) -> str:
        user = str((self._roles.get(role) or {}).get("ssh_user") or "").strip()
        if not user:
            raise ValueError(f"{role} 没有 ssh_user")
        return f"{user}@{self.ip(role)}"

    def http(self, role: str, port_key: str = "http_port") -> str:
        port = int((self._roles.get(role) or {}).get(port_key) or 0)
        if port <= 0:
            raise ValueError(f"{role} 没有 {port_key}")
        return f"http://{self.ip(role)}:{port}"

    def review_url(self) -> str:
        port = int((self._roles.get("nav") or {}).get("review_port") or 0)
        if port <= 0:
            raise ValueError("nav 没有 review_port")
        return f"http://{self.ip('nav')}:{port}"


def load_lan(path: Path | None = None) -> Lan:
    path = path or workspace_root() / "config" / "networks.yaml"
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Lan(data)


def lan() -> Lan:
    return load_lan()


def entry_url(env_name: str, port_key: str = "http_port") -> str:
    """现场入口默认跟随地址表；独立部署可显式提供完整 URL。"""
    value = os.environ.get(env_name, "").strip().rstrip("/")
    if value:
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{env_name} 必须是完整 HTTP(S) URL")
        return value
    return lan().http("brain", port_key)


def apply_to_brain_config(config: dict) -> dict:
    """大脑访问导航、操控的地址跟当前 WiFi，不看 brain.yaml 里写死的旧 URL。"""

    site = lan()
    real = config.setdefault("reception_real", {})
    real["dream_base_url"] = site.http("nav")
    real["vla_base_url"] = site.http("control")
    return config


def apply_proxy_bypass() -> None:
    """当前各端 IP 不走代理。"""

    site = lan()
    extras = [site.ip(role) for role in ROLES]
    for key in ("NO_PROXY", "no_proxy"):
        parts = [item.strip() for item in os.environ.get(key, "").split(",") if item.strip()]
        for ip in extras:
            if ip not in parts:
                parts.append(ip)
        if parts:
            os.environ[key] = ",".join(parts)
