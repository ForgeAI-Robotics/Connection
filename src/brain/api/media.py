"""Selected-source media and task timelines live with the brain, not its web client."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
import requests
from brain.storage.files import write_json
from brain.workers import WorkerPool
from shared.paths import workspace_root


def observation_url():
    from shared.execution_profile import applied_profile
    profile = applied_profile()
    if profile:
        route = profile['routes']['observation']
        if not route.get('available') or not route.get('url'):
            raise ValueError(route.get('reason') or '观察模块不可用')
        return route['url'].rstrip('/')
    from execution.robot_api.config import load_robot_api_config
    config = load_robot_api_config()
    name = config.observation_backend or config.active_backend
    backend = next((b for b in config.backends if b.name == name), None)
    if backend is None or not backend.enabled or name == 'desk':
        raise ValueError('所选后端不提供图像；请显式配置观察模块')
    return backend.url.rstrip('/')


class MediaService:
    def __init__(self, task_status):
        self.root = workspace_root() / 'data/timelines'
        self.status = task_status
        self.pool = WorkerPool('brain-media', 1, capacity=8)
        self.dropped = 0
        self.errors = 0
        self.lock = threading.Lock()

    def event(self, event, record):
        if event not in {'task_opened', 'step_passed', 'task_succeeded', 'verdict'}:
            return
        try:
            url = observation_url()
            self.pool.submit(self.capture, event, record, url)
        except Exception:
            self.dropped += 1

    def capture(self, event, record, url):
        task_id = record['task_id']
        folder_name = hashlib.sha256(task_id.encode()).hexdigest()[:24]
        folder = self.root / folder_name
        manifest = folder / 'timeline.json'
        try:
            response = requests.get(url + '/camera/latest', timeout=15)
            response.raise_for_status()
            if not response.headers.get('content-type', '').startswith('image/'):
                raise ValueError('观察后端未返回图像')
            folder.mkdir(parents=True, exist_ok=True)
            previous = json.loads(manifest.read_text()) if manifest.exists() else {
                'task': record.get('task_desc'), 'task_id': task_id, 'won': None,
                'created': datetime.now().astimezone().isoformat(),
                'cameras': ['Top', 'Front', 'Wrist', 'Agent'], 'frames': []}
            filename = f'frame_{len(previous["frames"]):03d}.jpg'
            (folder / filename).write_bytes(response.content)
            previous['frames'].append({'file': folder_name + '/' + filename,
                'label': event + ': ' + str(record.get('phase') or ''), 't': (datetime.now().astimezone() - datetime.fromisoformat(previous['created'])).total_seconds(),
                'observed_at': datetime.now().astimezone().isoformat(), 'source': url})
            if record.get('state') in {'succeeded', 'failed', 'cancelled'}:
                previous['won'] = record['state'] == 'succeeded'
            write_json(manifest, previous)
            if self.status().get('task_id') == task_id:
                write_json(self.root / 'timeline.json', previous)
        except Exception:
            self.errors += 1

    def timeline(self):
        task_id = self.status().get('task_id')
        if task_id:
            key = hashlib.sha256(task_id.encode()).hexdigest()[:24]
            path = self.root / key / 'timeline.json'
        else:
            path = self.root / 'timeline.json'
        return {'exists': True, **json.loads(path.read_text())} if path.is_file() else {'exists': False, 'frames': [], 'task_id': task_id}

    def close(self, timeout=2):
        return self.pool.close(timeout)
