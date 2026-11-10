"""Authenticated, bounded VLA camera reads, including task-bound wrists. No motion API."""
import base64
from datetime import datetime, timezone
import hashlib
import io
import math
import time
from urllib.parse import urlencode
from PIL import Image

from brain.adapters.http_client import HttpClient, HttpContractError
from shared.execution_profile import applied_profile

FRAME_SCHEMA = 'fq/vla-camera-frame/v1'
BOUND_FRAME_SCHEMA = 'fq/vla-camera-frame/v2'
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_AGE_SEC = .5


def real_camera_route():
    profile = applied_profile()
    if not profile:
        return None
    route = profile['routes']['observation']
    if route.get('mode') != 'real':
        return None
    if (route.get('backend') != 'vla_camera' or route.get('available') is not True
            or not route.get('url')):
        raise ValueError(route.get('reason') or '真机观察路由未应用；拒绝使用其他相机')
    return dict(route)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('图片缺少源采集时间')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('图片时间缺少时区')
    return result


class VlaCameraClient:
    def __init__(self, url, *, config=None, http=None):
        self.http = http or HttpClient(url, timeout_sec=3, error_body_in_message=False)
        control = ((config or {}).get('reception_real') or {}).get('body_control') or {}
        self.http.auth_token_env = control.get('vla_token_env') or control.get('token_env') or 'BRAIN_CONTROL_TOKEN'

    def latest(self, camera='ego_view', *, task_id=None, command_id=None):
        if camera not in {'ego_view', 'left_wrist', 'right_wrist'}:
            raise ValueError('未知相机视角')
        if bool(task_id) != bool(command_id) or (camera != 'ego_view' and not command_id):
            raise ValueError('腕部取图必须绑定原任务和命令')
        params = {'camera': camera}
        if command_id:
            params.update(task_id=task_id, command_id=command_id)
        started = time.monotonic()
        frame = self.http.request_json('GET', '/v1/camera/frames/latest?' + urlencode(params),
            headers={'Cache-Control': 'no-cache'}, max_response_bytes=3 * 1024 * 1024)[0]
        elapsed = time.monotonic() - started
        try:
            if (frame.get('schema') != (BOUND_FRAME_SCHEMA if command_id else FRAME_SCHEMA) or frame.get('camera_key') != camera
                    or frame.get('content_type') != 'image/jpeg'):
                raise ValueError('头部图片合同不匹配')
            binding = frame.get('task_binding') or {}
            if command_id and (not isinstance(binding, dict) or binding.get('task_id') != task_id
                               or binding.get('command_id') != command_id or binding.get('operation') not in {'open_fridge','grasp_cola'}
                               or not all(isinstance(frame.get(k), str) and frame[k] for k in
                                          ('service_instance_id', 'camera_receiver_instance_id', 'robot_serial',
                                           'frame_receipt_id', 'binding_proof'))
                               or not all(isinstance(binding.get(k), str) and 0 < len(binding[k]) <= 256
                                          for k in ('execution_epoch', 'controller_instance_id'))):
                raise ValueError('图片任务绑定或服务身份不符')
            if not isinstance(frame.get('frame_id'), str) or not 0 < len(frame['frame_id']) <= 256:
                raise ValueError('图片缺少帧身份')
            captured, received = timestamp(frame.get('captured_at')), timestamp(frame.get('received_at'))
            now = datetime.now(timezone.utc)
            for key in ('age_ms', 'received_age_ms'):
                age = frame.get(key)
                if type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age / 1000 + elapsed <= MAX_AGE_SEC:
                    raise ValueError('头部图片已过期或新鲜度未知')
            if not -.05 <= (now - captured).total_seconds() <= MAX_AGE_SEC:
                raise ValueError('图片源采集时间过期或跨端时钟不一致')
            if not -.05 <= (now - received).total_seconds() <= MAX_AGE_SEC or captured > received:
                raise ValueError('图片接收时间无效或跨端时钟不一致')
            encoded = frame.get('image_base64')
            if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                raise ValueError('图片超过大小限制')
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > MAX_IMAGE_BYTES or hashlib.sha256(raw).hexdigest() != frame.get('sha256'):
                raise ValueError('图片校验失败')
            with Image.open(io.BytesIO(raw)) as image:
                if (image.format != 'JPEG' or image.width * image.height > 8_000_000
                        or any(type(frame.get(k)) is not int for k in ('width', 'height'))
                        or image.size != (frame['width'], frame['height'])):
                    raise ValueError('图片格式或尺寸不匹配')
                image.load()
        except (ValueError, TypeError, OverflowError, OSError, KeyError, Image.DecompressionBombError) as exc:
            raise HttpContractError('VLA 头部图片无效：' + str(exc)) from None
        metadata = {k: frame[k] for k in ('schema', 'camera_key', 'frame_id', 'captured_at',
                    'received_at', 'age_ms', 'received_age_ms', 'width', 'height', 'content_type', 'sha256')}
        metadata.update({k: frame[k] for k in ('task_binding', 'service_instance_id', 'robot_serial', 'camera_receiver_instance_id', 'frame_receipt_id', 'binding_proof', 'arm_return', 'arm_hold') if k in frame})
        return metadata, raw


def capture_head(*, config=None, route=None):
    route = real_camera_route() if route is None else route
    if not route:
        raise ValueError('未选择真机头部相机')
    return VlaCameraClient(route['url'], config=config).latest()
