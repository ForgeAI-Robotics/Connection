import os
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
    if not response.ok:
        raise RuntimeError(f"分流接口返回 HTTP {response.status_code}")
    payload = response.json()
    choices = payload.get("choices") or []
    message = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
    label = str(message).strip()
    if not label:
        raise RuntimeError("分流接口返回空内容")
    return label
