import unittest

from entries.feishu.cards import confirmation_card, task_card
from entries.feishu.bridge import FeishuBridge
from entries.feishu.sdk_compat import enable_websocket_card_callbacks
from entries.feishu.store import TaskRecord


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

    def test_manual_skip_is_not_rendered_as_success(self):
        from types import SimpleNamespace
        record = SimpleNamespace(state='failed', task_text='接待', brain_task_id='t', last_error=None,
                                 created_at=1, updated_at=2)
        rendered = str(task_card(record, {'state': 'failed', 'flow_finished': True,
            'manual_skips': [{'step_id': 'nav'}], 'subtask_list': [
                {'subtask': '导航', 'done': True, 'status': 'skipped', 'result': '人工跳过'}]}))
        self.assertIn('⏭', rendered)
        self.assertNotIn('✅', rendered)
        self.assertIn('未全部成功', rendered)

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

    def test_task_card_shows_local_answer(self):
        record = TaskRecord(
            message_id="m3",
            event_id="e3",
            chat_id="c1",
            chat_type="p2p",
            sender_open_id="ou_1",
            task_text="桌上有什么",
            risk_level="read_only",
            state="succeeded",
            brain_task_id=None,
            card_message_id="card_2",
            created_at=1,
            updated_at=2,
            expires_at=None,
            tracking_timeout_sec=None,
            confirmed_at=None,
            completed_at=2,
            last_error=None,
            status_signature=None,
            last_status_json=None,
        )
        rendered = str(
            task_card(
                record,
                {"answer": "当前桌面/场景里有：\n• 可乐（cola_1）", "source": "observe"},
            )
        )
        self.assertIn("可乐（cola_1）", rendered)
        self.assertIn("回答：", rendered)

    def test_task_card_shows_look_result(self):
        record = TaskRecord(
            message_id="m4",
            event_id="e4",
            chat_id="c1",
            chat_type="p2p",
            sender_open_id="ou_1",
            task_text="桌上有什么",
            risk_level="read_only",
            state="succeeded",
            brain_task_id="feishu0123456789abcdef01234567",
            card_message_id="card_3",
            created_at=1,
            updated_at=2,
            expires_at=None,
            tracking_timeout_sec=None,
            confirmed_at=None,
            completed_at=2,
            last_error=None,
            status_signature=None,
            last_status_json=None,
        )
        rendered = str(
            task_card(
                record,
                {
                    "subtask_list": [
                        {
                            "subtask": "拍照查看：桌上有什么",
                            "done": True,
                            "status": "success",
                            "result": "视野描述（overhead_cam）：桌上有几个小球。",
                        }
                    ]
                },
            )
        )
        self.assertIn("视野描述", rendered)
        self.assertIn("回答：", rendered)

    def test_task_card_shows_reflection(self):
        record = TaskRecord(
            message_id="m5",
            event_id="e5",
            chat_id="c1",
            chat_type="p2p",
            sender_open_id="ou_1",
            task_text="开始接待",
            risk_level="motion",
            state="succeeded",
            brain_task_id="feishu0123456789abcdef01234567",
            card_message_id="card_4",
            created_at=1,
            updated_at=2,
            expires_at=None,
            tracking_timeout_sec=None,
            confirmed_at=None,
            completed_at=2,
            last_error=None,
            status_signature=None,
            last_status_json=None,
        )
        status = {
            "active": True,
            "all_done": True,
            "reflection": {
                "summary": "顺利完成：记录的步骤均成功结束 （模板档）",
                "source": "模板档",
                "final": "success",
            },
        }
        rendered = str(task_card(record, status))
        self.assertIn("复盘：", rendered)
        self.assertIn("顺利完成", rendered)
        first = FeishuBridge._status_signature(status)
        status["reflection"]["summary"] = "抓取失败：夹爪未夹住 （模板档）"
        second = FeishuBridge._status_signature(status)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
