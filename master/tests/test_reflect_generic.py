import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


MASTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO_ROOT = os.path.abspath(os.path.join(MASTER_DIR, ".."))
for path in (MASTER_DIR, REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from sop.episode import episode_from_steps, episode_from_task_queue
from sop.reflect import generic_findings, maybe_reflect, reflection_enabled


class _Queue:
    def __init__(self, tasks):
        self.tasks = tasks


class GenericReflectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_dir = os.environ.get("FQPLANNER_REFLECTION_DIR")
        self._old_llm = os.environ.get("FQPLANNER_REFLECTION_LLM")
        self._old_enabled = os.environ.get("REFLECTION_ENABLED")
        os.environ["FQPLANNER_REFLECTION_DIR"] = self._tmp.name
        os.environ["FQPLANNER_REFLECTION_LLM"] = "off"
        os.environ.pop("REFLECTION_ENABLED", None)

        def _restore():
            self._set_env("FQPLANNER_REFLECTION_DIR", self._old_dir)
            self._set_env("FQPLANNER_REFLECTION_LLM", self._old_llm)
            self._set_env("REFLECTION_ENABLED", self._old_enabled)

        self.addCleanup(_restore)

    @staticmethod
    def _set_env(name, value):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def test_generic_findings_success_and_look(self):
        success = episode_from_steps(
            "t1", "去会议室", [{"phase": "导航", "status": "success", "claimed_ok": True}],
            task_type="generic", backend="sim", final="success",
        )
        self.assertEqual("顺利完成", generic_findings(success)[0]["type"])
        look = episode_from_steps(
            "t2", "桌上有什么",
            [{"phase": "拍照查看", "status": "success", "detail": "桌上有可乐"}],
            task_type="look", backend="real", final="success",
        )
        finding = generic_findings(look)[0]
        self.assertEqual("观察完成", finding["type"])
        self.assertIn("可乐", finding["detail"])

    def test_generic_findings_recovery_and_false_success(self):
        recovery = episode_from_steps(
            "t3", "开始接待", [],
            task_type="reception", backend="real", final="recovery",
            error="nav failed",
        )
        self.assertEqual("需要人工恢复", generic_findings(recovery)[0]["type"])
        lied = episode_from_steps(
            "t4", "开始接待",
            [{
                "phase": "VLA放置",
                "status": "failure",
                "claimed_ok": True,
                "verify_ok": False,
                "verify_detail": "桌上没有可乐",
            }],
            task_type="reception", backend="real", final="failure",
        )
        types = {item["type"] for item in generic_findings(lied)}
        self.assertIn("执行报告与实际不符", types)

    def test_episode_from_task_queue(self):
        queue = _Queue([
            {"order": 1, "subtask": "拍照查看：桌上有什么", "done": True,
             "status": "success", "result": "桌上有笔"},
            {"order": 2, "subtask": "未执行", "done": False},
        ])
        episode = episode_from_task_queue(
            "look-1", "桌上有什么", queue, task_type="look", backend="sim",
        )
        self.assertEqual(1, len(episode["steps"]))
        self.assertEqual("look", episode["task_type"])
        self.assertEqual("sim", episode["backend"])

    def test_maybe_reflect_persists_without_writing_sop(self):
        episode = episode_from_steps(
            "task-persist", "去门口",
            [{"phase": "导航到门口", "status": "success", "claimed_ok": True}],
            task_type="generic", backend="sim", final="success",
        )
        result = maybe_reflect(episode, config={"reflection": {"write_sop": False}}, quiet=True)
        self.assertFalse(result.get("skipped"))
        self.assertTrue(result.get("summary"))
        self.assertIsNone(result.get("sop_v2"))
        saved = Path(result["path"])
        self.assertTrue(saved.is_file())
        payload = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual("fq/reflection-episode/v1", payload["episode"]["schema"])
        candidates = Path(self._tmp.name) / "candidates.jsonl"
        self.assertTrue(candidates.is_file())
        sop_v2 = Path(MASTER_DIR) / "sop" / "reception_sop_v2.yaml"
        before = sop_v2.stat().st_mtime if sop_v2.exists() else None
        maybe_reflect(episode, quiet=True)
        after = sop_v2.stat().st_mtime if sop_v2.exists() else None
        self.assertEqual(before, after)

    def test_reflection_can_be_disabled(self):
        os.environ["REFLECTION_ENABLED"] = "0"
        self.assertFalse(reflection_enabled({"reflection": {"enabled": True}}))
        episode = episode_from_steps("off", "去门口", [], final="success")
        result = maybe_reflect(episode, quiet=True)
        self.assertTrue(result.get("skipped"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
