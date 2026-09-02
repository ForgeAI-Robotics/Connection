import unittest

from integrations.feishu.classifier import RiskLevel, classify_task


class ClassifierTests(unittest.TestCase):
    def test_read_only_task_is_immediate(self):
        result = classify_task("查看机器人当前位置")
        self.assertEqual(result.risk, RiskLevel.READ_ONLY)
        self.assertFalse(result.requires_confirmation)

    def test_motion_wins_over_read_only(self):
        result = classify_task("查看货架状态并移动到货架前")
        self.assertEqual(result.risk, RiskLevel.MOTION)
        self.assertTrue(result.requires_confirmation)

    def test_unknown_task_is_ambiguous(self):
        result = classify_task("帮我处理一下现场")
        self.assertEqual(result.risk, RiskLevel.AMBIGUOUS)
        self.assertTrue(result.requires_confirmation)


if __name__ == "__main__":
    unittest.main()
