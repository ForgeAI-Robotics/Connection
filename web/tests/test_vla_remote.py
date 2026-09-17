import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from web import vla_remote


class VlaRemoteTests(unittest.TestCase):
    def setUp(self):
        self.logdir = tempfile.TemporaryDirectory()
        os.environ["FQPLANNER_LOG_ROOT"] = self.logdir.name
        self.addCleanup(self.logdir.cleanup)
        self.addCleanup(os.environ.pop, "FQPLANNER_LOG_ROOT", None)
        vla_remote._HISTORY.clear()

    def test_start_command_follows_documented_runtime_sequence(self):
        argv, timeout, env = vla_remote.ssh_argv("start")
        self.assertGreaterEqual(timeout, 90)
        self.assertIn("ssh", argv)
        self.assertEqual(argv[-2], "gpu4090")
        remote = argv[-1]
        self.assertIn("./run_agent_vla_runtime.sh check", remote)
        self.assertIn("./run_phase_aware_action_stack.sh live-dependencies", remote)
        self.assertIn("./run_agent_vla_runtime.sh start", remote)
        self.assertIn("./run_agent_vla_runtime.sh status", remote)
        self.assertIn("BRAIN_VLA_REAL_ACTION_ACK=PHYSICAL_ESTOP_READY", remote)
        self.assertNotIn("run_real_inference_stack.sh", remote)
        self.assertNotIn("run_phase_aware_action_stack.sh start", remote)
        self.assertNotIn("123", " ".join(argv))

    def test_stop_does_not_send_the_physical_ack(self):
        argv, _timeout, _env = vla_remote.ssh_argv("stop")
        remote = argv[-1]
        self.assertIn("./run_agent_vla_runtime.sh stop", remote)
        self.assertNotIn("BRAIN_VLA_REAL_ACTION_ACK", remote)

    def test_permission_denied_is_translated(self):
        def runner(_argv, _timeout, env=None):
            return subprocess.CompletedProcess(
                _argv, 255, stdout="", stderr="Permission denied (publickey)."
            )

        with self.assertRaises(RuntimeError) as raised:
            vla_remote.run("start", runner=runner)
        self.assertIn("4090 SSH 登录失败", str(raised.exception))

    def test_successful_start_records_output(self):
        def runner(_argv, _timeout, env=None):
            return subprocess.CompletedProcess(
                _argv, 0, stdout="AGENT_VLA_RUNTIME_STARTED\n", stderr=""
            )

        text = vla_remote.run("start", runner=runner)
        self.assertIn("AGENT_VLA_RUNTIME_STARTED", text)
        self.assertIn("AGENT_VLA_RUNTIME_STARTED", vla_remote.history())
        self.assertIn("check → live-dependencies", vla_remote.history())
        files = list(Path(self.logdir.name).glob("*/vla/monitor.log"))
        self.assertEqual(1, len(files))
        self.assertIn("AGENT_VLA_RUNTIME_STARTED", files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
