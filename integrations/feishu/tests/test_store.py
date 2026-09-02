import sqlite3
import tempfile
import unittest
from pathlib import Path

from integrations.feishu.store import TaskStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tempdir.name) / "feishu.db")

    def tearDown(self):
        self.tempdir.cleanup()

    def create(self, message_id="m1", sender="ou_1", now=100):
        return self.store.create(
            message_id=message_id,
            event_id="e1",
            chat_id="c1",
            chat_type="p2p",
            sender_open_id=sender,
            task_text="查看状态",
            risk_level="read_only",
            now=now,
        )

    def test_message_id_is_deduplicated(self):
        self.assertTrue(self.create())
        self.assertFalse(self.create())

    def test_transition_is_compare_and_set(self):
        self.create()
        self.assertTrue(
            self.store.transition("m1", from_states={"received"}, to_state="submitted")
        )
        self.assertFalse(
            self.store.transition("m1", from_states={"received"}, to_state="failed")
        )
        self.assertEqual(self.store.get("m1").state, "submitted")

    def test_only_one_pending_confirmation_per_sender(self):
        self.create("m1", "ou_same")
        self.create("m2", "ou_same")
        self.assertTrue(
            self.store.transition(
                "m1", from_states={"received"}, to_state="pending_confirmation"
            )
        )
        self.assertFalse(
            self.store.transition(
                "m2", from_states={"received"}, to_state="pending_confirmation"
            )
        )

    def test_cleanup_deletes_old_terminal_records_only(self):
        self.create("old", now=10)
        self.store.transition(
            "old", from_states={"received"}, to_state="dry_run", now=20
        )
        self.create("open", now=10)
        self.store.transition(
            "open", from_states={"received"}, to_state="submitted", now=20
        )
        deleted = self.store.cleanup(30, now=31 * 86400)
        self.assertEqual(deleted, 1)
        self.assertIsNone(self.store.get("old"))
        self.assertIsNotNone(self.store.get("open"))

    def test_previous_database_schema_is_migrated(self):
        old_path = Path(self.tempdir.name) / "old_feishu.db"
        connection = sqlite3.connect(old_path)
        try:
            connection.execute(
                """
                CREATE TABLE messages (
                    message_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    sender_open_id TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    state TEXT NOT NULL,
                    brain_task_id TEXT,
                    card_message_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    confirmed_at INTEGER,
                    completed_at INTEGER,
                    last_error TEXT,
                    status_signature TEXT,
                    last_status_json TEXT
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        migrated = TaskStore(old_path)
        self.assertTrue(
            migrated.create(
                message_id="migrated",
                event_id="e",
                chat_id="c",
                chat_type="p2p",
                sender_open_id="ou",
                task_text="开始接待",
                risk_level="motion",
                tracking_timeout_sec=6000,
            )
        )
        self.assertEqual(migrated.get("migrated").tracking_timeout_sec, 6000)


if __name__ == "__main__":
    unittest.main()
