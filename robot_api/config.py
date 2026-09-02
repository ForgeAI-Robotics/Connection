"""Configuration for the unified robot API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "robot_api" / "config.yaml"
DEFAULT_URL = "http://127.0.0.1:5001"
DEFAULT_TIMEOUT = 120


@dataclass(frozen=True)
class BackendConfig:
    name: str
    enabled: bool
    provide_state: bool
    accept_action: bool
    required: bool
    url: str = ""
    timeout: float = DEFAULT_TIMEOUT
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class PolicyServiceConfig:
    name: str
    enabled: bool
    url: str = ""
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class RobotApiConfig:
    backends: list[BackendConfig]
    active_backend: str | None = None
    navigation: BackendConfig | None = None
    policy_services: list[PolicyServiceConfig] | None = None

    def _active(self) -> BackendConfig | None:
        if not self.active_backend:
            return None
        for backend in self.backends:
            if backend.name == self.active_backend:
                return backend
        return None

    @property
    def server_url(self) -> str:
        active = self._active()
        if active is not None and active.url:
            return active.url
        for backend in self.backends:
            if backend.enabled and backend.url:
                return backend.url
        return DEFAULT_URL

    @property
    def timeout(self) -> float:
        for backend in self.backends:
            if backend.enabled:
                return backend.timeout
        return DEFAULT_TIMEOUT

    @property
    def backend(self) -> str:
        active = self._active()
        if active is not None:
            return active.name
        for backend in self.backends:
            if backend.enabled:
                return backend.name
        return self.active_backend or "mujoco"

    def state_backends(self) -> list[BackendConfig]:
        active = self._active()
        if active is not None:
            return [active] if active.enabled and active.provide_state else []
        return [b for b in self.backends if b.enabled and b.provide_state]

    def action_backends(self) -> list[BackendConfig]:
        active = self._active()
        if active is not None:
            return [active] if active.enabled and active.accept_action else []
        return [b for b in self.backends if b.enabled and b.accept_action]

    def navigation_backend(self) -> BackendConfig | None:
        if self.navigation is not None and self.navigation.enabled:
            return self.navigation
        return None

    def policy_service(self, name: str) -> PolicyServiceConfig | None:
        """Get an enabled policy service by name (e.g. "act")."""
        if not self.policy_services:
            return None
        for svc in self.policy_services:
            if svc.name == name and svc.enabled:
                return svc
        return None

    def active_policy_services(self) -> list[PolicyServiceConfig]:
        """Return all enabled policy services."""
        if not self.policy_services:
            return []
        return [svc for svc in self.policy_services if svc.enabled]


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        if yaml is not None:
            with path.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return _read_simple_yaml(path)
    except Exception:
        return {}


def _read_simple_yaml(path: Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        key = key.strip().strip("\"'")
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if not value:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(value)
    return root


def _parse_scalar(value: str):
    value = value.strip().strip("\"'")
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if lowered.startswith("0x"):
            return int(lowered, 16)
        if any(ch in value for ch in (".", "e", "E")):
            return float(value)
        return int(value)
    except ValueError:
        return value


def _default_backends() -> dict[str, dict[str, Any]]:
    return {
        "mujoco": {
            "enabled": 1,
            "provide_state": 1,
            "accept_action": 1,
            "required": 1,
            "url": DEFAULT_URL,
            "timeout": DEFAULT_TIMEOUT,
        }
    }


def load_robot_api_config() -> RobotApiConfig:
    data = _read_yaml(CONFIG_PATH)
    backends_cfg = data.get("backends") or _default_backends()

    active_backend = str(
        os.getenv("ROBOT_API_BACKEND")
        or os.getenv("ROBOT_BACKEND")
        or data.get("active_backend")
        or ""
    ).strip() or None
    env_url = os.getenv("ROBOT_API_URL")
    env_nav_url = os.getenv("ROBOT_NAV_URL") or os.getenv("NAV2_API_URL")
    env_timeout = os.getenv("ROBOT_API_TIMEOUT")
    backends: list[BackendConfig] = []

    for name, raw in backends_cfg.items():
        raw = raw or {}
        url = str(raw.get("url") or "").rstrip("/")
        if env_url and (active_backend is None or name == active_backend):
            url = env_url.rstrip("/")
        timeout = float(env_timeout or raw.get("timeout") or DEFAULT_TIMEOUT)
        backends.append(
            BackendConfig(
                name=str(name),
                enabled=_as_bool(raw.get("enabled"), default=False),
                provide_state=_as_bool(raw.get("provide_state"), default=False),
                accept_action=_as_bool(raw.get("accept_action"), default=False),
                required=_as_bool(raw.get("required"), default=False),
                url=url,
                timeout=timeout,
                raw=raw,
            )
        )

    if not backends:
        return RobotApiConfig(
            backends=[
                BackendConfig(
                    name="mujoco",
                    enabled=True,
                    provide_state=True,
                    accept_action=True,
                    required=True,
                    url=DEFAULT_URL,
                    timeout=DEFAULT_TIMEOUT,
                    raw={},
                )
            ],
            active_backend=active_backend,
            navigation=None,
        )

    nav_cfg = data.get("navigation") or {}
    navigation = None
    if nav_cfg:
        nav_url = str(env_nav_url or nav_cfg.get("url") or "").rstrip("/")
        navigation = BackendConfig(
            name=str(nav_cfg.get("backend") or "nav2"),
            enabled=_as_bool(nav_cfg.get("enabled"), default=False),
            provide_state=False,
            accept_action=True,
            required=True,
            url=nav_url,
            timeout=float(env_timeout or nav_cfg.get("timeout") or DEFAULT_TIMEOUT),
            raw=nav_cfg,
        )

    policy_services = None
    ps_cfg = data.get("policy_services") or {}
    if ps_cfg:
        policy_services = [
            PolicyServiceConfig(
                name=str(name),
                enabled=_as_bool(raw.get("enabled"), default=False),
                url=str(raw.get("url") or "").rstrip("/"),
                raw=raw,
            )
            for name, raw in ps_cfg.items()
            if isinstance(raw, dict)
        ]

    return RobotApiConfig(
        backends=backends, active_backend=active_backend,
        navigation=navigation, policy_services=policy_services,
    )
