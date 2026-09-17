"""Scene answers for Feishu: camera+VLM when a frame exists, else /objects."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv


from robot_api.look import cameras_for


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=False)

CATEGORY_ZH = {
    "milk": "牛奶",
    "cola": "可乐",
    "penholder": "笔筒",
    "trash": "垃圾/纸巾团",
    "personal": "个人物品",
}

NO_RENDER_BACKENDS = {"desk"}

VLM_PROMPT = """你是机器人现场观察助手。用户问：{question}

下面是机器人当前相机画面。根据图片如实描述视野里有什么。
不要编造看不见的东西，不要说正在控制机器人。
用简体中文：先一句总述，再列看到的物体。"""


def format_objects(objects: Any) -> str:
    if not isinstance(objects, dict):
        return "读到的场景数据格式无效，无法列出物体。"
    if objects.get("success") is False:
        detail = str(objects.get("result") or "场景后端不可用")
        return f"读不到现场物体：{detail}"

    items: list[str] = []
    for name, data in objects.items():
        if name in {"success", "result", "_backend", "_required"}:
            continue
        if not isinstance(data, dict):
            continue
        category = str(data.get("category") or "").strip()
        label = CATEGORY_ZH.get(category.lower(), category or name)
        holding = "（机器人手中）" if data.get("grasped") else ""
        if label == name:
            items.append(f"• {name}{holding}")
        else:
            items.append(f"• {label}（{name}）{holding}")

    if not items:
        return "当前场景里没有读到物体。"
    return "当前桌面/场景里有：\n" + "\n".join(items)


def extract_image_b64(result: Any) -> str:
    if not isinstance(result, dict) or result.get("success") is False:
        return ""
    image = result.get("image")
    if isinstance(image, str) and image.strip():
        return image.strip()
    if isinstance(image, (bytes, bytearray)) and image:
        return base64.b64encode(bytes(image)).decode("ascii")
    return ""


def compose_observe_answer(
    *,
    vision: str = "",
    vision_source: str = "",
    vision_error: str = "",
    objects_text: str = "",
) -> str:
    vision = str(vision or "").strip()
    objects_text = str(objects_text or "").strip()
    if vision:
        source = f"（来源：{vision_source} 看图）" if vision_source else "（来源：相机看图）"
        return f"{vision}\n{source}"
    if objects_text.startswith("读不到现场物体"):
        if vision_error:
            return f"{objects_text}；相机：{vision_error}"
        return objects_text
    if not objects_text:
        return f"读不到现场物体：{vision_error or '场景后端不可用'}"
    if vision_error:
        return (
            f"{objects_text}\n"
            f"（当前没有可用相机画面：{vision_error}。以上是物体名单，不是相机实拍。）"
        )
    return objects_text


class QwenVLM:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = (api_key or os.getenv("VLM_API_KEY") or "").strip()
        base = (
            api_base
            or os.getenv("VLM_API_BASE")
            or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        ).strip()
        self.api_base = base.rstrip("/")
        self.model = (model or os.getenv("VLM_MODEL") or "qwen3-vl-plus").strip()
        self.timeout = timeout

    def describe(self, question: str, images: list[tuple[str, str]]) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 VLM_API_KEY，无法看图")
        if not images:
            raise RuntimeError("没有可分析的画面")
        content: list[dict[str, Any]] = [
            {"type": "text", "text": VLM_PROMPT.format(question=question or "前面有什么")}
        ]
        for name, b64 in images:
            content.append({"type": "text", "text": f"[{name}]"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                }
            )
        response = requests.post(
            f"{self.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0,
                "max_tokens": 500,
            },
            timeout=self.timeout,
        )
        if not response.ok:
            raise RuntimeError(f"千问看图接口返回 HTTP {response.status_code}")
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("千问没有返回看图结果")
        text = str((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise RuntimeError("千问看图返回空内容")
        return text


def _master_camera_urls() -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    dream = (os.getenv("DREAM_BASE_URL") or "").strip().rstrip("/")
    vla = (os.getenv("VLA_BASE_URL") or "").strip().rstrip("/")
    if not dream or not vla:
        try:
            from robot_api.config import _read_yaml

            data = _read_yaml(PROJECT_ROOT / "master" / "config.yaml")
            reception = (data or {}).get("reception_real") or {}
            dream = dream or str(reception.get("dream_base_url") or "").rstrip("/")
            vla = vla or str(reception.get("vla_base_url") or "").rstrip("/")
        except Exception:
            pass
    if dream:
        urls.append(("dream", dream))
    if vla:
        urls.append(("vla", vla))
    return urls


def default_vision_targets() -> list[tuple[str, str]]:
    """Local render backends first, then real cameras. Desk is never a vision target."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, url: str) -> None:
        name = str(name or "").strip().lower()
        url = str(url or "").strip().rstrip("/")
        if not name or not url or name in NO_RENDER_BACKENDS or url in seen:
            return
        seen.add(url)
        found.append((name, url))

    try:
        from robot_api.config import load_robot_api_config

        config = load_robot_api_config()
        for backend in config.backends:
            if not backend.enabled or not backend.url:
                continue
            add(backend.name, backend.url)
    except Exception:
        pass
    add("mujoco_3dgs", "http://127.0.0.1:5002")
    add("mujoco", "http://127.0.0.1:5001")
    for name, url in _master_camera_urls():
        add(name, url)
    return found


def _response_image_b64(response: requests.Response) -> str:
    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
    if not response.ok or not response.content:
        return ""
    if content_type.startswith("image/"):
        return base64.b64encode(response.content).decode("ascii")
    if "json" in content_type:
        try:
            payload = response.json()
        except ValueError:
            return ""
        return extract_image_b64(payload)
    return ""


