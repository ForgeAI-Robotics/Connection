import os
import time
from contracts.intent import ROUTE_SYSTEM

def route_ambiguous_with_llm(text: str) -> str:
    """Return 'chat' or 'task' for leftover ambiguous wording."""

    import requests

    api_key = (os.getenv("CLOUD_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("未配置 CLOUD_API_KEY，无法做歧义分流")
    base = (os.getenv("CLOUD_API_BASE") or "https://api.deepseek.com").strip().rstrip("/")
    model = (os.getenv("CLOUD_CHAT_MODEL") or "deepseek-chat").strip()
    endpoint = f"{base}/chat/completions"
    started = time.perf_counter()
    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": ROUTE_SYSTEM},
                    {"role": "user", "content": text},
                ],
                "temperature": 0,
                "max_tokens": 16,
            },
            timeout=15.0,
        )
    except Exception as exc:
        _log_route(endpoint, model, text, started, error=exc)
        raise
    if not response.ok:
        _log_route(endpoint, model, text, started, status=response.status_code,
                   error=f"分流接口返回 HTTP {response.status_code}")
        raise RuntimeError(f"分流接口返回 HTTP {response.status_code}")
    payload = response.json()
    choices = payload.get("choices") or []
    message = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
    label = str(message).strip()
    if not label:
        _log_route(endpoint, model, text, started, status=response.status_code, error="分流接口返回空内容")
        raise RuntimeError("分流接口返回空内容")
    _log_route(endpoint, model, text, started, status=response.status_code, result=label)
    return label


def _log_route(endpoint, model, text, started, **fields):
    try:
        from shared.log_setup import log_forward
        log_forward('意图路由（intent_route）', 'POST', endpoint, model=model,
                    duration_s=time.perf_counter() - started, request_text=text, **fields)
    except Exception:
        pass
