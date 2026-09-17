import asyncio
import re
import tempfile
import unittest
from pathlib import Path

from integrations.feishu.brain_client import (
    BrainOffline,
    BrainPreflight,
    BrainStatus,
    BrainTaskIdMismatch,
)
from integrations.feishu.bridge import FeishuBridge, IncomingCardAction, IncomingMessage
from integrations.feishu.config import Settings
from integrations.feishu.store import TaskStore


class FakeBrain:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or [{"active": False}])
        self.last_status = self.statuses[-1]
        self.published = []

    async def get_status(self):
        if self.statuses:
            self.last_status = self.statuses.pop(0)
        return BrainStatus(self.last_status)

    async def publish_task(self, task, task_id):
        self.published.append((task, task_id))
        return {"status": "success", "accepted": True, "task_id": task_id}

    async def task_preflight(self, task):
        return BrainPreflight({"ready": True, "required": False, "blockers": []})


class OfflineBrain(FakeBrain):
    async def get_status(self):
        raise BrainOffline("down")


class FakeMessenger:
    def __init__(self):
        self.texts = []
        self.cards = []
        self.updates = []

    async def send_text(self, chat_id, text, *, reply_to=None):
        self.texts.append((chat_id, text, reply_to))
        return f"text_{len(self.texts)}"

    async def send_card(self, chat_id, card, *, reply_to=None):
        message_id = f"card_{len(self.cards) + 1}"
        self.cards.append((message_id, chat_id, card, reply_to))
        return message_id

    async def update_card(self, message_id, card):
        self.updates.append((message_id, card))


def message(message_id, text, **overrides):
    values = dict(
        message_id=message_id,
        event_id=f"event_{message_id}",
        chat_id="chat_1",
        chat_type="p2p",
        sender_open_id="ou_sender",
        text=text,
        content_type="text",
    )
    values.update(overrides)
    return IncomingMessage(**values)


class FakeChat:
    def __init__(self, text="今天估计多云，这不是实时天气。"):
        self.text = text
        self.queries = []
        self.fail_with = None

    async def reply(self, text):
        self.queries.append(text)
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return self.text


class FakeRouter:
    def __init__(self, label="chat"):
        self.label = label
        self.queries = []
        self.fail_with = None

    async def route(self, text):
        self.queries.append(text)
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        return self.label


