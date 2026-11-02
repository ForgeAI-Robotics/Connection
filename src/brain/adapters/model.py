"""Model transport configured by the application, created lazily on first use."""
from openai import OpenAI, AzureOpenAI
from typing import Dict
import logging
import time
import threading
import uuid
from shared.diagnostic_context import (context_fields, diagnostic_context,
                                       diagnostic_event, report_exception)


def _purpose(messages):
    blob = []
    for item in messages or []:
        content = item.get('content') if isinstance(item, dict) else ''
        if isinstance(content, str):
            blob.append(content)
        elif isinstance(content, list):
            blob.extend(part.get('text', '') for part in content
                        if isinstance(part, dict) and isinstance(part.get('text'), str))
    text = '\n'.join(blob)
    if '任务异常分析器' in text:
        return '异常建议（recovery_advice）'
    if '选择任务业务包' in text:
        return '业务包选择（package_select）'
    if '事后反思' in text:
        return '任务复盘（reflection）'
    return '模型调用（llm）'


def _excerpt(messages, limit=500):
    for item in messages or []:
        content = item.get('content') if isinstance(item, dict) else ''
        if isinstance(content, str) and content.strip():
            text = content.strip()
            return text if len(text) <= limit else text[:limit - 3] + '...'
        if isinstance(content, list):
            texts = [part.get('text', '').strip() for part in content
                     if isinstance(part, dict) and part.get('type') == 'text']
            joined = ' '.join(piece for piece in texts if piece)
            if joined:
                return joined if len(joined) <= limit else joined[:limit - 3] + '...'
    return ''

class ModelClient:
    def __init__(self, config):
        self.config = config
        self.global_model = None
        self._init_lock = threading.Lock()
        self.profiling = bool(config.get("profiling", False))

    def __call__(self, messages):
        if self.global_model is None:
            with self._init_lock:
                if self.global_model is None:
                    client, self.model_name = self._get_model_info_from_config(self.config["model"])
                    self.global_model = client
        return self._call_llm(messages)

    def display_profiling_info(self, description, message):
        if self.profiling:
            # Never dump full prompts, response objects or internal credentials.
            diagnostic_event('MODEL', 'profiling', component='brain.model',
                             description=description, value_chars=len(str(message)),
                             _human_suppressed=True)

    def _get_model_info_from_config(self, config: Dict) -> tuple:
        """Get the model info from config."""
        from shared.networks import normalize_proxy_environment
        normalize_proxy_environment()
        candidate = config["model_dict"]
        if candidate["cloud_model"] in config["model_select"]:
            if candidate["cloud_type"] == "azure":
                model_name = config["model_select"]
                model_client = AzureOpenAI(
                    azure_endpoint=candidate["azure_endpoint"],
                    azure_deployment=candidate["azure_deployment"],
                    api_version=candidate["azure_api_version"],
                    api_key=candidate["azure_api_key"], timeout=float(config.get("timeout_sec", 60)), max_retries=0,
                )
            elif candidate["cloud_type"] == "default":
                model_client = OpenAI(
                    base_url=candidate["cloud_server"],
                    api_key=candidate["cloud_api_key"], timeout=float(config.get("timeout_sec", 60)), max_retries=0,
                )
                model_name = config["model_select"]
            else:
                raise ValueError(f"Unsupported cloud type: {candidate['cloud_type']}")
            return model_client, model_name
        raise ValueError(f"Unsupported model: {config['model_select']}")

    def _call_llm(self, messages: list) -> str:
        """Record lifecycle metadata while preserving model request semantics."""
        started = time.perf_counter()
        base = str(getattr(self.global_model, 'base_url', '') or '').rstrip('/')
        endpoint = base + ('' if base.endswith('/chat/completions') else '/chat/completions')
        purpose = _purpose(messages)
        settings = self.config["model"]
        generation = {
            "temperature": settings.get("temperature", 0.2),
            "top_p": settings.get("top_p", 0.9),
            "max_tokens": settings.get("max_tokens", 2048),
            "seed": settings.get("seed", 42),
        }
        for key in ("presence_penalty", "extra_body"):
            if key in settings:
                generation[key] = settings[key]
        parent_call = context_fields().get('call_id')
        with diagnostic_context(call_id=uuid.uuid4().hex, parent_call_id=parent_call):
            diagnostic_event('MODEL', 'started', component='brain.model', purpose=purpose,
                method='POST', endpoint=endpoint, model=self.model_name,
                timeout_sec=settings.get('timeout_sec', 60))
            try:
                response = self.global_model.chat.completions.create(
                    model=self.model_name, messages=messages, **generation)
                raw = response.choices[0].message.content
                if raw is None:
                    raw = ''
                elif isinstance(raw, list):
                    raw = ''.join(part.get('text', '') if isinstance(part, dict)
                                  else getattr(part, 'text', '') for part in raw)
                elif not isinstance(raw, str):
                    raw = str(raw)
            except Exception as exc:
                self._log_forward(purpose, endpoint, started, error=exc)
                report_exception(exc, component='brain.model', purpose=purpose)
                raise
            self._log_forward(purpose, endpoint, started, result=raw, status=200)
            return raw

    def _log_forward(self, purpose, endpoint, started, **fields):
        error = fields.get('error')
        diagnostic_event('MODEL', 'failed' if error is not None else 'completed',
            component='brain.model', purpose=purpose, method='POST', endpoint=endpoint,
            model=self.model_name, duration_s=round(time.perf_counter() - started, 6),
            http_status=fields.get('status'), level='ERROR' if error is not None else 'INFO',
            error=str(error) if error is not None else None,
            exception_type=type(error).__name__ if error is not None else None,
            response_chars=len(fields['result']) if isinstance(fields.get('result'), str) else None)
