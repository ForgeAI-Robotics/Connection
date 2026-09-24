"""Compatibility of existing HTTP fields around the independent application."""
from contracts.tasks import KernelError
from brain.storage.tasks import load_existing
from brain.service_support import runtime_dir


class HttpFacade:
    def __init__(self, application):
        self.application = application
        self.config = application.config
        self._pending_success = self._pending_failure = None

    @property
    def current_task_id(self):
        return self.application.status().get('task_id')

    @property
    def _exploration_rate(self):
        return self.application.service.experiences.exploration_rate

    def get_task_status(self):
        return self.application.status()

    def get_task_preflight(self, task):
        return {'ready': True, 'required': False, 'task_type': 'runtime', 'blockers': []}

    def publish_global_task(self, task, refresh, task_id, force_new_task=False, resume=False, *, options=None):
        try:
            return self.application.publish(task, task_id, resume=resume, options=options)
        except (KernelError, ValueError) as exc:
            return {'ignored': True, 'error': str(exc), 'reasoning_explanation': str(exc),
                    'subtask_list': [], 'blocks_new_motion': True}

    def control(self, action):
        try:
            return self.application.control(action)
        except (KernelError, ValueError) as exc:
            return {'accepted': False, 'error': str(exc)}

    def kernel_pause(self):
        return self.control('pause')

    def kernel_continue(self):
        return self.control('continue')

    def kernel_cancel(self):
        return self.control('cancel')

    def _record(self, task_id=None):
        record = load_existing(runtime_dir(self.config)) or {}
        if task_id and record.get('task_id') != task_id:
            raise ValueError('经验 task_id 与记录不符')
        return record

    def classify_and_save_success_experience(self, note):
        return self.application.service.experiences.save(note, 'positive', self._record(), classify=True)

    def classify_and_save_failure_experience(self, note):
        return self.application.service.experiences.save(note, 'negative', self._record(), classify=True)

    def save_experience(self, task_id='', exp_type=None, note=''):
        return self.application.service.experiences.save(note, exp_type, self._record(task_id))

    def get_experiences(self):
        return self.application.service.experiences.read()
