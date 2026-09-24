import json
import unittest

from ops.services import catalog, format_http_health_detail, start_shell


class CatalogTests(unittest.TestCase):
    def test_brain_and_robot_ids(self):
        items = catalog()
        self.assertEqual(
            [item.id for item in items if item.layer == "brain"],
            ["redis", "master", "deploy", "feishu", "slaver", "desk", "mujoco", "gs"],
        )
        self.assertEqual(
            [item.id for item in items if item.layer == "robot"],
            ["dream", "vla"],
        )
        robot = [item for item in items if item.layer == "robot"]
        self.assertTrue(all(not item.controllable for item in robot))
        dream = next(item for item in robot if item.id == "dream")
        vla = next(item for item in robot if item.id == "vla")
        self.assertEqual(dream.remote_control, "ssh_dream")
        self.assertEqual(
            dream.remote_actions, ("start", "stand-enter", "stop")
        )
        self.assertTrue(dream.confirm_start)
        self.assertFalse(dream.disabled_action)
        self.assertIn("9882", dream.confirm_start_message)
        self.assertIn("肩带", dict(dream.action_confirms)["stand-enter"])
        self.assertEqual(vla.remote_control, "ssh_vla")
        self.assertEqual(vla.remote_actions, ("start", "stop"))
        self.assertTrue(vla.confirm_start)

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
        recovery = format_http_health_detail(
            True,
            "HTTP 200",
            json.dumps(
                {
                    "active": True,
                    "all_done": False,
                    "state": "RECOVERY_REQUIRED",
                    "task": "开始接待",
                },
                ensure_ascii=False,
            ),
        )
        self.assertEqual(recovery, "HTTP 200 待恢复 开始接待")

    def test_desk_tmux_command(self):
        desk = next(item for item in catalog() if item.id == "desk")
        self.assertEqual(desk.port, 5008)
        self.assertTrue(desk.controllable)
        command = start_shell(desk)
        self.assertIn("simulation/backends/desk/main.py", command)
        self.assertIn("exec", command)

    def test_mujoco_tmux_command(self):
        mujoco = next(item for item in catalog() if item.id == "mujoco")
        self.assertEqual(mujoco.port, 5001)
        self.assertTrue(mujoco.controllable)
        command = start_shell(mujoco)
        self.assertIn("simulation/backends/mujoco/main.py", command)
        self.assertIn("--no-viewer", command)
        self.assertIn("MUJOCO_GL=egl", command)
        master = next(item for item in catalog() if item.id == "master")
        self.assertTrue(master.confirm_restart)

    def test_gs_tmux_command(self):
        gs = next(item for item in catalog() if item.id == "gs")
        self.assertEqual(gs.port, 5002)
        self.assertTrue(gs.controllable)
        command = start_shell(gs)
        self.assertIn("simulation/backends/gs/main.py", command)
        self.assertIn("--no-viewer", command)
        self.assertIn("--robot xlerobot", command)
        self.assertIn("exec", command)

    def test_redis_tmux_command_is_foreground(self):
        from unittest.mock import patch
        with patch('ops.redis_service.build_redis_command', return_value=['redis-server', '--daemonize', 'no']), patch('ops.redis_service.redis_env', return_value={}):
            cmd = start_shell(next(s for s in catalog() if s.id == 'redis'))
        self.assertIn("'--daemonize' 'no'", cmd)
        self.assertIn('tee -a', cmd)

    def test_every_card_has_a_log_service(self):
        items = catalog()
        self.assertTrue(all(item.log_service for item in items))
        self.assertEqual(
            {item.id: item.log_service for item in items}["feishu"],
            "feishu",
        )
        self.assertEqual(
            {item.id: item.log_service for item in items}["desk"],
            "desk",
        )
        self.assertEqual(
            {item.id: item.log_service for item in items}["mujoco"],
            "mujoco",
        )
        self.assertEqual(
            {item.id: item.log_service for item in items}["gs"],
            "gs",
        )
        self.assertEqual(
            {item.id: item.log_service for item in items}["dream"],
            "dream",
        )
        self.assertEqual(
            {item.id: item.log_service for item in items}["vla"],
            "vla",
        )

    def test_latest_log_file_reads_day_service_folder(self):
        import os
        from datetime import datetime
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from ops.services import by_id, latest_log_file
        from shared import log_setup

        with TemporaryDirectory() as raw:
            os.environ[log_setup.LOG_ROOT_ENV] = raw
            self.addCleanup(os.environ.pop, log_setup.LOG_ROOT_ENV, None)
            day = Path(raw) / datetime.now().strftime("%Y-%m-%d") / "master"
            day.mkdir(parents=True)
            older = day / "10-00-00.log"
            newer = day / "11-00-00.log"
            older.write_text("old\n", encoding="utf-8")
            newer.write_text("new\n", encoding="utf-8")
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))
            self.assertEqual(newer, latest_log_file(by_id("master")))

    def test_remote_probe_rate_limits_monitor_file(self):
        import os
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from ops import services
        from shared import log_setup

        services.PROBE_HISTORY.clear()
        services._LAST_MONITOR.clear()
        with TemporaryDirectory() as raw:
            os.environ[log_setup.LOG_ROOT_ENV] = raw
            self.addCleanup(os.environ.pop, log_setup.LOG_ROOT_ENV, None)
            self.addCleanup(services.PROBE_HISTORY.clear)
            self.addCleanup(services._LAST_MONITOR.clear)
            services._record_probe("vla", "down", ok=False)
            services._record_probe("vla", "still down", ok=False)
            services._record_probe("vla", "up", ok=True)
            files = list(Path(raw).glob("*/vla/monitor.log"))
            self.assertEqual(1, len(files))
            text = files[0].read_text(encoding="utf-8")
        self.assertIn("down", text)
        self.assertNotIn("still down", text)
        self.assertIn("up", text)

    def test_tail_file_reads_from_the_end(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from ops.services import tail_file

        with TemporaryDirectory() as raw:
            path = Path(raw) / "app.log"
            path.write_text("\n".join(f"line-{i}" for i in range(50)) + "\n", encoding="utf-8")
            text = tail_file(path, 3)
        self.assertEqual(text, "line-47\nline-48\nline-49")


if __name__ == "__main__":
    unittest.main()
