"""Durable, idempotent reflection jobs consume immutable task snapshots."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from contracts.episode import episode_from_steps
from brain.storage.files import write_json
from brain.storage.tasks import KernelStore


def episode_from_record(record):
    steps = []
    for step_id in record.get('phase_order') or []:
        for attempt in (record.get('steps', {}).get(step_id) or {}).get('attempts') or []:
            verdict = attempt.get('verdict')
            progress = attempt.get('progress') or {}
            evidence = progress.get('evidence') or {}
            contract = attempt.get('contract') or {}
            downstream = evidence.get('downstream') or {}
            result = downstream.get('result') or {}
            steps.append({'phase': step_id, 'attempt_id': attempt['attempt_id'],
                'command_id': attempt['command_id'], 'status': verdict or 'unknown',
                'verify_ok': True if verdict == 'PASS' else False if verdict == 'FAIL' else None,
                'claimed_ok': evidence.get('reported_success', evidence.get('claimed_ok')),
                'detail': evidence.get('detail') or progress.get('error')
                          or (downstream.get('error') or {}).get('message') or result.get('message') or '',
                'skill': contract.get('skill'), 'goal': contract.get('goal'),
                'object_id': contract.get('object_id'),
                'evidence': {key: deepcopy(evidence.get(key)) for key in
                             ('grade', 'effect', 'effect_verified', 'identity_ok', 'time_ok')},
                'evidence_source': (result.get('evidence') or {}).get('source'),
                'downstream_error': deepcopy(downstream.get('error'))})
    final = 'success' if record['state'] == 'succeeded' else 'recovery' if record['state'] == 'recovery_required' else 'failure'
    episode = episode_from_steps(record['task_id'], record.get('task_desc', ''), steps,
        task_type=record['package'], backend=record.get('execution_backend'),
        final=final, error=record.get('blocked_reason'))
    episode['record_revision'] = record.get('revision')
    episode['release_id'] = record.get('release_id')
    episode['selection'] = deepcopy(record.get('selection'))
    episode['manual_skips'] = deepcopy(record.get('manual_skips') or [])
    episode['flow_finished'] = bool(record.get('flow_finished'))
    episode['established_facts'] = [deepcopy(item) for item in record.get('observations') or []
                                    if item.get('kind') == 'established']
    episode['existing_rules'] = deepcopy(record.get('release_rules') or [])
    return episode


class ReflectionWorker:
    def __init__(self, root, config, processor=None, emit=None):
        self.root, self.config = Path(root), deepcopy(config)
        self.processor, self.emit = processor, emit
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.last_error = None
        self.failed = set()

    def enqueue(self, record):
        if not record.get('reflection_enabled', True) or (self.config.get('reflection') or {}).get('enabled') is False:
            return None
        snapshot = deepcopy(record)
        identity = f'{snapshot["task_id"]}:{snapshot["revision"]}'
        key = hashlib.sha256(identity.encode()).hexdigest()
        path = self.root / (key + '.json')
        with self.lock:
            if not path.exists():
                write_json(path, {'job_id': key, 'state': 'pending', 'record': snapshot})
        self.wakeup.set()
        return key

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='brain-reflection', daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            for path in sorted(self.root.glob('*.json')):
                if self.stop.is_set():
                    break
                job = {}
                try:
                    job = json.loads(path.read_text())
                    if job['state'] != 'pending' or job['job_id'] in self.failed:
                        continue
                    episode = episode_from_record(job['record'])
                    episode['_work_id'] = job['job_id']
                    if self.processor is None:
                        from brain.learning.reflection import maybe_reflect
                        result = maybe_reflect(episode, config=self.config, quiet=True, write_sop=False)
                    else:
                        result = self.processor(episode)
                    job.update(state='completed', result=result)
                    write_json(path, job)
                    if self.emit:
                        self.emit('reflection_completed', job['record'], result=result)
                except Exception as exc:
                    self.last_error = str(exc)
                    self.failed.add(job.get("job_id"))
                    # Retain the pending input; retry after restart or the next wakeup.
                    if self.emit:
                        self.emit('reflection_failed', job.get('record') or {}, error=str(exc))
            self.wakeup.wait(1)
            self.wakeup.clear()

    def result(self, task_id):
        matches = []
        for path in self.root.glob('*.json'):
            job = json.loads(path.read_text())
            if job['record']['task_id'] == task_id and job['state'] == 'completed':
                matches.append(job)
        latest = max(matches, key=lambda job: job['record']['revision'], default=None)
        return latest.get('result') if latest else None

    def close(self, timeout=2):
        self.stop.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(timeout)
        return self.thread is None or not self.thread.is_alive()
