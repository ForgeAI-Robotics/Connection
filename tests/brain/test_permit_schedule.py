"""Control journal atomic encoding regression; no robot IO."""
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from brain.kernel.body_control import BodyControl
from brain.storage.body_control import ControlStore


class PermitScheduleTests(unittest.TestCase):
    def test_encoding_keeps_history_and_failed_write_keeps_original(self):
        with tempfile.TemporaryDirectory() as root:
            store = ControlStore(Path(root))
            value = dict(schema=2, epoch=14, leases={'old': {'state': 'released'}},
                         requests={'old': {'error': '原始失败', 'samples': list(range(100))}})
            store.write(value)
            before = store.path.read_bytes()
            self.assertEqual(store.read(), value)
            self.assertEqual(before.decode(), json.dumps(value, ensure_ascii=True, allow_nan=False))
            with self.assertRaises(ValueError):
                store.write(dict(value, invalid=float('nan')))
            self.assertEqual(store.path.read_bytes(), before)
