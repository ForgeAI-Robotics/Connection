import unittest

from integrations.feishu.cards import confirmation_card, task_card
from integrations.feishu.bridge import FeishuBridge
from integrations.feishu.sdk_compat import enable_websocket_card_callbacks
from integrations.feishu.store import TaskRecord


class CardsAndSdkTests(unittest.TestCase):
    def test_confirmation_card_contains_bound_actions(self):
        record = TaskRecord(
            message_id="m1",
            event_id="e1",
            chat_id="c1",
            chat_type="p2p",
            sender_open_id="ou_1",
            task_text="移动到门口",
            risk_level="motion",
            state="pending_confirmation",
            brain_task_id=None,
            card_message_id=None,
            created_at=1,
            updated_at=1,
            expires_at=121,
            tracking_timeout_sec=None,
            confirmed_at=None,
            completed_at=None,
            last_error=None,
            status_signature=None,
            last_status_json=None,
        )
        card = confirmation_card(record, 120)
        columns = card["body"]["elements"][-1]["columns"]
        values = [column["elements"][0]["value"] for column in columns]
        self.assertEqual(values[0], {"action": "confirm", "message_id": "m1"})
        self.assertEqual(values[1], {"action": "cancel", "message_id": "m1"})

    def test_websocket_card_patch_is_idempotent(self):
        first = enable_websocket_card_callbacks()
        second = enable_websocket_card_callbacks()
        self.assertIn(first, {True, False})
        self.assertFalse(second)

    def test_reception_state_changes_card_and_tracking_signature(self):
        record = TaskRecord(
            message_id="m2",
            event_id="e2",
            chat_id="c1",
            chat_type="p2p",
            sender_open_id="ou_1",
            task_text="开始接待",
            risk_level="motion",
            state="running",
            brain_task_id="feishu0123456789abcdef01234567",
            card_message_id="card_1",
            created_at=1,
            updated_at=2,
            expires_at=None,
            tracking_timeout_sec=6000,
            confirmed_at=1,
            completed_at=None,
            last_error=None,
            status_signature=None,
            last_status_json=None,
        )
        status = {
            "active": True,
            "task_id": record.brain_task_id,
            "all_done": False,
            "completed": 1,
            "total": 2,
            "subtask_list": [],
            "reception_state": {
                "state": "RUNNING",
                "runtime_phase": "NAVIGATING_TO_TABLE2",
                "verified_state": "INITIALIZED",
                "holding": None,
                "object_location": "table_2",
                "commands": {"table2": {"command_id": "nav-table2-abcdef012345"}},
                "remote_states": {
                    "nav-table2-abcdef012345": {
                        "service": "dream",
                        "state": "navigating",
                        "updated_at": "ignored-in-signature",
                    }
                },
            },
        }
        rendered = str(task_card(record, status))
        self.assertIn("NAVIGATING_TO_TABLE2", rendered)
        self.assertIn("nav-table2-abcdef012345", rendered)
        first = FeishuBridge._status_signature(status)
        status["reception_state"]["runtime_phase"] = "VLA_PICKING"
        second = FeishuBridge._status_signature(status)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
