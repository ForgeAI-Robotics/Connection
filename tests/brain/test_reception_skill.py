"""Declarative flow checks, snapshot isolation, and Runtime compatibility."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from brain.app import create_service
from brain.packages.reception_skill import BASELINE_PATH, compile_skill, from_snapshot, load_skill, validate_backend
from brain.packages.registry import PackagePolicy
from contracts.steps import step_spec_dict, steps_from_specs
from contracts.tasks import Rejected
from tests.brain.test_kernel_reception import FakeBody


class ReceptionDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.definition = load_skill().definition

    def test_definition_generates_original_wire_bodies(self):
        golden = json.loads((Path(__file__).parents[1] / "contracts/reception_wire.json").read_text())
        policy = PackagePolicy()
        steps = {step.step_id: step for step in compile_skill(self.definition).steps}
        for sample in golden:
            body = sample["body"]
            actual = policy.request(steps[sample["step_id"]], body["task_id"], body["command_id"],
                                    lambda _: "nav-proof-baseline0001")
            self.assertEqual(actual["body"], body)

    def test_handoffs_and_required_evidence_are_derived(self):
        skill = compile_skill(self.definition)
        pick = next(step for step in skill.steps if step.kind == "pick")
        place = next(step for step in skill.steps if step.kind == "place")
        self.assertTrue(pick.requires_object_evidence)
        self.assertTrue(place.requires_object_evidence)
        self.assertTrue(place.requires_safe_idle)
        self.assertEqual(pick.handoff_before, "to_vla")
        self.assertEqual([step.handoff_before for step in skill.steps if step.kind == "navigate"],
                         ["", "to_nav", "", ""])
        self.assertEqual([step.kind for step in skill.phases(nav_only=True)],
                         ["local", "local", "navigate", "navigate", "navigate", "navigate"])
        self.assertFalse(any(step.handoff_before for step in skill.phases(nav_only=True)))

    def test_invalid_definitions_fail_before_any_action(self):
        mutations = {
            "unknown_action": lambda d: d["steps"][2].update(action="open_door"),
            "unknown_field": lambda d: d["steps"][2].update(requires_object_evidence=False),
            "duplicate_id": lambda d: d["steps"][2].update(id=d["steps"][0]["id"]),
            "duplicate_prefix": lambda d: d["steps"][2].update(command_prefix=d["steps"][0]["command_prefix"]),
            "missing_point": lambda d: d["steps"][0].update(point="fridge"),
            "future_proof": lambda d: d["steps"][2].update(after_navigation="NAVIGATING_TO_TABLE1"),
            "wrong_proof": lambda d: d["steps"][-1].update(after_navigation="NAVIGATING_TO_TABLE2"),
            "unknown_object": lambda d: d["parameters"].update(object_id="water"),
            "multiple_objects": lambda d: d["parameters"].update(quantity=2),
            "bool_quantity": lambda d: d["parameters"].update(quantity=True),
            "unapproved_pose": lambda d: d["points"]["table2"].update(goal_xyt=[1, 2, 3]),
            "unapproved_target": lambda d: d["points"]["table2"].update(target_id="fridge"),
            "nan_pose": lambda d: d["points"]["table2"].update(goal_xyt=[float("nan"), 2, 3]),
            "negative_timeout": lambda d: d["steps"][0].update(timeout_sec=-1),
            "infinite_timeout": lambda d: d["steps"][0].update(timeout_sec=float("inf")),
            "skip_pick": lambda d: d["steps"][2].update(optional=True),
            "missing_place": lambda d: d["steps"].pop(),
            "unsupported_contract": lambda d: d.update(contract_version="future/v2"),
            "invalid_action_type": lambda d: d["steps"][0].update(action=[]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                definition = deepcopy(self.definition)
                mutate(definition)
                with self.assertRaises(Rejected):
                    compile_skill(definition)
        wrong_order = deepcopy(self.definition)
        wrong_order["steps"][3], wrong_order["steps"][4] = wrong_order["steps"][4], wrong_order["steps"][3]
        with self.assertRaises(Rejected):
            compile_skill(wrong_order)

    def test_duplicate_yaml_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "skill.yaml"
            path.write_text("schema_version: 1\nschema_version: 2\n")
            with self.assertRaises(Rejected):
                load_skill(path=path)

    def test_snapshot_integrity_and_step_roundtrip(self):
        skill = compile_skill(self.definition)
        snapshot = skill.snapshot()
        self.assertEqual(from_snapshot(snapshot).steps, skill.steps)
        snapshot["definition"]["steps"][0]["timeout_sec"] = 12
        with self.assertRaises(Rejected):
            from_snapshot(snapshot)
        restored = steps_from_specs(json.loads(json.dumps([step_spec_dict(s) for s in skill.steps])))
        self.assertEqual(restored, list(skill.steps))
        # Copying a plan and editing the source do not mutate its pinned inputs.
        self.definition["points"]["table2"]["goal_xyt"][0] = 99
        phases = skill.phases()
        phases[2].navigation["goal_xyt"][0] = 88
        self.assertNotIn(skill.steps[2].navigation["goal_xyt"][0], (88, 99))

    def test_new_route_exact_coordinates_and_unchanged_manipulation(self):
        skill = load_skill(path=BASELINE_PATH.with_name("reception_single_can_v2.yaml"))
        nav = [s for s in skill.steps if s.kind == "navigate"]
        self.assertEqual([s.navigation["goal_xyt"] for s in nav], [
            [-3.658864, 4.424073, 0.178434], [-2.248818, -0.541162, 1.476931],
            [-2.423402, -6.008235, -3.021833], [-2.168660, -7.378595, -2.981830],
            [-0.773222, -7.528610, 1.823351]])
        self.assertEqual([s.navigation["leg_index"] for s in nav], [1, 2, 3, 4, 5])
        self.assertEqual([s.navigation["motion_mode"] for s in nav],
                         ["forward_path"] * 3 + ["lateral_path_aligned", "forward_path"])
        self.assertEqual([s.handoff_before for s in nav], ["", "to_nav", "", "", ""])
        policy = PackagePolicy()
        for kind in ("pick", "place"):
            old = next(s for s in load_skill().steps if s.kind == kind)
            new = next(s for s in skill.steps if s.kind == kind)
            self.assertEqual(new, old)
            self.assertEqual(policy.request(old, "task", "cmd", lambda _: "proof"),
                             policy.request(new, "task", "cmd", lambda _: "proof"))
        self.assertEqual(len(skill.phases(nav_only=True)), 7)

    def test_configurable_route_validation_and_legacy_backend_boundary(self):
        skill = load_skill(path=BASELINE_PATH.with_name("reception_single_can_v2.yaml"))
        changed = deepcopy(skill.definition)
        changed["points"]["relay1"]["goal_xyt"] = [-2.1, -.6, 1.5]
        self.assertEqual(compile_skill(changed).steps[6].navigation["goal_xyt"], [-2.1, -.6, 1.5])
        for backend in ("reception_protocol",):
            validate_backend(load_skill(), backend)
            with self.assertRaisesRegex(Rejected, "只支持旧四段"):
                validate_backend(skill, backend)
        for mutation in (
                lambda d: d["points"]["relay1"].update(leg_index=True),
                lambda d: d["points"]["relay1"].update(leg_index=6),
                lambda d: d["points"]["relay1"].update(goal_xyt=[True, 2, 3]),
                lambda d: d["points"]["relay1"].update(goal_xyt=[1, float("inf"), 0]),
                lambda d: d["points"]["relay1"].update(motion_mode="teleport"),
                lambda d: d["points"]["relay1"].update(motion_mode="lateral_path_aligned"),
                lambda d: d["points"]["relay2"].update(route_phase="ordinary"),
                lambda d: d["points"]["relay1"].update(route_phase={})):
            candidate = deepcopy(skill.definition)
            mutation(candidate)
            with self.assertRaises(Rejected):
                compile_skill(candidate)


class ReceptionDefinitionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "skill.yaml"
        self.definition = load_skill().definition
        self.write()
        self.config = {"brain": {"capture_timeline": False}, "reflection": {"enabled": False},
                       "reception_real": {"kernel_runtime_dir": str(Path(self.temp.name) / "tasks"),
                                          "skill_file": str(self.path), "dream_inspection_enabled": False}}
        self.body = FakeBody()
        self.ports = []
        for name, fn in (("select_backend", lambda *a, **kw: "reception_real"),
                         ("target_identity", lambda c, backend: {"backend": backend})):
            patcher = patch("brain.service." + name, side_effect=fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self):
        self.path.write_text(yaml.safe_dump(self.definition, allow_unicode=True), encoding="utf-8")

    def service(self, model=None):
        def factory(backend):
            self.ports.append(backend)
            return self.body
        def unexpected_model(_):
            raise AssertionError("固定触发词不得要求模型重新生成流程")
        return create_service(self.config, model=model or unexpected_model, port_factory=factory)

    def test_renamed_steps_and_point_keys_execute_without_hardcoded_proofs(self):
        self.definition["sop"].update(id="reception.reference", version="test")
        names = {item["id"]: "STEP_" + str(i) for i, item in enumerate(self.definition["steps"])}
        self.definition["points"]["pickup"] = self.definition["points"].pop("table2")
        self.definition["steps"][0]["point"] = "pickup"
        for item in self.definition["steps"]:
            item["id"] = names[item["id"]]
            if "after_navigation" in item:
                item["after_navigation"] = names[item["after_navigation"]]
        self.write()
        service = self.service()
        service.publish("开始接待", "renamed")
        record = service.runtime.record
        self.assertEqual(record["state"], "succeeded")
        self.assertEqual(record["object_location"], "table_1")
        self.assertEqual(len(self.body.submits), 6)
        self.assertEqual(record["selection"]["sop"]["version"], "test")
        for item in self.definition["steps"]:
            if item["action"] not in {"pick", "place"}:
                continue
            attempt = record["steps"][item["id"]]["attempts"][-1]
            nav_attempt = record["steps"][item["after_navigation"]]["attempts"][-1]
            self.assertEqual(attempt["request"]["body"]["navigation_proof"]["dream_command_id"], nav_attempt["command_id"])

    def test_model_wait_pins_definition_and_new_tasks_validate_new_file(self):
        def model(_):
            self.definition["points"]["table2"]["goal_xyt"] = [99, 99, 0]
            self.write()
            return json.dumps({"package": "reception", "scope_match": True, "reason": "单罐接待"})
        service = self.service(model)
        service.publish("把二号桌的一罐可乐送到一号桌", "pinned")
        self.assertEqual(service.status()["state"], "succeeded")
        self.assertEqual(len(self.body.submits), 6)
        pinned = service.runtime.record["selection"]["workflow"]
        self.assertNotEqual(pinned["definition"]["points"]["table2"]["goal_xyt"], [99, 99, 0])
        service.publish("开始接待", "invalid-next")
        self.assertEqual(service.status()["state"], "recovery_required")
        self.assertEqual(len(self.body.submits), 6)
        self.assertEqual(len(self.ports), 1)

    def test_restart_uses_saved_plan_even_if_skill_file_is_gone(self):
        self.body.gate_open = False
        service = self.service()
        service.publish("开始接待", "restart")
        self.assertEqual(service.status()["state"], "waiting_human")
        snapshot = service.runtime.record["selection"]["workflow"]
        self.path.unlink()
        self.body.gate_open = True
        restored = self.service()
        restored.control("continue", task_id="restart")
        self.assertEqual(restored.status()["state"], "succeeded")
        self.assertEqual(restored.runtime.record["selection"]["workflow"], snapshot)
        self.assertEqual(len(self.body.submits), 6)

    def test_custom_version_navigation_only_does_not_dispatch_manipulation(self):
        self.definition["sop"].update(id="reception.reference", version="test")
        self.write()
        service = self.service()
        service.publish("开始接待（仅导航）", "navigation")
        self.assertEqual(service.status()["state"], "succeeded")
        self.assertEqual(service.runtime.record["selection"]["sop"]["id"], "reception.reference.nav_only")
        self.assertEqual(len(self.body.submits), 4)
        self.assertEqual(self.body.handoff_queries, [])

    def use_new_route(self):
        self.definition = load_skill(path=BASELINE_PATH.with_name("reception_single_can_v2.yaml")).definition
        self.write()

    def test_five_leg_reception_and_navigation_only_execute_without_model(self):
        self.use_new_route()
        service = self.service()
        service.publish("开始接待", "new-full")
        record = service.runtime.record
        self.assertEqual(record["state"], "succeeded")
        self.assertEqual(record["object_location"], "table_1")
        self.assertEqual(len(self.body.submits), 7)
        self.assertEqual(record["selection"]["source"], "registered_trigger")
        self.assertEqual(record["selection"]["sop"]["version"], "2")
        order = record["phase_order"]
        self.assertLess(order.index("VLA_PICKING"), order.index("NAVIGATING_TO_RELAY1"))
        self.assertLess(order.index("NAVIGATING_TO_RELAY1"), order.index("NAVIGATING_TO_RELAY2"))
        self.body.handoff_queries.clear()
        service.publish("开始接待（仅导航）", "new-nav")
        self.assertEqual(service.status()["state"], "succeeded")
        self.assertEqual(len(self.body.submits), 12)
        self.assertEqual(self.body.handoff_queries, [])

    def test_new_route_is_rejected_before_opening_legacy_simulator(self):
        self.use_new_route()
        for backend in ("reception_protocol",):
            with patch("brain.service.select_backend", return_value=backend):
                service = self.service()
                service.publish("开始接待", backend)
                self.assertEqual(service.status()["state"], "recovery_required")
                self.assertIn("只支持旧四段", service.runtime.record["blocked_reason"])
                service.control("cancel", task_id=backend)
        self.assertEqual(self.ports, [])
        self.assertEqual(self.body.submits, [])

    def test_relay1_failure_continue_keeps_pinned_route_and_original_attempt(self):
        self.use_new_route()
        self.config["brain"]["reasoning"] = {"recovery_advice": False}
        self.body.outcomes["NAVIGATING_TO_RELAY1"] = {
            "terminal": "failed", "started": True, "stopped": True, "resources_released": True}
        service = self.service()
        service.publish("开始接待", "relay-recovery")
        self.assertEqual(service.status()["state"], "recovery_required")
        self.assertEqual(len(self.body.submits), 3)
        original = self.body.submits[-1]
        # The next task may use changed parameters; this task must keep its snapshot.
        self.definition["points"]["relay1"]["goal_xyt"] = [10, 10, 0]
        self.write()
        self.body.outcomes.clear()
        service.control("continue", task_id="relay-recovery")
        self.assertEqual(service.status()["state"], "succeeded")
        attempts = service.runtime.record["steps"]["NAVIGATING_TO_RELAY1"]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["command_id"], original)
        self.assertNotEqual(attempts[1]["command_id"], original)
        self.assertEqual(attempts[1]["request"]["body"]["goal_xyt"], [-2.248818, -0.541162, 1.476931])