class SceneObserver:
    def __init__(
        self,
        fetch_objects: Callable[[], Any] | None = None,
        capture_image: Callable[[str], Any] | None = None,
        ask_vlm: Callable[[str, list[tuple[str, str]]], str] | None = None,
        live_frame: Callable[[], tuple[str, str] | None] | None = None,
        backend_name: Callable[[], str] | str | None = None,
        vision_targets: list[tuple[str, str]] | Callable[[], list[tuple[str, str]]] | None = None,
        http_get: Callable[..., Any] | None = None,
        http_post: Callable[..., Any] | None = None,
    ) -> None:
        self._fetch = fetch_objects or self._default_fetch
        self._capture = capture_image
        self._ask_vlm = ask_vlm or QwenVLM().describe
        self._live_frame = live_frame
        self._backend_name = backend_name
        self._vision_targets = vision_targets
        self._http_get = http_get or requests.get
        self._http_post = http_post or requests.post

    @staticmethod
    def _default_fetch() -> Any:
        from robot_api.client import get_objects

        return get_objects()

    @staticmethod
    def _default_capture(camera_name: str) -> Any:
        from robot_api.client import capture_image

        return capture_image(camera_name=camera_name)

    def _targets(self) -> list[tuple[str, str]]:
        if callable(self._vision_targets):
            return list(self._vision_targets() or [])
        if self._vision_targets is not None:
            return list(self._vision_targets)
        return default_vision_targets()

    def _get_image(self, url: str, timeout: float) -> str:
        try:
            response = self._http_get(url, timeout=timeout)
        except Exception:
            return ""
        if hasattr(response, "ok"):
            return _response_image_b64(response)
        return extract_image_b64(response)

    def _screenshot(self, url: str, camera_name: str) -> str:
        try:
            response = self._http_post(
                f"{url}/screenshot",
                json={"camera_name": camera_name, "width": 640, "height": 480},
                timeout=90,
            )
        except Exception:
            return ""
        if hasattr(response, "ok"):
            return _response_image_b64(response)
        return extract_image_b64(response)

    def _server_up(self, url: str) -> bool:
        for path in ("/camera/status", "/health", "/camera/latest"):
            try:
                response = self._http_get(url + path, timeout=1.2)
            except requests.ConnectionError:
                return False
            except Exception:
                continue
            status = getattr(response, "status_code", None)
            if getattr(response, "ok", False) or status in {200, 404, 501}:
                return True
        return False

    def _pull_from_target(
        self, name: str, url: str, question: str
    ) -> tuple[list[tuple[str, str]], str, str]:
        if not self._server_up(url):
            return [], "", f"{name} 未启动"
        cameras = cameras_for(question)
        for camera in cameras:
            image = self._get_image(f"{url}/camera/latest?camera={camera}", timeout=8)
            if image:
                return [(camera, image)], f"{name} {camera}", ""
        for path in ("/camera/latest", "/camera/rgb", "/v1/camera/rgb"):
            image = self._get_image(url + path, timeout=8)
            if image:
                return [(path, image)], f"{name} {path}", ""
        for camera in cameras:
            image = self._screenshot(url, camera)
            if image:
                return [(camera, image)], f"{name} {camera}", ""
        extra = ""
        if name in {"vla", "dream"}:
            status = self._get_json(f"{url}/v1/camera/status", timeout=3) or self._get_json(
                f"{url}/camera/status", timeout=3
            )
            if isinstance(status, dict) and (
                status.get("ready") is True or status.get("streaming") is True
            ):
                extra = "相机在线，但没有闲时取帧接口（VLA 快照要动作完成后才能要）"
        return [], "", extra or f"{name} 无画面"

    def _get_json(self, url: str, timeout: float) -> Any:
        try:
            response = self._http_get(url, timeout=timeout)
        except Exception:
            return None
        if hasattr(response, "json"):
            try:
                if getattr(response, "ok", False):
                    return response.json()
            except ValueError:
                return None
        return response if isinstance(response, dict) else None

    def _collect_images(self, question: str) -> tuple[list[tuple[str, str]], str, str]:
        errors: list[str] = []
        for name, url in self._targets():
            images, source, err = self._pull_from_target(name, url, question)
            if images:
                return images, source, ""
            if err:
                errors.append(err)
        if self._live_frame is not None:
            try:
                live = self._live_frame()
            except Exception as exc:
                errors.append(f"live: {exc}")
                live = None
            if live:
                path, image = live
                return [(path, image)], path, ""
        if self._capture is not None:
            for camera in cameras_for(question):
                try:
                    result = self._capture(camera)
                except Exception as exc:
                    errors.append(f"{camera}: {exc}")
                    continue
                image = extract_image_b64(result)
                if image:
                    return [(camera, image)], camera, ""
                detail = ""
                if isinstance(result, dict):
                    detail = str(result.get("result") or "")
                errors.append(f"{camera}: {detail or '无图像'}")
        return [], "", "；".join(errors) or "无可用画面"

    def _describe(self, question: str = "") -> str:
        images, source, image_error = self._collect_images(question)
        vision = ""
        if images:
            try:
                vision = str(self._ask_vlm(question, images) or "").strip()
            except Exception as exc:
                image_error = f"拍到了画面但看图失败：{exc}"
        try:
            objects_text = format_objects(self._fetch())
        except Exception as exc:
            objects_text = f"读不到现场物体：{exc}"
        if vision:
            return compose_observe_answer(
                vision=vision, vision_source=source, objects_text=objects_text
            )
        return compose_observe_answer(
            vision_error=image_error, objects_text=objects_text
        )

    async def describe(self, text: str = "") -> str:
        return await asyncio.to_thread(self._describe, text)
