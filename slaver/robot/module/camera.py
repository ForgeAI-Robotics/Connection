"""
摄像头模块 - 多相机截图 + VLM 综合分析
"""

import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
load_dotenv(os.path.join(_project_root, '.env'))

import yaml

from robot_api.client import capture_image

# ============================================================
# 从 config.yaml 加载配置
# ============================================================

_config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml')
with open(_config_path, "r", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)
_camera_cfg = _cfg.get("camera", {})
_vlm_cfg = _camera_cfg.get("vlm", {})

_serve_camera_config_path = os.path.join(
    _project_root, "serve", "scene", "config", "camera.yaml"
)
_serve_camera_cfg = {}
if os.path.exists(_serve_camera_config_path):
    with open(_serve_camera_config_path, "r", encoding="utf-8") as _f:
        _serve_camera_cfg = yaml.safe_load(_f) or {}

CAMERAS = (
    (_serve_camera_cfg.get("preview") or {}).get("cameras")
    or list((_serve_camera_cfg.get("cameras") or {}).keys())
    or _camera_cfg.get("cameras")
    or ["overhead_cam", "head_cam", "right_arm_cam", "left_arm_cam"]
)
VLM_MODEL = _vlm_cfg.get("model", "qwen-vl-max")
VLM_API_BASE = _vlm_cfg.get("api_base", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
VLM_MAX_TOKENS = _vlm_cfg.get("max_tokens", 1000)
VLM_EXTRA_BODY = _vlm_cfg.get("extra_body") or {}  # GLM 关思考 {thinking:{type:disabled}};原样透传


# ============================================================
# VLM 调用
# ============================================================

def _call_vlm(images, context=""):
    """
    多图 VLM 分析

    Args:
        images: {camera_name: base64_str}
        context: 任务描述

    Returns:
        (status, description)
    """
    try:
        if context:
            prompt = f"""你是机器人场景监控系统。当前任务：{context}

以下是来自机器人不同视角的场景图片。
请综合所有图片信息，判断任务执行后场景是否符合预期。
- normal：场景状态符合任务预期
- abnormal：场景状态不符合预期、有异常

请先用几句话描述你观察到的场景，然后最后一行只回复 normal 或 abnormal。"""
        else:
            prompt = """你是机器人场景监控系统。

以下是来自机器人不同视角的场景图片。
请综合所有图片分析场景是否正常。
- normal：物体在预期位置，没有异常
- abnormal：物体位置不对、有障碍物、场景异常

请先用几句话描述你观察到的场景，然后最后一行只回复 normal 或 abnormal。"""

        content = [{"type": "text", "text": prompt}]
        for cam_name, b64 in images.items():
            content.append({"type": "text", "text": f"[{cam_name}]"})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })

        # VLM 用专用 key(阿里百炼 Qwen-VL-Max);没配就回退 CLOUD_API_KEY。master 规划另用 deepseek 的 CLOUD_API_KEY。
        api_key = os.environ.get("VLM_API_KEY") or os.environ.get("CLOUD_API_KEY", "")
        client = OpenAI(api_key=api_key, base_url=VLM_API_BASE)
        create_kw = dict(
            model=VLM_MODEL,
            messages=[{"role": "user", "content": content}],
            max_tokens=VLM_MAX_TOKENS,
            temperature=0,
        )
        if VLM_EXTRA_BODY:
            create_kw["extra_body"] = VLM_EXTRA_BODY
        response = client.chat.completions.create(**create_kw)
        raw_content = response.choices[0].message.content
        result = raw_content.strip() if raw_content else ""
        print(f"[camera] VLM 原始输出: {repr(raw_content)}", file=sys.stderr)

        if not result:
            return "normal", "VLM 返回空内容，无法判断场景状态"

        lines = result.split("\n")
        status_line = lines[-1].strip().lower()
        status = "abnormal" if "abnormal" in status_line else "normal"
        description = "\n".join(lines[:-1]).strip() if len(lines) > 1 else result
        print(f"[camera] VLM 结果: {status}, 描述: {description}", file=sys.stderr)
        return status, description
    except Exception as e:
        print(f"[camera] VLM 调用失败: {e}", file=sys.stderr)
        return "normal", f"VLM 调用失败: {e}"


# ============================================================
# MCP 工具注册
# ============================================================

def register_tools(mcp):

    @mcp.tool()
    async def capture_image(context: str = "") -> str:
        """拍照：从机器人多个相机捕获当前场景，用 VLM 综合分析。
        注意：每个任务最多拍照1次。拍照后必须立即执行具体操作（导航/抓取/放置），不要连续拍照。

        Args:
            context: 当前任务描述，用于 VLM 判断场景是否正常。例如："正在抓取马克杯"、"已导航到水槽附近"

        Returns:
            截图结果和场景分析。
        """
        print(f"[camera] 拍照请求 (context: {context})", file=sys.stderr)

        images = {}
        for cam in CAMERAS:
            result = capture_image(camera_name=cam)
            if result.get("success"):
                images[cam] = result["image"]
            else:
                print(f"[camera] {cam} 截图失败: {result.get('result', '')}", file=sys.stderr)

        if not images:
            msg = "截图失败：所有相机均不可用"
            print(f"[camera] {msg}", file=sys.stderr)
            return json.dumps([msg, {"_status": "failure"}])

        n_cams = len(images)
        print(f"[camera] VLM 分析: {n_cams} 个相机", file=sys.stderr)
        scene_status, vlm_description = _call_vlm(images, context)

        cam_list = "、".join(images.keys())
        response = (
            f"截图成功（{cam_list}），"
            f"VLM 判断: {scene_status}，描述: {vlm_description}"
        )
        return json.dumps([response, {"_status": "none"}])

    print(f"[camera.py] 摄像头模块已注册 ({len(CAMERAS)} 相机 VLM): {CAMERAS}", file=sys.stderr)
