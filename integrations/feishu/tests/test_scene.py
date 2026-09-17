import unittest

from integrations.feishu.scene import (
    SceneObserver,
    cameras_for,
    compose_observe_answer,
    extract_image_b64,
    format_objects,
)


class SceneFormatTests(unittest.TestCase):
    def test_desk_objects_are_listed_in_chinese(self):
        text = format_objects(
            {
                "cola_1": {"pos": [0.1, 0.2, 0.7], "grasped": False, "category": "cola"},
                "milk_1": {"pos": [0.2, 0.1, 0.7], "grasped": True, "category": "milk"},
            }
        )
        self.assertIn("可乐（cola_1）", text)
        self.assertIn("牛奶（milk_1）（机器人手中）", text)
        self.assertTrue(text.startswith("当前桌面/场景里有："))

    def test_backend_error_is_not_an_object_list(self):
        text = format_objects({"success": False, "result": "desk 连接失败"})
        self.assertEqual(text, "读不到现场物体：desk 连接失败")

    def test_empty_world_is_explicit(self):
        self.assertEqual(format_objects({}), "当前场景里没有读到物体。")


class SceneObserveTests(unittest.TestCase):
    def test_front_question_prefers_head_camera(self):
        self.assertEqual(cameras_for("前面有什么")[0], "head_cam")

    def test_desk_question_prefers_overhead_camera(self):
        self.assertEqual(cameras_for("桌上有什么")[0], "overhead_cam")

    def test_extract_image_ignores_failed_capture(self):
        self.assertEqual(
            extract_image_b64({"success": False, "result": "desk mock 无渲染"}),
            "",
        )

    def test_compose_vision_answer_does_not_pretend_to_be_object_list(self):
        text = compose_observe_answer(
            vision="视野里有一罐可乐。",
            vision_source="mujoco head_cam",
            objects_text="当前桌面/场景里有：\n• 可乐（cola_1）",
        )
        self.assertIn("视野里有一罐可乐", text)
        self.assertIn("mujoco head_cam 看图", text)

    def test_compose_falls_back_to_objects_and_says_not_a_photo(self):
        text = compose_observe_answer(
            vision_error="desk mock 无渲染",
            objects_text="当前桌面/场景里有：\n• 可乐（cola_1）",
        )
        self.assertIn("可乐（cola_1）", text)
        self.assertIn("不是相机实拍", text)

    def test_desk_backend_skips_camera_and_uses_object_list(self):
        captured = []

        def capture(camera_name):
            captured.append(camera_name)
            return {"success": True, "image": "abc"}

        def ask_vlm(question, images):
            raise AssertionError("desk 不应看图")

        observer = SceneObserver(
            fetch_objects=lambda: {
                "cola_1": {"pos": [0, 0, 0], "grasped": False, "category": "cola"}
            },
            ask_vlm=ask_vlm,
            vision_targets=[],
        )
        answer = observer._describe("前面有什么")
        self.assertEqual(captured, [])
        self.assertIn("可乐（cola_1）", answer)
        self.assertIn("不是相机实拍", answer)

    def test_mujoco_frame_goes_to_vlm(self):
        asked = []

        def capture(camera_name):
            if camera_name == "head_cam":
                return {"success": True, "image": "base64frame"}
            return {"success": False, "result": "missing"}

        def ask_vlm(question, images):
            asked.append((question, images))
            return "前方台面上有杯子和可乐。"

        observer = SceneObserver(
            fetch_objects=lambda: {"success": False, "result": "unused"},
            capture_image=capture,
            ask_vlm=ask_vlm,
            live_frame=lambda: None,
            backend_name="mujoco",
            vision_targets=[],
        )
        answer = observer._describe("前面有什么")
        self.assertEqual(asked[0][0], "前面有什么")
        self.assertEqual(asked[0][1], [("head_cam", "base64frame")])
        self.assertIn("杯子和可乐", answer)
        self.assertIn("看图", answer)
        self.assertNotIn("读不到现场物体", answer)

    def test_capture_without_image_falls_back(self):
        observer = SceneObserver(
            fetch_objects=lambda: {
                "milk_1": {"pos": [0, 0, 0], "grasped": False, "category": "milk"}
            },
            capture_image=lambda camera: {"success": False, "result": "无图像"},
            ask_vlm=lambda question, images: "不应调用",
            live_frame=lambda: None,
            backend_name="mujoco",
            vision_targets=[],
        )
        answer = observer._describe("桌上有什么")
        self.assertIn("牛奶（milk_1）", answer)
        self.assertIn("不是相机实拍", answer)

    def test_http_sim_frame_is_preferred_over_object_list(self):
        asked = []

        class FakeResp:
            def __init__(self, content=b"", content_type="image/jpeg"):
                self.content = content
                self.ok = True
                self.status_code = 200
                self.headers = {"content-type": content_type}

            def json(self):
                return {}

        def http_get(url, timeout=0):
            if url.endswith("/camera/status"):
                return FakeResp(b"{}", "application/json")
            if "camera/latest?camera=head_cam" in url:
                return FakeResp(b"fakejpeg")
            return FakeResp(b"", "text/plain")

        def ask_vlm(question, images):
            asked.append((question, images))
            return "仿真画面里有杯子。"

        observer = SceneObserver(
            fetch_objects=lambda: {
                "cola_1": {"pos": [0, 0, 0], "grasped": False, "category": "cola"}
            },
            ask_vlm=ask_vlm,
            vision_targets=[("mujoco", "http://127.0.0.1:5001")],
            http_get=http_get,
        )
        answer = observer._describe("前面有什么")
        self.assertEqual(asked[0][0], "前面有什么")
        self.assertIn("仿真画面里有杯子", answer)
        self.assertIn("mujoco head_cam 看图", answer)
        self.assertNotIn("不是相机实拍", answer)

    def test_vla_ready_without_frame_is_explained(self):
        class FakeResp:
            def __init__(self, payload=None, ok=True):
                self.content = b""
                self.ok = ok
                self.status_code = 200 if ok else 404
                self.headers = {"content-type": "application/json"}
                self._payload = payload or {}

            def json(self):
                return self._payload

        def http_get(url, timeout=0):
            if url.endswith("/camera/status") or url.endswith("/v1/camera/status"):
                return FakeResp({"ready": True, "streaming": True})
            return FakeResp({}, ok=False)

        observer = SceneObserver(
            fetch_objects=lambda: {
                "cola_1": {"pos": [0, 0, 0], "grasped": False, "category": "cola"}
            },
            ask_vlm=lambda question, images: "不应调用",
            vision_targets=[("vla", "http://192.168.5.194:8091")],
            http_get=http_get,
            http_post=lambda *args, **kwargs: FakeResp({}, ok=False),
        )
        answer = observer._describe("前面有什么")
        self.assertIn("可乐（cola_1）", answer)
        self.assertIn("动作完成后", answer)


if __name__ == "__main__":
    unittest.main()
