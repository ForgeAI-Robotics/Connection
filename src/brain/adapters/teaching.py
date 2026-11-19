"""One start snapshot and its finish manifest. Runtime owns the teaching task."""
import hashlib
import json
import os
from pathlib import Path

from brain.adapters.navigation_pose import NavigationPoseClient, is_number
from brain.adapters.vla_camera import VlaCameraClient, timestamp
from brain.storage.files import write_json
from brain.storage.tasks import now_iso
from contracts.tasks import CONTRACT_VERSION


def snapshot_sources(config):
    from shared.networks import lan
    return {'pose_url': lan().review_url(), 'camera_url': config['reception_real']['vla_base_url']}


class TeachingAdapter:
    contract_version = CONTRACT_VERSION

    def __init__(self, root, *, pose_url, camera_url, config=None):
        self.root = Path(root)
        self.sources = dict(pose_url=pose_url, camera_url=camera_url)
        self.pose = NavigationPoseClient(pose_url)
        self.camera = VlaCameraClient(camera_url, config=config)

    def _path(self, request):
        # Task identity, never the user-supplied demonstration name, selects a directory.
        directory = hashlib.sha256(request['task_id'].encode()).hexdigest()
        filename = {'teaching_start': 'start.json', 'teaching_finish': 'finish.json'}[request['skill']]
        return self.root / directory / filename

    def _expected(self, request):
        if request['parameters']['sop']['version'] != '2':
            raise ValueError('当前采集适配器只接受示教 SOP v2；不将旧元数据任务升级为真实采集')
        return {'task_id': request['task_id'], 'step_id': request['step_id'],
                'parameters': request['parameters'], 'source_text': request['body']['task'],
                'mode': 'start_snapshot', 'capture_status': 'captured', 'sources': self.sources}

    def _start(self, request):
        path = self._path(request).with_name('start.json')
        raw = path.read_bytes()
        start = json.loads(raw)
        expected = self._expected(request)
        if (any(start.get(k) != expected[k] for k in
                ('task_id', 'source_text', 'mode', 'capture_status', 'sources'))
                or any(start['parameters'][k] != request['parameters'][k]
                       for k in ('teaching', 'definition_sha256'))
                or start['step_id'] != 'TEACHING_STARTED'
                or start['parameters']['sop']['id'] != 'teaching.start'
                or start['parameters']['sop']['version'] != '2'):
            raise ValueError('示教快照与原任务、参数或数据源不匹配')
        pose, camera = start['pose'], start['camera']
        saved_at = timestamp(start['saved_at'])
        if (pose['frame_id'] != 'map' or not all(is_number(pose[k]) for k in ('x', 'y', 'yaw'))
                or not -.05 <= (saved_at - timestamp(pose['captured_at'])).total_seconds() <= 1):
            raise ValueError('保存的起始位姿无效或在采集时已过期')
        if (camera['path'] != 'head.jpg' or camera['camera_key'] != 'ego_view'
                or not -.05 <= (saved_at - timestamp(camera['captured_at'])).total_seconds() <= .5
                or hashlib.sha256(path.with_name('head.jpg').read_bytes()).hexdigest() != camera['sha256']):
            raise ValueError('保存的头部快照缺失、损坏或在采集时已过期')
        return start, hashlib.sha256(raw).hexdigest()

    def submit(self, command_id, request):
        path = self._path(request)
        expected = self._expected(request)
        if path.exists():
            return {'accepted': True}  # wait/query checks the original files; never recapture.
        if request['skill'] == 'teaching_start':
            if path.with_name('head.jpg').exists():
                raise ValueError('存在未完成的起始快照；请核对原文件，不自动重新采集')
            pose = self.pose.latest()
            camera, raw = self.camera.latest()
            saved_at = now_iso()
            if (timestamp(saved_at) - timestamp(pose['captured_at'])).total_seconds() > 1:
                raise ValueError('取图完成时起始位姿已过期，请重新发起示教')
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation preserves a partial capture if an earlier write was interrupted.
            with path.with_name('head.jpg').open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            write_json(path, dict(expected, pose=pose, camera=dict(camera, path='head.jpg'), saved_at=saved_at))
        else:
            start, digest = self._start(request)
            write_json(path, dict(expected, start_artifact='start.json', start_sha256=digest,
                                 saved_at=now_iso()))
        return {'accepted': True}

    def query(self, command_id, request):
        path = self._path(request)
        if not path.is_file():
            return {'command_id': command_id, 'terminal': '', 'evidence': {}}
        raw = path.read_bytes()
        saved = json.loads(raw)
        if any(saved.get(k) != v for k, v in self._expected(request).items()):
            raise ValueError('示教产物与本次步骤身份不匹配')
        start, digest = self._start(request)
        if (request['skill'] == 'teaching_finish'
                and (saved.get('start_artifact') != 'start.json' or saved.get('start_sha256') != digest
                     or timestamp(saved['saved_at']) < timestamp(start['saved_at']))):
            raise ValueError('结束归档与原始快照不匹配')
        return {'command_id': command_id, 'terminal': 'succeeded',
                'started': True, 'stopped': True, 'resources_released': True,
                'evidence': {'supports': True, 'identity_ok': True, 'time_ok': True,
                             'artifact': str(path), 'artifact_sha256': hashlib.sha256(raw).hexdigest(),
                             'capture_status': 'captured', 'mode': 'start_snapshot',
                             'pose': start['pose'],
                             'camera': dict(start['camera'], artifact=str(path.with_name('head.jpg')))}}

    def wait(self, command_id, request):
        return self.query(command_id, request)
