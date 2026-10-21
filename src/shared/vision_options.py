"""Request options for existing vision endpoints; no endpoint/config discovery."""

import os
from urllib.parse import urlsplit


def client_options(api_base, *, timeout):
    """Loopback inference must not inherit desktop HTTP/SOCKS proxy settings."""
    from shared.networks import normalize_proxy_environment
    normalize_proxy_environment()
    options = {"timeout": float(timeout), "max_retries": 0}
    if urlsplit(api_base).hostname in {"127.0.0.1", "localhost", "::1"}:
        import httpx
        options["http_client"] = httpx.Client(trust_env=False, timeout=float(timeout))
    return options


def api_key(config, default_names=("VLM_API_KEY", "CLOUD_API_KEY")):
    """Unauthenticated endpoints must opt in; never infer auth from a hostname."""
    if config.get("auth_required", True) is False:
        return "local"
    name = config.get("api_key_env")
    names = (name,) if name else default_names
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    raise ValueError("缺少模型API Key环境变量: " + ", ".join(names))


def generation_options(config, *, max_tokens, temperature=0, raw=False):
    """Preserve per-caller defaults and explicitly forward local model controls."""
    options = {"temperature": config.get("temperature", temperature)}
    budget = config.get("max_tokens", max_tokens)
    if budget is not None:
        options["max_tokens"] = budget
    for name in ("top_p", "presence_penalty", "seed", "response_format"):
        if name in config:
            options[name] = config[name]
    extra = config.get("extra_body") or {}
    if not isinstance(extra, dict):
        raise ValueError("extra_body 必须是映射")
    reserved = {"model", "messages", "stream", "stream_options", "n",
                "max_tokens", "temperature", "top_p", "presence_penalty",
                "seed", "response_format"}
    if reserved.intersection(extra):
        raise ValueError("extra_body 不能覆盖模型、消息、流式或标准生成参数")
    if extra:
        if raw:
            options.update(extra)
        else:
            options["extra_body"] = dict(extra)
    return options
