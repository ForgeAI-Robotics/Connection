"""Bounded demonstration extraction; saved artifacts are unpublished candidates."""
import json
from pathlib import Path
import threading
import uuid
from werkzeug.utils import secure_filename
from brain.workers import WorkerPool
from brain.storage.files import write_json


class DemoLearning:
    def __init__(self, root, processor=None):
        self.root = Path(root)
        self.pool = WorkerPool('brain-demo', 1, capacity=1)
        self.lock = threading.Lock()
        self.running = False
        self.processor = processor or self._extract

    @staticmethod
    def _extract(path, task):
        from brain.learning.video import video_to_actions
        from brain.learning.demo_extract import learn_from_demo
        demo = video_to_actions(str(path), hint=task)
        specific, rules = learn_from_demo(demo, write=False, context=task)
        return {**demo, 'task_specific': specific, 'global_rules': rules}

    def submit(self, upload, task):
        with self.lock:
            if self.running:
                raise ValueError('已有示教作业正在解析')
            job_id = uuid.uuid4().hex
            path = self.root / 'uploads' / (job_id + '_' + (secure_filename(upload.filename) or 'video'))
            path.parent.mkdir(parents=True, exist_ok=True)
            upload.save(str(path))
            self.running = True
            write_json(self.root / 'demo_teach_status.json', {'job_id': job_id, 'running': True,
                'done': False, 'phase': '解析中', 'task': task})
            try:
                self.pool.submit(self._run, job_id, path, task)
            except Exception:
                self.running = False
                raise
            return {'success': True, 'started': True, 'job_id': job_id, 'video': path.name, 'task': task}

    def _run(self, job_id, path, task):
        status = {'job_id': job_id, 'running': False, 'done': True, 'phase': '完成', 'task': task}
        try:
            result = {**self.processor(path, task), 'job_id': job_id, 'task': task, 'video': path.name}
            write_json(self.root / 'demo_teach_result.json', result)
        except Exception as exc:
            status.update(phase='出错', detail=str(exc), error=True)
        finally:
            with self.lock:
                write_json(self.root / 'demo_teach_status.json', status)
                self.running = False

    def status(self):
        path = self.root / 'demo_teach_status.json'
        result = json.loads(path.read_text()) if path.exists() else {'running': False, 'done': False, 'phase': '空闲'}
        if result.get('running') and not self.running:
            result.update(running=False, done=True, error=True, phase='已中断', detail='解析进程已停止；请重新提交视频')
        return result

    def result(self):
        path = self.root / 'demo_teach_result.json'
        result = json.loads(path.read_text()) if path.exists() else None
        return result if result and result.get('job_id') == self.status().get('job_id') else None

    def save(self, job_id=None):
        with self.lock:
            result = self.result()
            if self.running or not result or (job_id and result.get('job_id') != job_id):
                raise ValueError('没有对应的已完成示教结果')
            if not result.get('job_id'):
                raise ValueError('旧结果缺少作业标识，请重新解析后确认')
            write_json(self.root / 'demonstration_candidates' / (result['job_id'] + '.json'),
                       {**result, 'status': 'unevaluated'})
            return {'success': True, 'status': 'unevaluated', 'job_id': result['job_id'],
                    'task_specific': result.get('task_specific', []), 'global_rules': result.get('global_rules', [])}

    def close(self, timeout=2):
        return self.pool.close(timeout)
