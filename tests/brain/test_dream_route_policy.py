import os
import sys
import unittest


from pathlib import Path
ROOT = str(Path(__file__).resolve().parents[2])

from brain.packages.navigation_policy import enforce_dream_route_plan


class DreamRoutePolicyTest(unittest.TestCase):
    def test_table2_then_table1_expands_reviewed_door_route(self):
        original = {
            "reasoning_explanation": "先去二号桌，再去一号桌。",
            "subtask_list": [
                {"robot_name": "FQrobot", "subtask": "导航到table2", "subtask_order": 1},
                {"robot_name": "FQrobot", "subtask": "导航到table1", "subtask_order": 2},
            ],
        }
        result = enforce_dream_route_plan(
            "导航到2号桌，之后再导航到1号桌", original)
        self.assertEqual(4, len(result["subtask_list"]))
        self.assertEqual(
            [1, 2, 3, 4],
            [item["subtask_order"] for item in result["subtask_list"]],
        )
        text = " ".join(item["subtask"] for item in result["subtask_list"])
        self.assertIn("table2", text)
        self.assertIn("door_approach", text)
        self.assertIn("door_lateral_exit", text)
        self.assertIn("table1", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

