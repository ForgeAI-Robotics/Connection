import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops import dream_remote


class DreamRemoteTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        env = patch.dict(os.environ, {"DREAM_SSH_TARGET": "dream"})
        env.start(); self.addCleanup(env.stop)
        self.logdir = tempfile.TemporaryDirectory()
        os.environ["FQPLANNER_LOG_ROOT"] = self.logdir.name
        os.environ["DREAM_FOLLOW_MARKER"] = str(
            Path(self.logdir.name) / "follow-marker"
        )
        self.addCleanup(self.logdir.cleanup)
        self.addCleanup(os.environ.pop, "FQPLANNER_LOG_ROOT", None)
        self.addCleanup(os.environ.pop, "DREAM_FOLLOW_MARKER", None)
        dream_remote._HISTORY.clear()

    def test_start_runs_the_reviewed_oneclick_inside_remote_tmux(self):
        argv, timeout, env = dream_remote.ssh_argv("start")
        self.assertGreaterEqual(timeout, 30)
        self.assertIn("ssh", argv)
        self.assertEqual(argv[-2], "dream")
        remote = argv[-1]
        self.assertIn("tmux new-session -d -s g1_panel_oneclick", remote)
        self.assertIn("g1_three_party_oneclick.sh start", remote)
        self.assertIn("pipe-pane", remote)
        self.assertIn(dream_remote.REMOTE_LOG, remote)
        self.assertNotIn("g1_navigation_only_oneclick.sh", remote)
        self.assertNotIn("123", " ".join(argv))

    def test_resume_reuses_the_same_tmux_host(self):
        remote = dream_remote.ssh_argv("resume")[0][-1]
        self.assertIn("g1_three_party_oneclick.sh resume", remote)
        self.assertIn("tmux new-session -d -s g1_panel_oneclick", remote)

    def test_ready_is_sent_with_send_keys_not_a_held_pipe(self):
        argv, timeout, _env = dream_remote.ready_argv()
        remote = argv[-1]
        self.assertIn("输入 READY", remote)
        self.assertIn("send-keys -t g1_panel_oneclick READY Enter", remote)
        self.assertGreaterEqual(timeout, 60)

    def test_follower_tails_the_remote_log(self):
        argv, _env = dream_remote.follow_argv()
        self.assertIn(f"tail -n +1 -F {dream_remote.REMOTE_LOG}", argv[-1])

    def test_gate_hint_records_the_600_second_deadline(self):
        dream_remote._note_gate("  First Enter -> wait for stage 1 stable.")
        text = dream_remote.history()
        self.assertIn("600 秒", text)
        self.assertIn("navigation remains locked", text)

    def test_preflight_does_not_start(self):
        argv, _timeout, _env = dream_remote.ssh_argv("preflight")
        remote = argv[-1]
        self.assertIn("g1_three_party_oneclick.sh preflight", remote)
        self.assertNotIn("g1_three_party_oneclick.sh check-remote", remote)
        self.assertNotIn("g1_three_party_oneclick.sh start", remote)

    def test_check_remote_is_a_separate_step(self):
        argv, _timeout, _env = dream_remote.ssh_argv("check-remote")
        remote = argv[-1]
        self.assertIn("g1_three_party_oneclick.sh check-remote", remote)
        self.assertNotIn("g1_three_party_oneclick.sh start", remote)

    def test_stand_enter_sends_one_enter_to_the_adapter_window(self):
        argv, _timeout, _env = dream_remote.ssh_argv("stand-enter")
        remote = argv[-1]
        self.assertEqual(
            1, remote.count("send-keys -t g1_fixed_map_relocalize_navigation:adapter")
        )
        self.assertIn("has-session", remote)
        self.assertIn("capture-pane", remote)
        self.assertNotIn("oneclick", remote)

    def test_stand_enter_never_touches_the_strap_or_review_page(self):
        def runner(_argv, _timeout, env=None):
            return subprocess.CompletedProcess(_argv, 0, stdout="stage 1\n", stderr="")

        dream_remote.run("stand-enter", runner=runner)
        text = dream_remote.history()
        self.assertIn("不代解肩带", text)
        self.assertNotIn("Approve", text)

    def test_review_tunnel_forwards_localhost_only_port(self):
        argv, _env = dream_remote.review_tunnel_argv()
        self.assertIn("-N", argv)
        self.assertIn("-L", argv)
        self.assertIn("0.0.0.0:9882:127.0.0.1:9882", argv)
        self.assertEqual("dream", argv[-1])

    def test_stop_preserves_sonic_by_using_official_stop(self):
        argv, _timeout, _env = dream_remote.ssh_argv("stop")
        remote = argv[-1]
        self.assertIn("g1_three_party_oneclick.sh stop", remote)
        self.assertNotIn("g1_three_party_oneclick.sh start", remote)

    def test_successful_start_with_runner_records_follow_up(self):
        def runner(_argv, _timeout, env=None):
            return subprocess.CompletedProcess(
                _argv, 0, stdout="READY_SENT\n", stderr=""
            )

        text = dream_remote.run("start", runner=runner)
        history = dream_remote.history()
        self.assertIn("9882", text)
        self.assertIn("READY_SENT", history)
        self.assertFalse(dream_remote.follow_marker().exists())
        files = list(Path(self.logdir.name).glob("*/dream/monitor.log"))
        self.assertEqual(1, len(files))
        self.assertIn("9882", files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
