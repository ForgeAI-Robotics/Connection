"""Read-only Tianji head frames using the existing G1 JPEG/freshness validator."""
import re

from brain.adapters.http_client import HttpClient, HttpContractError
from brain.adapters.vla_camera import VlaCameraClient, timestamp


class TianjiFrameHttp(HttpClient):
    def __init__(self, url, *, offline=False):
        super().__init__(url, timeout_sec=3, error_body_in_message=False)
        self.offline = offline

    def request_json(self, method, path, *args, **kwargs):
        if method != 'GET' or not path.startswith('/v1/camera/frames/latest?'):
            raise ValueError('天机头部取图只允许读取 latest 帧')
        value, status, headers = super().request_json(method, path, *args, **kwargs)
        if self.offline:
            if value.get('simulated') is not True or value.get('test_fixture') is not True:
                raise HttpContractError('离线相机要求明确的模拟测试帧')
        elif value.get('simulated') is True or value.get('test_fixture') is True:
            raise HttpContractError('真实头部取图拒绝模拟测试帧')
        if not all(isinstance(value.get(k), str) and 0 < len(value[k]) <= 256 for k in (
                'service_instance_id', 'camera_receiver_instance_id', 'robot_serial')):
            raise HttpContractError('头部图片缺少服务、相机接收实例或机器人身份')
        return value, status, headers


class TianjiHeadCamera:
    def __init__(self, base_url, *, token_env='TIANJI_VLA_TOKEN', offline=False):
        if not isinstance(token_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', token_env):
            raise ValueError('token_env 必须为凭据环境变量名')
        self.offline = offline is True
        self.http = TianjiFrameHttp(base_url, offline=self.offline)
        # Only credential routing is reused; never consult or change G1's active
        # observation profile, start cameras, or invoke an executor.
        config = {'reception_real': {'body_control': {'vla_token_env': token_env}}}
        self.camera = VlaCameraClient(base_url, http=self.http, config=config)

    def capture(self, *, captured_after=None, previous_frame_id=None, expected_robot_serial=None):
        after = timestamp(captured_after) if captured_after is not None else None
        if previous_frame_id is not None and (not isinstance(previous_frame_id, str) or not previous_frame_id):
            raise ValueError('previous_frame_id 必须为非空字符串')
        if expected_robot_serial is not None and (not isinstance(expected_robot_serial, str) or not expected_robot_serial):
            raise ValueError('expected_robot_serial 必须为非空字符串')
        metadata, raw = self.camera.latest('ego_view')
        if after is not None and timestamp(metadata['captured_at']) <= after:
            raise HttpContractError('图片采集时间没有晚于本次到站／观察边界')
        if previous_frame_id is not None and metadata['frame_id'] == previous_frame_id:
            raise HttpContractError('相机未提供新的源帧')
        if expected_robot_serial is not None and metadata['robot_serial'] != expected_robot_serial:
            raise HttpContractError('头部图片机器人身份与本次目标不符')
        if self.offline:
            metadata.update(simulated=True, test_fixture=True)
        return metadata, raw
