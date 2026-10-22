"""Mixed reception through Runtime, with no real network or robot actions."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from brain.app import create_service
from brain.adapters.ports import build_port, target_identity
from brain.adapters.simulated_manipulation import HybridReceptionAdapter, SimulatedManipulationClient
from brain.adapters.http_client import HttpContractError
from brain.adapters.execution import BodyAdapter
from brain.packages.reception_skill import BASELINE_PATH
from brain.packages.registry import PackagePolicy
from brain.packages.reception_skill import load_skill


TRIGGER = "开始接待（导航真机＋操控模拟）"
BACKEND = "reception_real_sim_manipulation"


class HandoffReasonTests(unittest.TestCase):
    def test_both_adapters_distinguish_failure_stop_resources_and_invalid_receipts(self):
        source = {'terminal': 'succeeded', 'stopped': True, 'resources_released': True,
                  'evidence': {'identity_ok': True, 'time_ok': True}}
        cases = [({'terminal': 'failed'}, 'source_failed'),
                 ({'terminal': 'cancelled'}, 'source_cancelled'),
                 ({'terminal': ''}, 'source_success_unconfirmed'),
                 ({'terminal': 'failed', 'stopped': False}, 'source_stop_unconfirmed'),
                 ({'resources_released': False}, 'source_resources_unreleased'),
                 ({'evidence': {'identity_ok': False, 'time_ok': True}}, 'source_receipt_invalid'),
                 ({'evidence': {'identity_ok': True, 'time_ok': False}}, 'source_receipt_invalid')]
        for adapter_type in (BodyAdapter, HybridReceptionAdapter):
            for changes, expected in cases:
                with self.subTest(adapter=adapter_type.__name__, changes=changes):
                    nav, vla = Mock(), Mock()
                    adapter = adapter_type(nav, vla)
                    observed = dict(deepcopy(source), **changes)
                    adapter.query = Mock(return_value=observed)
                    result = adapter.handoff_with_context({
                        'task_id': 'task', 'kind': 'to_vla', 'source_command_id': 'original',
                        'source_request': {'skill': 'navigate'}})
                    self.assertFalse(result['confirmed'])
                    self.assertEqual(result['reason'], expected)
                    self.assertEqual(result['source'], observed)
                    nav.status.assert_not_called()
                    vla.control_status.assert_not_called()

class NavigationPeer:
    def __init__(self):
        self.commands, self.submits = {}, []
        self.fail_leg = None
        self.busy = False

    def status(self):
        return {"navigation_transport_ready": True,
                "active_command_id": "other" if self.busy else None,
                "active_command_state": "navigating" if self.busy else None}

    def submit_navigation(self, body):
        self.submits.append(deepcopy(body))
        ok = body["leg_index"] != self.fail_leg
        now = datetime.now().astimezone().isoformat()
        self.commands[body["command_id"]] = {
            **deepcopy(body), "state": "succeeded" if ok else "failed",
            "started_at": now, "completed_at": now,
            "result": {"success": ok, "reached": ok, "navigation_stopped": True},
        }
        return {"accepted": True}

    def command(self, command_id):
        return deepcopy(self.commands[command_id])

    def wait_command(self, command_id, **kwargs):
        return self.command(command_id)


class MixedReceptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.nav = NavigationPeer()
        self.mock = SimulatedManipulationClient(self.nav, delay_sec=0)
        self.adapter = HybridReceptionAdapter(self.nav, self.mock)
        self.config = {
            "brain": {"capture_timeline": False, "reasoning": {"recovery_advice": False}},
            "reflection": {"enabled": False},
            "reception_real": {"kernel_runtime_dir": self.temp.name, "kernel_enabled": True,
                               "dream_base_url": "http://nav.invalid:8001", "vla_base_url": "INVALID",
                               "skill_file": str(BASELINE_PATH.with_name("reception_single_can_v2.yaml")),
                               "dream_inspection_enabled": True},
        }
        self.route = patch("brain.service.select_backend", return_value="reception_real")
        self.route.start()
        self.addCleanup(self.route.stop)
        # Never inherit a live environment or its admission locks.
        env = patch.dict("os.environ", {"FQ_EXECUTION_STATE": self.temp.name + "/execution.json",
                                       "FQ_EXECUTION_LOCK": self.temp.name + "/execution.lock",
                                       "FQ_EXECUTION_BLOCK": self.temp.name + "/execution.blocked"})
        env.start()
        self.addCleanup(env.stop)

    def service(self, adapter=None):
        return create_service(self.config, model=lambda _: self.fail("unexpected model call"),
                              port_factory=lambda backend: adapter or self.adapter)

    def test_complete_flow_uses_same_skill_and_simulated_open_grasp(self):
        service = self.service()
        with patch('brain.adapters.http_client.HttpClient.request_json', side_effect=AssertionError('real VLA HTTP')):
            service.publish(TRIGGER, "mixed")
        record, status = service.runtime.record, service.status()
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(record["execution_backend"], BACKEND)
        self.assertEqual(record["execution_target"], target_identity(self.config, BACKEND))
        self.assertEqual([b["leg_index"] for b in self.nav.submits], [1, 2, 3, 4, 5])
        self.assertNotIn("DREAM_INSPECTING_TABLE2", record["phase_order"])
        self.assertEqual(len(self.mock._commands), 2)
        from contracts.steps import step_spec_dict
        self.assertEqual(record['phase_specs'], [step_spec_dict(s) for s in load_skill(self.config).phases(real=True)])
        self.assertEqual(record['selection']['workflow_variant'], 'real_sim_manipulation')
        self.assertEqual(record['holding'], 'cola_can_1')
        self.assertEqual([r['operation'] for r in self.mock._commands.values()], ['open_fridge','grasp_cola'])
        for step in ("VLA_OPEN_FRIDGE", "VLA_GRASP_COLA"):
            a = record["steps"][step]["attempts"][0]
            raw = a["progress"]["evidence"]["downstream"]
            self.assertEqual(a["verdict"], "PASS")
            self.assertTrue(raw["simulated"])
            self.assertFalse(raw["result"]["physical_execution"])
            self.assertEqual(raw["result"]["evidence_level"], "simulated_object")
        self.assertEqual(sum(t["simulated"] for t in status["subtask_list"]), 4)
        self.assertTrue(record["handoff_observation"]["simulated"])
        self.assertFalse(record["handoff_observation"]["physical_control_transfer"])
        self.assertIsNone(status['vla_confirmation'])
        from brain.adapters.execution import interpret_observation
        raw = record['steps']['VLA_GRASP_COLA']['attempts'][0]
        physical_view = interpret_observation(raw['command_id'], raw['progress']['evidence']['downstream'], raw['request'])
        self.assertFalse(physical_view['evidence']['supports'])
        # Reattach restores only existing receipts, never repeats a physical goal.
        restored = HybridReceptionAdapter(self.nav, SimulatedManipulationClient(self.nav))
        second = self.service(restored)
        second.attach()
        self.assertEqual(len(restored.vla._commands), 2)
        self.assertEqual(len(self.nav.submits), 5)

    def test_navigation_failure_blocks_mock_until_manual_retry(self):
        self.nav.fail_leg = 1
        service = self.service()
        service.publish(TRIGGER, "failure")
        self.assertEqual(service.status()["state"], "recovery_required")
        self.assertEqual(len(self.mock._commands), 0)
        handoff = service.runtime.record['handoff_observation']
        self.assertEqual(handoff['reason'], 'source_failed')
        self.assertTrue(handoff['source']['stopped'])
        self.assertTrue(handoff['source']['resources_released'])
        self.nav.fail_leg = None
        service.control("continue", task_id="failure")
        self.assertEqual(service.status()["state"], "succeeded")
        self.assertTrue(self.nav.submits[1]["command_id"].endswith("-r1"))
        pick = next(raw for raw in self.mock._commands.values() if raw["operation"] == "grasp_cola")
        self.assertEqual(pick["navigation_proof"]["dream_command_id"], self.nav.submits[1]["command_id"])

    def test_skip_refused_mock_pick_after_skipped_navigation_survives_restart(self):
        self.use_legacy_skill()
        self.nav.fail_leg = 1
        service = self.service()
        service.publish(TRIGGER, 'skip-refused-pick')
        service.control('skip', task_id='skip-refused-pick', step_id='NAVIGATING_TO_TABLE2')
        self.assertEqual(service.status()['failure_detail']['code'], 'NAVIGATION_PROOF_INVALID')
        service.control('continue', task_id='skip-refused-pick')
        self.assertEqual(service.status()['failure_detail']['code'], 'NAVIGATION_PROOF_INVALID')
        original = deepcopy(service.runtime.record['steps']['VLA_PICKING']['attempts'])
        restarted = self.service()
        runtime = restarted.attach()
        with patch.object(self.mock, 'task', side_effect=AssertionError('refused command must not be queried')), \
             patch.object(self.nav, 'command', wraps=self.nav.command) as query:
            result = runtime.skip_current(step_id='VLA_PICKING')
        self.assertEqual(result['next_step_id'], 'NAVIGATING_TO_RELAY1')
        query.assert_called_once_with(self.nav.submits[0]['command_id'])
        self.assertEqual(runtime.record['steps']['VLA_PICKING']['attempts'], original)
        self.assertEqual(runtime.record['steps']['VLA_PICKING']['status'], 'manual_skipped')
        self.assertEqual(runtime.record['steps']['VERIFYING_GRASP']['status'], 'manual_skipped')
        self.assertFalse(self.mock._commands)
        self.assertIsNone(runtime.record['holding'])
        runtime.drive()
        self.assertEqual(self.nav.submits[1]['leg_index'], 2)
        self.assertFalse(any(x['operation'] == 'pick' for x in self.mock._commands.values()))

    def test_refused_pick_does_not_bypass_previous_navigation_stop_check(self):
        self.use_legacy_skill()
        from contracts.tasks import Rejected
        self.nav.fail_leg = 1
        service = self.service()
        service.publish(TRIGGER, 'skip-with-unknown-nav')
        service.control('skip', task_id='skip-with-unknown-nav', step_id='NAVIGATING_TO_TABLE2')
        command = self.nav.submits[0]['command_id']
        self.nav.commands[command]['state'] = 'running'
        self.nav.commands[command]['result'] = None
        before = deepcopy(service.runtime.record['manual_skips'])
        with self.assertRaises(Rejected):
            service.runtime.skip_current(step_id='VLA_PICKING')
        self.assertEqual(service.runtime.record['manual_skips'], before)
        self.assertEqual(len(self.nav.submits), 1)

    def test_busy_navigation_boundary_blocks_simulated_grasp(self):
        self.nav.busy = True
        service = self.service()
        service.publish(TRIGGER, "busy")
        self.assertNotEqual(service.status()["state"], "succeeded")
        self.assertEqual(len(self.mock._commands), 0)
        self.assertEqual(len(self.nav.submits), 1)

    def test_simulated_failure_does_not_advance_to_next_navigation(self):
        original = self.mock.task

        def failed(command_id):
            raw = original(command_id)
            raw["state"] = "failed"
            raw["result"].update(success=False, object_grasped=False)
            return raw

        with patch.object(self.mock, "task", side_effect=failed):
            service = self.service()
            service.publish(TRIGGER, "mock-failure")
        self.assertEqual(service.status()["state"], "recovery_required")
        self.assertEqual(len(self.nav.submits), 1)
        attempt = service.runtime.record["steps"]["VLA_OPEN_FRIDGE"]["attempts"][0]
        self.assertEqual(attempt["verdict"], "FAIL")

    def test_missing_simulated_effect_is_not_promoted_to_success(self):
        original = self.mock.task

        def weak(command_id):
            raw = original(command_id)
            raw["result"]["evidence_level"] = "hand_state_only"
            return raw

        with patch.object(self.mock, "task", side_effect=weak):
            service = self.service()
            service.publish(TRIGGER, "weak-mock")
        self.assertNotEqual(service.status()["state"], "succeeded")
        self.assertEqual(len(self.nav.submits), 1)
        attempt = service.runtime.record["steps"]["VLA_OPEN_FRIDGE"]["attempts"][0]
        self.assertEqual(attempt["verdict"], "UNKNOWN")

    def test_non_real_environment_is_rejected_before_dispatch(self):
        with patch("brain.service.select_backend", return_value="reception_protocol"):
            service = self.service()
            service.publish(TRIGGER, "wrong-environment")
        self.assertEqual(service.status()["state"], "recovery_required")
        self.assertEqual(self.nav.submits, [])

    def test_ros_hybrid_requires_simulation_route_and_original_ros_identity(self):
        with patch("brain.service.select_backend", return_value="reception_simple"), \
                patch("brain.adapters.ports.protocol_endpoints", return_value={"dream": "http://nav.invalid", "vla": "http://unused.invalid"}), \
                patch("shared.protocol_simulation.require_simulator_pair", return_value="isolated") as identity:
            service = self.service()
            service.publish("开始接待（导航仿真＋操控模拟）", "ros-mixed")
        self.assertEqual(service.status()["state"], "succeeded")
        self.assertEqual(service.status()["execution_backend"], "reception_ros_sim_manipulation")
        self.assertEqual(identity.call_args.kwargs["scene"], "office_ros_8001")
        self.assertTrue(identity.call_args.kwargs["navigation_only"])
        self.assertEqual(sum(t["simulated"] for t in service.status()["subtask_list"]), 4)

    def test_ros_trigger_cannot_dispatch_to_real_environment(self):
        service = self.service()
        service.publish("开始接待（导航仿真＋操控模拟）", "not-real")
        self.assertEqual(service.status()["state"], "recovery_required")
        self.assertEqual(self.nav.submits, [])

    def test_factory_never_constructs_real_vla_and_motion_gate_is_preserved(self):
        with patch("brain.adapters.vla_client.VlaClient.__init__", side_effect=AssertionError("real VLA")):
            port = build_port(self.config, BACKEND)
        self.assertIsInstance(port.vla, SimulatedManipulationClient)
        self.assertTrue(port.enabled)
        config = deepcopy(self.config)
        config["reception_real"]["kernel_enabled"] = False
        port = build_port(config, BACKEND)
        self.assertFalse(port.gate_open)
        self.assertFalse(port.submit("x", {})["accepted"])

    def pick_request(self):
        skill = load_skill(self.config)
        nav = next(s for s in skill.steps if s.kind == "navigate")
        pick = next(s for s in skill.steps if s.kind == "pick")
        policy = PackagePolicy()
        self.nav.submit_navigation(policy.request(nav, "task", "nav", lambda _: "")["body"])
        return policy.request(pick, "task", "pick", lambda _: "nav")

    def use_legacy_skill(self):
        import yaml
        definition = deepcopy(load_skill(self.config).definition)
        definition.pop('real_workflow')
        path = Path(self.temp.name) / 'legacy.yaml'
        path.write_text(yaml.safe_dump(definition, allow_unicode=True))
        self.config['reception_real']['skill_file'] = str(path)

    def test_partial_delivery_restart_restores_virtual_holding_without_replaying_actions(self):
        self.nav.fail_leg = 3
        service = self.service()
        service.publish(TRIGGER, 'restart-native-mock')
        self.assertEqual(service.status()['state'], 'recovery_required')
        self.assertEqual(len(self.mock._commands), 2)
        original = set(self.mock._commands)
        restored = HybridReceptionAdapter(self.nav, SimulatedManipulationClient(self.nav, delay_sec=0))
        second = self.service(restored)
        second.attach()
        self.assertEqual(restored.vla.control_status()['holding'], 'cola_can_1')
        self.nav.fail_leg = None
        second.control('continue', task_id='restart-native-mock')
        self.assertEqual(second.status()['state'], 'succeeded')
        self.assertEqual(set(restored.vla._commands), original)
        self.assertEqual([b['leg_index'] for b in self.nav.submits], [1,2,3,3,4,5])

    def test_new_native_simulation_cancel_stays_cancelled_and_missing_receipt_stays_unknown(self):
        skill = load_skill(self.config)
        nav, opening = [s for s in skill.real_steps if s.kind not in {'local','verify'}][:2]
        policy = PackagePolicy()
        self.nav.submit_navigation(policy.request(nav,'task','nav',lambda _:'')['body'])
        request = policy.request(opening,'task','open',lambda _:'nav')
        self.mock.delay_sec = 60
        self.adapter.submit('open',request)
        restored = HybridReceptionAdapter(self.nav, SimulatedManipulationClient(self.nav))
        self.assertTrue(restored.query('open',request)['timed_out'])
        self.adapter.cancel('open',request)
        self.mock._ready_at['open'] = 0
        result = self.adapter.wait('open',request)
        self.assertEqual(result['terminal'],'cancelled')
        self.assertFalse(result['evidence']['supports'])
        self.assertNotIn('vla_confirmation',result['evidence'])

    def test_cancelled_simulation_cannot_turn_into_late_success(self):
        request = self.pick_request()
        self.mock.delay_sec = 60
        self.adapter.submit("pick", request)
        self.assertFalse(self.adapter.query("pick", request)["terminal"])
        self.adapter.cancel("pick", request)
        self.mock._ready_at["pick"] = 0
        observed = self.adapter.wait("pick", request)
        self.assertEqual(observed["terminal"], "cancelled")
        self.assertFalse(observed["evidence"]["supports"])
        self.assertTrue(observed["stopped"])

    def test_missing_receipt_after_restart_stays_unknown(self):
        request = self.pick_request()
        self.adapter.submit("pick", request)
        restored = HybridReceptionAdapter(self.nav, SimulatedManipulationClient(self.nav))
        result = restored.query("pick", request)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["terminal"])
        self.assertEqual(len(restored.vla._commands), 0)

    def test_mock_rejects_foreign_navigation_proof_and_conflicting_identity(self):
        request = self.pick_request()
        self.nav.commands["nav"]["task_id"] = "foreign"
        refusal = self.adapter.submit("pick", request)
        self.assertFalse(refusal["accepted"])
        self.assertFalse(refusal["unclear"])
        self.assertEqual(refusal["submission_rejected"]["code"], "NAVIGATION_PROOF_INVALID")
        self.nav.commands["nav"]["task_id"] = "task"
        self.assertTrue(self.adapter.submit("pick", request)["accepted"])
        self.assertTrue(self.adapter.submit("pick", request)["accepted"])
        changed = deepcopy(request["body"])
        changed["task_id"] = "other"
        with self.assertRaises(HttpContractError):
            self.mock.submit_task(changed)


if __name__ == "__main__":
    unittest.main()
