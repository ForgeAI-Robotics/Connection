import unittest

import requests

from integrations.feishu.brain_client import (
    BrainBusy,
    BrainClient,
    BrainOffline,
    BrainRejected,
    BrainTaskIdMismatch,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=False):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error
        self.ok = 200 <= status_code < 400

    def json(self):
        if self.json_error:
            raise ValueError("bad json")
        return self.payload


class FakeSession:
    def __init__(self, get_response=None, post_response=None, error=None):
        self.get_response = get_response
        self.post_response = post_response
        self.error = error
        self.last_post = None

    def get(self, url, timeout):
        if self.error:
            raise self.error
        return self.get_response

    def post(self, url, json, timeout):
        if self.error:
            raise self.error
        self.last_post = (url, json, timeout)
        return self.post_response


class BrainClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_busy_contract(self):
        session = FakeSession(
            get_response=FakeResponse(payload={"active": True, "all_done": False})
        )
        status = await BrainClient(
            "http://127.0.0.1:8888", session=session
        ).get_status()
        self.assertTrue(status.busy)

    async def test_publish_preserves_external_task_id(self):
        session = FakeSession(
            post_response=FakeResponse(
                payload={
                    "status": "success",
                    "accepted": True,
                    "task_id": "feishu_m1",
                }
            )
        )
        await BrainClient("http://127.0.0.1:8888", session=session).publish_task(
            "查看状态", "feishu_m1"
        )
        self.assertEqual(session.last_post[1]["task_id"], "feishu_m1")
        self.assertIs(session.last_post[1]["refresh"], True)

    async def test_publish_rejects_mismatched_task_id(self):
        session = FakeSession(
            post_response=FakeResponse(
                payload={
                    "status": "success",
                    "accepted": True,
                    "task_id": "another_task",
                }
            )
        )
        with self.assertRaises(BrainTaskIdMismatch):
            await BrainClient("http://127.0.0.1:8888", session=session).publish_task(
                "查看状态", "feishu_m1"
            )

    async def test_publish_surfaces_not_accepted_race_as_busy(self):
        session = FakeSession(
            post_response=FakeResponse(
                payload={
                    "status": "success",
                    "accepted": False,
                    "task_id": "existing_task",
                    "message": "Task was not accepted",
                }
            )
        )
        with self.assertRaises(BrainBusy):
            await BrainClient("http://127.0.0.1:8888", session=session).publish_task(
                "开始接待", "feishu_m1"
            )

    async def test_task_preflight_contract(self):
        session = FakeSession(
            post_response=FakeResponse(
                payload={
                    "ready": False,
                    "required": True,
                    "blockers": ["DREAM motion_ready=false"],
                    "recommended_tracking_timeout_sec": 6000,
                }
            )
        )
        result = await BrainClient(
            "http://127.0.0.1:8888", session=session
        ).task_preflight("开始接待")
        self.assertFalse(result.ready)
        self.assertTrue(result.required)
        self.assertEqual(result.recommended_tracking_timeout_sec, 6000)

    async def test_connection_error_is_offline(self):
        session = FakeSession(error=requests.ConnectionError("down"))
        with self.assertRaises(BrainOffline):
            await BrainClient("http://127.0.0.1:8888", session=session).get_status()

    async def test_bad_json_is_rejected(self):
        session = FakeSession(get_response=FakeResponse(json_error=True))
        with self.assertRaises(BrainRejected):
            await BrainClient("http://127.0.0.1:8888", session=session).get_status()


if __name__ == "__main__":
    unittest.main()