class FakeScene:
    def __init__(self, text="当前桌面/场景里有：\n• 可乐（cola_1）"):
        self.text = text
        self.queries = []

    async def describe(self, text):
        self.queries.append(text)
        return self.text


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.settings = Settings(
            app_id="cli_test",
            app_secret="secret",
            task_mode="active",
            brain_url="http://127.0.0.1:8888",
            confirm_timeout=30,
            task_timeout=5,
            poll_interval=0.01,
            db_path=root / "feishu.db",
            retention_days=30,
            http_timeout=1,
        )
        self.store = TaskStore(self.settings.db_path)
        self.messenger = FakeMessenger()
        self.bridges = []

    async def asyncTearDown(self):
        for bridge in self.bridges:
            await bridge.shutdown()
        self.tempdir.cleanup()

    def make_bridge(self, brain, settings=None, chat=None, scene=None, router=None):
        kwargs = dict(
            settings=settings or self.settings,
            store=self.store,
            brain=brain,
            messenger=self.messenger,
            chat=chat or FakeChat(),
            scene=scene or FakeScene(),
        )
        if router is not None:
            kwargs["router"] = router
        bridge = FeishuBridge(**kwargs)
        self.bridges.append(bridge)
        return bridge

    async def wait_for_state(self, message_id, expected, timeout=1):
        async def poll():
            while self.store.get(message_id).state != expected:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout=timeout)

    def test_brain_task_id_matches_project_contract(self):
        task_id = FeishuBridge._brain_task_id("om_复杂/message:id")
        self.assertEqual(len(task_id), 32)
        self.assertRegex(task_id, re.compile(r"^[A-Za-z0-9]+$"))
        self.assertEqual(task_id, FeishuBridge._brain_task_id("om_复杂/message:id"))

    async def test_read_only_task_submits_and_tracks_to_success(self):
        expected_id = FeishuBridge._brain_task_id("m1")
        brain = FakeBrain(
            [
                {"active": False},
                {
                    "active": True,
                    "task_id": expected_id,
                    "task": "查看机器人状态",
                    "all_done": False,
                    "failed": False,
                    "completed": 0,
                    "total": 1,
                    "subtask_list": [],
                },
                {
                    "active": True,
                    "task_id": expected_id,
                    "task": "查看机器人状态",
                    "all_done": True,
                    "failed": False,
                    "completed": 1,
                    "total": 1,
                    "subtask_list": [],
                },
            ]
        )
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("m1", "查看机器人状态"))
        await self.wait_for_state("m1", "succeeded")
        self.assertEqual(brain.published[0], ("查看机器人状态", expected_id))
        self.assertEqual(len(self.messenger.cards), 1)
        self.assertGreaterEqual(len(self.messenger.updates), 1)

    async def test_motion_task_requires_original_sender_confirmation(self):
        brain = FakeBrain([{"active": False}])
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("m2", "把海绵放到盘子上"))
        self.assertEqual(self.store.get("m2").state, "pending_confirmation")
        self.assertEqual(brain.published, [])

        await bridge.handle_card_action(
            IncomingCardAction(
                "card_1",
                "chat_1",
                "ou_other",
                {"action": "confirm", "message_id": "m2"},
            )
        )
        self.assertEqual(self.store.get("m2").state, "pending_confirmation")

        await bridge.handle_card_action(
            IncomingCardAction(
                "card_1",
                "chat_1",
                "ou_sender",
                {"action": "confirm", "message_id": "m2"},
            )
        )
        self.assertEqual(self.store.get("m2").state, "submitted")
        self.assertEqual(len(brain.published), 1)

    async def test_group_requires_mention_and_task_command(self):
        brain = FakeBrain()
        bridge = self.make_bridge(brain)
        await bridge.handle_message(
            message("g1", "/task 查看状态", chat_type="group", mentioned_bot=False)
        )
        await bridge.handle_message(
            message("g2", "查看状态", chat_type="group", mentioned_bot=True)
        )
        self.assertIsNone(self.store.get("g1"))
        self.assertIsNone(self.store.get("g2"))

    async def test_dry_run_never_publishes(self):
        dry_settings = Settings(**{**self.settings.__dict__, "task_mode": "dry_run"})
        brain = FakeBrain()
        bridge = self.make_bridge(brain, dry_settings)
        await bridge.handle_message(message("dry1", "移动到门口"))
        self.assertEqual(self.store.get("dry1").state, "pending_confirmation")
        await bridge.handle_card_action(
            IncomingCardAction(
                "card_1",
                "chat_1",
                "ou_sender",
                {"action": "confirm", "message_id": "dry1"},
            )
        )
        self.assertEqual(self.store.get("dry1").state, "dry_run")
        self.assertEqual(brain.published, [])

    async def test_busy_brain_rejects_without_publish(self):
        brain = FakeBrain(
            [
                {
                    "active": True,
                    "all_done": False,
                    "task": "已有任务",
                    "completed": 1,
                    "total": 3,
                }
            ]
        )
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("busy1", "查看机器人状态"))
        self.assertEqual(self.store.get("busy1").state, "brain_busy")
        self.assertEqual(brain.published, [])

    async def test_offline_brain_does_not_queue_or_publish(self):
        brain = OfflineBrain()
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("offline1", "查看机器人状态"))
        self.assertEqual(self.store.get("offline1").state, "brain_offline")
        self.assertEqual(brain.published, [])

    async def test_duplicate_message_is_ignored(self):
        dry_settings = Settings(**{**self.settings.__dict__, "task_mode": "dry_run"})
        brain = FakeBrain()
        bridge = self.make_bridge(brain, dry_settings)
        inbound = message("duplicate1", "查看机器人状态")
        await bridge.handle_message(inbound)
        await bridge.handle_message(inbound)
        self.assertEqual(len(self.messenger.cards), 1)
        self.assertEqual(brain.published, [])

    async def test_sender_can_cancel_pending_motion(self):
        brain = FakeBrain()
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("cancel1", "移动到门口"))
        await bridge.handle_card_action(
            IncomingCardAction(
                "card_1",
                "chat_1",
                "ou_sender",
                {"action": "cancel", "message_id": "cancel1"},
            )
        )
        self.assertEqual(self.store.get("cancel1").state, "canceled")
        self.assertEqual(brain.published, [])

    async def test_expired_confirmation_cannot_publish(self):
        brain = FakeBrain()
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("expired1", "移动到门口"))
        self.store.update("expired1", expires_at=1)
        await bridge.handle_card_action(
            IncomingCardAction(
                "card_1",
                "chat_1",
                "ou_sender",
                {"action": "confirm", "message_id": "expired1"},
            )
        )
        self.assertEqual(self.store.get("expired1").state, "expired")
        self.assertEqual(brain.published, [])

    async def test_changed_task_id_stops_tracking_as_superseded(self):
        brain = FakeBrain(
            [
                {"active": False},
                {
                    "active": True,
                    "task_id": "another_task",
                    "all_done": False,
                    "completed": 0,
                    "total": 1,
                },
            ]
        )
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("superseded1", "查看机器人状态"))
        await self.wait_for_state("superseded1", "superseded")
        self.assertEqual(len(brain.published), 1)

    async def test_required_preflight_blocks_motion_task(self):
        class BlockedBrain(FakeBrain):
            async def task_preflight(self, task):
                return BrainPreflight(
                    {
                        "ready": False,
                        "required": True,
                        "blockers": ["DREAM motion_ready=false"],
                        "recommended_tracking_timeout_sec": 6000,
                    }
                )

        brain = BlockedBrain([{"active": False}])
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("blocked1", "开始接待"))
        await bridge.handle_card_action(
            IncomingCardAction(
                "card_1",
                "chat_1",
                "ou_sender",
                {"action": "confirm", "message_id": "blocked1"},
            )
        )
        record = self.store.get("blocked1")
        self.assertEqual(record.state, "preflight_failed")
        self.assertEqual(record.tracking_timeout_sec, 6000)
        self.assertEqual(brain.published, [])

    async def test_publish_identity_mismatch_is_not_reported_as_execution_failure(self):
        class MismatchBrain(FakeBrain):
            async def publish_task(self, task, task_id):
                raise BrainTaskIdMismatch("returned another_task")

        brain = MismatchBrain([{"active": False}])
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("mismatch1", "查看机器人状态"))
        self.assertEqual(self.store.get("mismatch1").state, "superseded")

    async def test_chat_question_replies_without_publishing(self):
        chat = FakeChat("北京烤鸭值得一试。")
        brain = FakeBrain()
        bridge = self.make_bridge(brain, chat=chat)
        await bridge.handle_message(message("chat1", "北京有什么好吃的"))
        self.assertEqual(self.store.get("chat1").state, "succeeded")
        self.assertEqual(brain.published, [])
        self.assertEqual(chat.queries, ["北京有什么好吃的"])
        self.assertEqual(self.messenger.texts[0][1], "北京烤鸭值得一试。")
        self.assertEqual(self.messenger.cards, [])

    async def test_look_question_submits_to_brain(self):
        expected_id = FeishuBridge._brain_task_id("look1")
        brain = FakeBrain(
            [
                {"active": False},
                {
                    "active": True,
                    "task_id": expected_id,
                    "task": "桌上有什么",
                    "all_done": True,
                    "failed": False,
                    "completed": 1,
                    "total": 1,
                    "reasoning": "现场观察任务：拍照看图后描述视野里有什么，不移动、不抓取。",
                    "subtask_list": [
                        {
                            "subtask": "拍照查看：桌上有什么",
                            "done": True,
                            "status": "success",
                            "result": "视野描述（overhead_cam）：桌上有几个小球。",
                        }
                    ],
                },
            ]
        )
        bridge = self.make_bridge(brain)
        await bridge.handle_message(message("look1", "桌上有什么"))
        await self.wait_for_state("look1", "succeeded")
        self.assertEqual(brain.published[0], ("桌上有什么", expected_id))
        self.assertEqual(len(self.messenger.cards), 1)

    async def test_llm_routes_knowledge_question_to_chat(self):
        chat = FakeChat("北京市东城区天安门广场。")
        router = FakeRouter("chat")
        brain = FakeBrain()
        bridge = self.make_bridge(brain, chat=chat, router=router)
        await bridge.handle_message(message("where1", "天安门在哪里"))
        self.assertEqual(router.queries, ["天安门在哪里"])
        self.assertEqual(chat.queries, ["天安门在哪里"])
        self.assertEqual(self.store.get("where1").state, "succeeded")
        self.assertEqual(brain.published, [])
        self.assertEqual(self.messenger.cards, [])

    async def test_llm_route_failure_keeps_ambiguous_task(self):
        router = FakeRouter("chat")
        router.fail_with = "timeout"
        brain = FakeBrain()
        bridge = self.make_bridge(brain, router=router)
        await bridge.handle_message(message("where2", "天安门在哪里"))
        self.assertEqual(self.store.get("where2").state, "pending_confirmation")
        self.assertEqual(brain.published, [])

    async def test_motion_does_not_call_llm_router(self):
        router = FakeRouter("chat")
        brain = FakeBrain()
        bridge = self.make_bridge(brain, router=router)
        await bridge.handle_message(message("recv1", "开始接待"))
        self.assertEqual(router.queries, [])
        self.assertEqual(self.store.get("recv1").state, "pending_confirmation")


if __name__ == "__main__":
    unittest.main()
