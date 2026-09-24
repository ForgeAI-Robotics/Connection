import unittest

from contracts.look import is_look_task, preferred_camera


class LookTaskTests(unittest.TestCase):
    def test_desk_and_front_questions(self):
        self.assertTrue(is_look_task("桌上有什么"))
        self.assertTrue(is_look_task("看一下面前有啥"))
        self.assertTrue(is_look_task("前面有什么东西"))
        self.assertTrue(is_look_task("拍照查看：桌上有什么"))

    def test_motion_is_not_look_only(self):
        self.assertFalse(is_look_task("看看桌上有什么然后整理可乐"))
        self.assertFalse(is_look_task("开始接待"))
        self.assertFalse(is_look_task("把可乐放到盘子上"))

    def test_knowledge_is_not_look(self):
        self.assertFalse(is_look_task("天安门在哪里"))
        self.assertFalse(is_look_task("北京有什么好吃的"))

    def test_preferred_camera(self):
        self.assertEqual(preferred_camera("前面有什么"), "head_cam")
        self.assertEqual(preferred_camera("桌上有什么"), "overhead_cam")


if __name__ == "__main__":
    unittest.main()
