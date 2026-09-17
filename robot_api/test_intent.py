import unittest

from robot_api.intent import (
    Intent,
    RiskLevel,
    classify_entry,
    classify_task,
    refine_with_llm,
)


class EntryClassifierTests(unittest.TestCase):
    def test_reception_is_motion_task(self):
        report = classify_entry("开始接待")
        self.assertEqual(report["intent"], "task")
        self.assertEqual(report["risk"], "motion")
        self.assertTrue(report["requires_confirmation"])
        self.assertFalse(report["needs_llm_route"])

    def test_weather_is_chat(self):
        report = classify_entry("天气怎么样")
        self.assertEqual(report["intent"], "chat")
        self.assertFalse(report["requires_confirmation"])

    def test_desk_look_is_read_only_task(self):
        report = classify_entry("桌上有什么")
        self.assertEqual(report["intent"], "task")
        self.assertEqual(report["risk"], "read_only")
        self.assertFalse(report["requires_confirmation"])

    def test_llm_can_send_ambiguous_where_question_to_chat(self):
        seed = classify_task("天安门在哪里")
        self.assertEqual(seed.intent, Intent.TASK)
        self.assertEqual(seed.risk, RiskLevel.AMBIGUOUS)
        report = classify_entry("天安门在哪里", llm_label="chat")
        self.assertEqual(report["intent"], "chat")
        self.assertFalse(report["requires_confirmation"])

    def test_llm_cannot_override_motion(self):
        refined = refine_with_llm(classify_task("开始接待"), "chat")
        self.assertEqual(refined.intent, Intent.TASK)
        self.assertEqual(refined.risk, RiskLevel.MOTION)


if __name__ == "__main__":
    unittest.main()
