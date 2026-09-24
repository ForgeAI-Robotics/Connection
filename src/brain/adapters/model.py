"""Model transport configured by the application, created lazily on first use."""
from openai import OpenAI, AzureOpenAI
from typing import Dict
import logging

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
        response = self.global_model.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=0.2,
            top_p=0.9,
            max_tokens=2048,
            seed=42,
        )
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
        return raw
