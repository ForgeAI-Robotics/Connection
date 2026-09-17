import json
import unittest

from web.services import catalog, format_http_health_detail, start_shell


class CatalogTests(unittest.TestCase):
    def test_brain_and_robot_ids(self):
        items = catalog()
        self.assertEqual(
            [item.id for item in items if item.layer == "brain"],
            ["redis", "master", "deploy", "feishu", "slaver", "desk", "mujoco"],
        )
        self.assertEqual(
            [item.id for item in items if item.layer == "robot"],
            ["dream", "vla"],
        )
        robot = [item for item in items if item.layer == "robot"]
        self.assertTrue(all(not item.controllable for item in robot))

    def test_task_status_health_does_not_dump_json(self):
        body = json.dumps(
            {
                "active": True,
                "all_done": True,
                "failed": True,
                "completed": 1,
                "total": 1,
                "task": "开始接待",
                "subtask_list": [{"subtask": "x" * 200}],
            },
            ensure_ascii=False,
        )
        detail = format_http_health_detail(True, "HTTP 200", body)
        self.assertEqual(detail, "HTTP 200 已结束(失败) 开始接待")
        self.assertNotIn("subtask_list", detail)
        idle = format_http_health_detail(True, "HTTP 200", '{"active": false}')
        self.assertEqual(idle, "HTTP 200 空闲")

    def test_desk_tmux_command(self):
        desk = next(item for item in catalog() if item.id == "desk")
        self.assertEqual(desk.port, 5008)
        self.assertTrue(desk.controllable)
        command = start_shell(desk)
        self.assertIn("serve_desk/main.py", command)
        self.assertIn("exec", command)

    def test_mujoco_tmux_command(self):
        mujoco = next(item for item in catalog() if item.id == "mujoco")
        self.assertEqual(mujoco.port, 5001)
        self.assertTrue(mujoco.controllable)
        command = start_shell(mujoco)
        self.assertIn("serve/main.py", command)
        self.assertIn("--no-viewer", command)
        self.assertIn("MUJOCO_GL=egl", command)
        master = next(item for item in catalog() if item.id == "master")
        self.assertTrue(master.confirm_restart)

    def test_redis_tmux_command_is_foreground(self):
        redis = next(item for item in catalog() if item.id == "redis")
        command = start_shell(redis)
        self.assertIn("'--daemonize' 'no'", command)
        self.assertNotIn("'--daemonize' 'yes'", command)
        self.assertIn("exec", command)
        self.assertIn("tee -a", command)
        self.assertIn("'--logfile' ''", command)

    def test_tail_file_reads_from_the_end(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from web.services import tail_file

        with TemporaryDirectory() as raw:
            path = Path(raw) / "app.log"
            path.write_text("\n".join(f"line-{i}" for i in range(50)) + "\n", encoding="utf-8")
            text = tail_file(path, 3)
        self.assertEqual(text, "line-47\nline-48\nline-49")


if __name__ == "__main__":
    unittest.main()
