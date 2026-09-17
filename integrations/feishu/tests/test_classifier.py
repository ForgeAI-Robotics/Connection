import unittest

from integrations.feishu.classifier import (
    Intent,
    RiskLevel,
    classify_task,
    needs_llm_route,
    parse_route_intent,
    refine_with_llm,
)


class ClassifierTests(unittest.TestCase):
    def test_read_only_task_is_immediate(self):
        result = classify_task("查看机器人当前位置")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.READ_ONLY)
        self.assertFalse(result.requires_confirmation)

    def test_motion_wins_over_read_only(self):
        result = classify_task("查看货架状态并移动到货架前")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.MOTION)
        self.assertTrue(result.requires_confirmation)

    def test_unknown_task_is_ambiguous(self):
        result = classify_task("帮我处理一下现场")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.AMBIGUOUS)
        self.assertTrue(result.requires_confirmation)

    def test_weather_is_chat(self):
        result = classify_task("天气怎么样")
        self.assertEqual(result.intent, Intent.CHAT)
        self.assertFalse(result.requires_confirmation)

    def test_food_question_is_chat_not_observe(self):
        result = classify_task("北京有什么好吃的")
        self.assertEqual(result.intent, Intent.CHAT)
        self.assertFalse(result.requires_confirmation)

    def test_desk_question_is_company_look_task(self):
        result = classify_task("桌上有什么")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.READ_ONLY)
        self.assertFalse(result.requires_confirmation)

    def test_table_wording_is_company_look_task(self):
        result = classify_task("桌子上有什么")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.READ_ONLY)
        self.assertFalse(result.requires_confirmation)

    def test_front_question_is_company_look_task(self):
        result = classify_task("前面有什么东西")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.READ_ONLY)

    def test_look_around_is_company_task(self):
        result = classify_task("看一下面前有啥")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.READ_ONLY)

    def test_reception_stays_motion_task(self):
        result = classify_task("开始接待")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.MOTION)

    def test_tidy_stays_motion_task(self):
        result = classify_task("整理可乐")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.MOTION)

    def test_motion_wins_over_observe(self):
        result = classify_task("看看桌上有什么然后整理可乐")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.MOTION)

    def test_where_is_question_is_ambiguous_before_llm(self):
        result = classify_task("天安门在哪里")
        self.assertEqual(result.intent, Intent.TASK)
        self.assertEqual(result.risk, RiskLevel.AMBIGUOUS)
        self.assertTrue(needs_llm_route(result))

    def test_llm_routes_where_question_to_chat(self):
        seed = classify_task("方奇科技（北京）在哪里")
        refined = refine_with_llm(seed, "chat")
        self.assertEqual(refined.intent, Intent.CHAT)
        self.assertFalse(refined.requires_confirmation)

    def test_llm_cannot_override_motion(self):
        seed = classify_task("开始接待")
        refined = refine_with_llm(seed, "chat")
        self.assertEqual(refined.intent, Intent.TASK)
        self.assertEqual(refined.risk, RiskLevel.MOTION)

    def test_parse_route_intent_reads_first_token(self):
        self.assertEqual(parse_route_intent("chat"), Intent.CHAT)
        self.assertEqual(parse_route_intent("observe\n因为是现场"), Intent.OBSERVE)
        self.assertEqual(parse_route_intent("task"), Intent.TASK)
        self.assertIsNone(parse_route_intent("maybe"))

    def test_llm_observe_becomes_company_task(self):
        seed = classify_task("帮我处理一下现场")
        refined = refine_with_llm(seed, "observe", "桌上有什么")
        self.assertEqual(refined.intent, Intent.TASK)
        self.assertEqual(refined.risk, RiskLevel.READ_ONLY)


if __name__ == "__main__":
    unittest.main()
