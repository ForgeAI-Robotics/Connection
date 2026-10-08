"""Model transport configured by the application, created lazily on first use."""
from openai import OpenAI, AzureOpenAI
from typing import Dict
import logging
import time


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
        self.profiling = bool(config.get("profiling", False))

    def __call__(self, messages):
        if self.global_model is None:
            self.global_model, self.model_name = self._get_model_info_from_config(self.config["model"])
        return self._call_llm(messages)

    def display_profiling_info(self, description, message):
        if self.profiling:
            logging.getLogger("model").debug("%s: %s", description, message)

    def _get_model_info_from_config(self, config: Dict) -> tuple:
        """Get the model info from config."""
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
        """调用 LLM 并返回文本结果。"""
        self.display_profiling_info("messages", messages)

        from datetime import datetime
        start_inference = datetime.now()
        started = time.perf_counter()
        base = str(getattr(self.global_model, 'base_url', '') or '').rstrip('/')
        endpoint = base + ('' if base.endswith('/chat/completions') else '/chat/completions')
        purpose = _purpose(messages)
        try:
            response = self.global_model.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=0.2,
                top_p=0.9,
                max_tokens=2048,
                seed=42,
            )
        except Exception as exc:
            self._log_forward(purpose, endpoint, started, request_text=_excerpt(messages), error=exc)
            raise
        end_inference = datetime.now()

        self.display_profiling_info(
            "inference time",
            f"inference start:{start_inference} end:{end_inference} during:{end_inference-start_inference}",
        )
        self.display_profiling_info("response", response)
        self.display_profiling_info("response.usage", response.usage)

        raw = response.choices[0].message.content
        if raw is None:
            raw = ""
        elif isinstance(raw, list):
            raw = "".join(
                part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                for part in raw
            )
        elif not isinstance(raw, str):
            raw = str(raw)
        self._log_forward(purpose, endpoint, started, request_text=_excerpt(messages),
                          result=raw, status=200)
        return raw

    def _log_forward(self, purpose, endpoint, started, **fields):
        # 正常的异常建议留在账本，不把提示词和 JSON 打进进程日志。
        if str(purpose).startswith('异常建议') and 'error' not in fields:
            return
        if str(purpose).startswith('异常建议'):
            fields.pop('request_text', None)
            fields.pop('result', None)
        try:
            from shared.log_setup import log_forward
            log_forward(purpose, 'POST', endpoint, model=self.model_name,
                        duration_s=time.perf_counter() - started, **fields)
        except Exception:
            logging.getLogger('model').debug('forward log failed', exc_info=True)
