import time
import unittest

from ops.services import Service
from ops import tmuxctl


class TmuxControlTests(unittest.TestCase):
    def setUp(self):
        if not tmuxctl.tmux_available():
            self.skipTest("tmux 不可用")
        self.service = Service(
            id="paneltest",
            name="paneltest",
            layer="brain",
            controllable=True,
            window="paneltest",
            match="paneltest-sleep-marker",
        )

    def tearDown(self):
        if tmuxctl.session_exists("paneltest"):
            tmuxctl.stop_session(self.service, timeout=2)

    def test_start_stop_own_session(self):
        tmuxctl.start_session(
            self.service,
            "exec sleep 30 # paneltest-sleep-marker",
        )
        tmuxctl.wait_session(self.service, timeout=5)
        self.assertTrue(tmuxctl.session_alive("paneltest"))
        self.assertIn("paneltest", tmuxctl.list_sessions())
        pane = tmuxctl.capture_pane("paneltest", 20)
        self.assertIsInstance(pane, str)
        self.assertNotIn("can't find pane", pane)
        tmuxctl.stop_session(self.service, timeout=3)
        time.sleep(0.4)
        self.assertFalse(tmuxctl.session_alive("paneltest"))
        self.assertNotIn("paneltest", tmuxctl.list_sessions())


if __name__ == "__main__":
    unittest.main()
