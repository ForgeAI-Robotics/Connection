"""VLM image analysis followed by LLM evidence adjudication."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request


class VerificationError(RuntimeError):
    pass


def _env_key(name):
    value = os.environ.get(str(name or ""))
    if not value:
        raise VerificationError(f"缺少模型API Key环境变量: {name}")
    return value


def _content_text(message_content):
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        return "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in message_content
        )
    return str(message_content or "")


def _parse_json_object(text):
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I | re.S)
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise VerificationError(f"模型未返回JSON对象: {value[:300]}")
        try:
            data = json.loads(value[start:end + 1])
        except json.JSONDecodeError as exc:
            raise VerificationError(f"模型JSON解析失败: {value[:300]}") from exc
    if not isinstance(data, dict):
        raise VerificationError("模型结果必须是JSON对象")
    return data


class ReceptionVerifier:
    def __init__(self, config, *, chat_completion=None):
        self.config = dict(config or {})
        self._chat_completion_override = chat_completion
        self.min_confidence = float(self.config.get("min_vlm_confidence", 0.5))

    def _chat(self, endpoint_config, messages, *, max_tokens):
        if self._chat_completion_override:
            return self._chat_completion_override(
                endpoint_config, messages, max_tokens=max_tokens)
        api_base = str(endpoint_config.get("api_base") or "").rstrip("/")
        model = str(endpoint_config.get("model") or "")
        if not api_base or not model:
            raise VerificationError("模型api_base/model未配置")
        key = _env_key(endpoint_config.get("api_key_env"))
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": int(max_tokens),
        }
        req = urllib.request.Request(
            api_base + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=float(endpoint_config.get("timeout_sec", 120))
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            raise VerificationError(f"模型调用失败({model}): {exc}") from exc
        try:
            return _content_text(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise VerificationError(f"模型响应结构错误({model})") from exc

    @staticmethod
    def _vlm_prompt(operation, object_id, target_id):
        if operation == "pick":
            return f"""你是机器人抓取结果视觉核验器。只观察这一张动作完成后的图片。
目标物体ID：{object_id}；抓取位置：{target_id}。
判断目标是否可见、是否确实位于灵巧手/夹爪中。
不得根据动作预期猜测。严格只返回JSON对象：
{{"target_visible":true|false,"target_in_gripper":true|false,
"confidence":0到1,"reason":"简短依据"}}"""
        return f"""你是机器人放置结果视觉核验器。只观察这一张动作完成后的图片。
目标物体ID：{object_id}；目标区域：{target_id}。
判断目标是否可见、是否位于目标桌面、是否已经离开夹爪。
不得根据动作预期猜测。严格只返回JSON对象：
{{"target_visible":true|false,"target_on_table1":true|false,
"target_in_gripper":true|false,"confidence":0到1,"reason":"简短依据"}}"""

    def _analyze_image(self, operation, object_id, target_id, image_bytes, content_type):
        data_url = "data:{};base64,{}".format(
            content_type or "image/jpeg",
            base64.b64encode(image_bytes).decode("ascii"),
        )
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": self._vlm_prompt(operation, object_id, target_id)},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]
        raw = self._chat(self.config.get("vlm") or {}, messages, max_tokens=400)
        return _parse_json_object(raw)

    def _adjudicate(self, operation, object_id, target_id, vla_terminal, snapshot, vlm):
        if operation == "pick":
            fixed_rule = (
                "仅当VLA成功、确实holding目标、导航端口已恢复、图片为动作后新图，"
                "且VLM确认可乐确实位于灵巧手/夹爪中时，verified才可为true。"
            )
        else:
            fixed_rule = (
                "仅当VLA成功释放目标、holding为null、导航端口已恢复、图片为动作后新图，"
                "且VLM确认目标位于table1且不在夹爪中时，verified才可为true。"
            )
        prompt = """你是机器人任务结果的最终证据裁决器。不得改变动作流程，不得把任何明确失败改成成功。
根据固定规则、VLA终态和VLM视觉事实做一致性判断。严格只返回JSON对象：
{{"verified":true|false,"operation":"pick|place","object_id":"...",
"target_id":"...","reason":"简短依据"}}

固定规则：{rule}
VLA终态：{vla}
图片元数据：{snapshot}
VLM结果：{vlm}
""".format(
            rule=fixed_rule,
            vla=json.dumps(vla_terminal, ensure_ascii=False),
            snapshot=json.dumps(snapshot, ensure_ascii=False),
            vlm=json.dumps(vlm, ensure_ascii=False),
        )
        raw = self._chat(
            self.config.get("llm") or {},
            [{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        return _parse_json_object(raw)

    def verify(
        self, operation, *, object_id, target_id, vla_terminal,
        snapshot, image_bytes, content_type="image/jpeg",
    ):
        if operation not in {"pick", "place"}:
            raise VerificationError(f"不支持的判真操作: {operation}")
        vlm = self._analyze_image(
            operation, object_id, target_id, image_bytes, content_type)
        llm = self._adjudicate(
            operation, object_id, target_id, vla_terminal, snapshot, vlm)
        try:
            confidence = float(vlm.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if operation == "pick":
            visual_pass = (
                vlm.get("target_visible") is True
                and vlm.get("target_in_gripper") is True
            )
        else:
            visual_pass = (
                vlm.get("target_visible") is True
                and vlm.get("target_on_table1") is True
                and vlm.get("target_in_gripper") is False
            )
        verified = (
            visual_pass
            and confidence >= self.min_confidence
            and llm.get("verified") is True
            and llm.get("operation") == operation
            and llm.get("object_id") == object_id
            and llm.get("target_id") == target_id
        )
        return {
            "verified": bool(verified),
            "operation": operation,
            "object_id": object_id,
            "target_id": target_id,
            "vlm": vlm,
            "llm": llm,
            "min_vlm_confidence": self.min_confidence,
        }
