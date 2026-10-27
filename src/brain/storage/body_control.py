"""Resource ledger sharing the Runtime directory lock; never a second task ledger."""
import json
import os
from pathlib import Path
from brain.storage.tasks import KernelStore


class ControlStore:
    def __init__(self, root):
        self.tasks = KernelStore(root)
        self.path = Path(root) / 'body_control.json'
        self.writer = self.tasks.writer

    def read(self):
        if not self.path.exists():
            return None
        with self.path.open(encoding='utf-8') as stream:
            state = json.load(stream)
        if (not isinstance(state, dict) or state.get('schema') != 1
                or type(state.get('epoch')) is not int or state['epoch'] < 0
                or not isinstance(state.get('leases'), dict) or not isinstance(state.get('requests'), dict)):
            raise ValueError('Invalid body control ledger; refusing to reset it')
        return state

    def write(self, value):
        tmp = self.path.with_suffix('.json.tmp')
        with tmp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
