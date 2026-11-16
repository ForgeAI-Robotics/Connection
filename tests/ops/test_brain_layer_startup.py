"""Panel startup brings up a stopped brain layer and leaves running sessions alone."""
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from ops.control import ensure_brain_layer
from ops.services import Service, catalog


def _service(service_id, name, layer, controllable=True):
    return Service(id=service_id, name=name, layer=layer, controllable=controllable)


class BrainLayerStartupTests(unittest.TestCase):
    def test_catalog_brain_layer_is_master_and_three_entries(self):
        self.assertEqual(
            [item.id for item in catalog() if item.layer == "brain"],
            ["master", "deploy", "feishu", "voice"],
        )

    def test_only_stopped_brain_services_start_and_one_failure_continues(self):
        services = [
            _service("master", "大脑", "brain"),
            _service("deploy", "任务网页", "brain"),
            _service("feishu", "飞书入口", "brain"),
            _service("voice", "语音入口", "brain"),
            _service("redis", "Redis", "support"),
            _service("dream", "导航", "robot", controllable=False),
        ]
        states = {"master": "stopped", "deploy": "tmux", "feishu": "unmanaged", "voice": "stopped"}
        started = []

        def start(service):
            if service.id == "voice":
                raise RuntimeError("voice down")
            started.append(service.id)

        with patch("ops.services.catalog", return_value=services), \
                patch("ops.control._brain_state", side_effect=lambda service: states[service.id]), \
                patch("ops.control._start", side_effect=start), \
                patch("shared.execution_profile.admission", return_value=nullcontext()):
            results = ensure_brain_layer()

        self.assertEqual(started, ["master"])
        self.assertEqual(
            [(item["id"], item["action"]) for item in results],
            [("master", "started"), ("deploy", "skipped"), ("feishu", "skipped"), ("voice", "failed")],
        )
        self.assertEqual(results[1]["state"], "tmux")
        self.assertEqual(results[2]["state"], "unmanaged")
        self.assertIn("voice down", results[3]["error"])

    def test_panel_main_starts_the_brain_layer_beside_the_server(self):
        import ops.__main__ as panel_main

        app = MagicMock()
        app.extensions = {"workers": []}
        called = threading.Event()

        def run(**_kwargs):
            self.assertTrue(called.wait(2))

        app.run.side_effect = run
        with patch.object(panel_main, "create_app", return_value=app), \
                patch.object(panel_main, "attach_process_log", create=True), \
                patch("ops.control.ensure_brain_layer", side_effect=lambda: called.set()):
            # attach_process_log is imported inside main.
            with patch("shared.log_setup.attach_process_log"):
                panel_main.main()
        app.run.assert_called_once()
