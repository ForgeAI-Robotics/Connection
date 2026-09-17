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

from robot_api.client import capture_image as backend_capture_image
from robot_api.look import cameras_for, is_look_task

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


def _is_describe_request(context=""):
    text = str(context or "")
    return is_look_task(text) or "拍照查看" in text


def _call_vlm(images, context=""):
    """多图 VLM 分析。观察任务描述视野，其它任务判 normal/abnormal。"""
    try:
        if _is_describe_request(context):
            prompt = f"""你是机器人现场观察助手。用户问：{context or "前面/桌上有什么"}

下面是机器人当前相机画面。根据图片如实描述视野里有什么。
不要编造看不见的东西，不要说正在控制机器人。
用简体中文：先一句总述，再列看到的物体。"""
        elif context:
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

        if _is_describe_request(context):
            print(f"[camera] VLM 观察: {result}", file=sys.stderr)
            return "described", result

        lines = result.split("\n")
        status_line = lines[-1].strip().lower()
        status = "abnormal" if "abnormal" in status_line else "normal"
        description = "\n".join(lines[:-1]).strip() if len(lines) > 1 else result
        print(f"[camera] VLM 结果: {status}, 描述: {description}", file=sys.stderr)
        return status, description
    except Exception as e:
        print(f"[camera] VLM 调用失败: {e}", file=sys.stderr)
        return "normal", f"VLM 调用失败: {e}"


def register_tools(mcp):

    @mcp.tool()
    async def capture_image(context: str = "", camera_name: str = "") -> str:
        """拍照：截取当前相机画面。
        现场观察任务（桌上/前面有什么、拍照查看）用 VLM 描述视野；其它任务用于诊断场景是否正常。
        每个子任务最多拍照1次。

        Args:
            context: 当前任务描述。观察任务请传入原问句，例如「桌上有什么」。
            camera_name: 可选。指定单个相机名；留空则按任务选俯视或前方相机。

        Returns:
            截图结果和场景描述。
        """
        print(f"[camera] 拍照请求 (context: {context}, camera_name: {camera_name})", file=sys.stderr)

        requested = str(camera_name or "").strip()
        if requested:
            cams = [requested]
        elif _is_describe_request(context):
            cams = cameras_for(context)[:2]
        else:
            cams = list(CAMERAS)
        images = {}
        for cam in cams:
            result = backend_capture_image(camera_name=cam)
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
        if scene_status == "described":
            response = f"视野描述（{cam_list}）：{vlm_description}"
            return json.dumps([response, {"_status": "success"}])
        response = (
            f"截图成功（{cam_list}），"
            f"VLM 判断: {scene_status}，描述: {vlm_description}"
        )
        return json.dumps([response, {"_status": "none"}])

    print(f"[camera.py] 摄像头模块已注册 ({len(CAMERAS)} 相机 VLM): {CAMERAS}", file=sys.stderr)
