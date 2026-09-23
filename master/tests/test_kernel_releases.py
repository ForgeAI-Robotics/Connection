"""A candidate is proven against its own skill, then published as a package snapshot."""

import hashlib
import os
import sys
import tempfile
import threading
import unittest


MASTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.abspath(os.path.join(MASTER_DIR, ".."))
for _path in (ROOT, MASTER_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from kernel.adapters import interpret_observation
from kernel.contracts import CONTRACT_VERSION, SkillContract, StaleWrite
from kernel.packages.reception import navigation_body
from kernel.releases import ReleaseError, ReleaseLedger
from kernel.runtime import TaskRuntime
from kernel.store import KernelStore
from kernel.verifier import Verifier
from sop.episode import episode_from_reception_trace, episode_from_steps
from sop.reflect import _TEMPLATE_RULES, maybe_reflect


def _episode(task_id, steps, *, final="failure"):
    return episode_from_steps(
        task_id,
        "开始接待",
        steps,
        task_type="reception",
        backend="real",
        final=final,
    )


def _fail(phase, detail):
    return {
        "phase": phase,
        "status": "failure",
        "verify_ok": False,
        "detail": detail,
        "verify_detail": detail,
    }


def _triggered_pick():
    return [
        _fail("VLA_PICKING", "未抓住"),
        _ok("重新观测抓取", "核对后手里有物体"),
    ]


def _triggered_nav():
    return [
        _fail("walk", "导航被挡住"),
        _ok("导航复核", "重新确认到达"),
    ]


def _ok(phase, detail, **extra):
    step = {
        "phase": phase,
        "status": "success",
        "verify_ok": True,
        "detail": detail,
        "verify_detail": detail,
    }
    step.update(extra)
    return step


class RecordingPort:
    contract_version = CONTRACT_VERSION
    gate_open = True

    def __init__(self):
        self.submits = []

    def submit(self, command_id, request):
        self.submits.append((command_id, request))
        return {"accepted": True}

    def wait(self, command_id, request):
        return {
            "command_id": command_id,
            "terminal": "succeeded",
            "stopped": True,
            "started": True,
            "evidence": {
                "supports": True,
                "contradicts": False,
                "identity_ok": True,
                "time_ok": True,
                "grade": "object",
                "identity": command_id,
            },
        }

    def query(self, command_id, request):
        return {"terminal": None, "evidence": {}}

    def handoff(self, step_id, kind):
        return {"available": False, "confirmed": False}


class ReleaseTests(unittest.TestCase):
    def test_rule_is_scored_and_retry_from_reception_trace_is_excluded(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = ReleaseLedger(root)
            pick_source = _episode("src-pick", [_fail("VLA_PICKING", "未抓住")])
            nav_source = _episode("src-nav", [_fail("walk", "导航被挡住")])
            pick = ledger.add_candidate(
                rule=_TEMPLATE_RULES["抓取失败"],
                findings=[{"type": "抓取失败", "detail": "未抓住"}],
                package="reception",
                skill="pick",
                source_episode=pick_source,
            )
            nav = ledger.add_candidate(
                rule=_TEMPLATE_RULES["导航受阻"],
                findings=[{"type": "导航受阻", "detail": "挡住了"}],
                package="reception",
                skill="navigate",
                source_episode=nav_source,
            )
            plain = _episode(
                "proof-pick",
                [_ok("VLA_PICKING", "抓住")],
                final="success",
            )
            pick_plain = ledger.shadow(pick["candidate_id"], [plain], RecordingPort())
            nav_plain = ledger.shadow(nav["candidate_id"], [plain], RecordingPort())
            self.assertFalse(pick_plain["passed"])
            self.assertEqual(pick_plain["reason"], "样本没有触发这条规则的失败条件")
            self.assertEqual(pick_plain["submits"], 0)
            self.assertFalse(nav_plain["passed"])
            triggered = _episode("proof-triggered", _triggered_pick(), final="success")
            pick_report = ledger.evaluate(pick["candidate_id"], [triggered])
            nav_report = ledger.evaluate(nav["candidate_id"], [triggered])
            self.assertTrue(pick_report["passed"])
            self.assertEqual(pick_report["skill"], "pick")
            self.assertFalse(nav_report["passed"])
            self.assertEqual(nav_report["matched_episodes"], 0)
            reversed_episode = _episode(
                "reversed",
                list(reversed(_triggered_pick())),
                final="failure",
            )
            reversed_report = ledger.evaluate(pick["candidate_id"], [reversed_episode])
            self.assertFalse(reversed_report["passed"])
            self.assertEqual(reversed_report["reason"], "核验成功出现在触发之前")

            bare = _episode("bare", [{"status": "success", "verify_ok": True}], final="success")
            self.assertFalse(ledger.evaluate(pick["candidate_id"], [bare])["passed"])
            with self.assertRaises(ReleaseError):
                ledger.add_candidate(
                    rule="抓取失败后直接进入下一段导航",
                    findings=[{"type": "抓取失败", "detail": "未抓住"}],
                    package="reception",
                    skill="pick",
                    source_episode=pick_source,
                )

            retried = episode_from_reception_trace({
                "task_id": "trace-retry",
                "task": "开始接待",
                "rounds": [{
                    "attempts": [
                        {"steps": [{
                            "step": "pick",
                            "exec": {"success": False, "fail_reason": "未抓住"},
                            "verify_ok": False,
                            "verify_detail": "未抓住",
                        }]},
                        {"steps": [{
                            "step": "pick",
                            "exec": {"success": True},
                            "verify_ok": True,
                            "verify_detail": "抓住",
                        }]},
                    ],
                }],
                "final_check": {"verdict": True},
            })
            retry_report = ledger.evaluate(pick["candidate_id"], [retried])
            self.assertFalse(retry_report["passed"])
            self.assertEqual(retry_report["autonomous_success"], 0)
            self.assertGreaterEqual(retry_report["excluded"], 1)

    def test_success_recap_and_false_claim_cannot_publish(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = ReleaseLedger(root)
            success = _episode("ok", [_ok("VLA_PICKING", "抓住")], final="success")
            with self.assertRaises(ReleaseError):
                ledger.add_candidate(
                    rule="全部成功",
                    findings=[{"type": "顺利完成", "detail": "记录的步骤均成功结束"}],
                    package="reception",
                    source_episode=success,
                )
            source = _episode("src-lie", [{
                "phase": "VLA_PICKING",
                "status": "failure",
                "claimed_ok": True,
                "verify_ok": False,
                "detail": "桌上没有可乐",
                "verify_detail": "桌上没有可乐",
            }])
            row = ledger.add_candidate(
                rule=_TEMPLATE_RULES["执行报告与实际不符"],
                findings=[{"type": "执行报告与实际不符", "detail": "桌上没有可乐"}],
                package="reception",
                skill="pick",
                source_episode=source,
            )
            claimed = _episode(
                "claimed",
                [_ok("VLA_PICKING", "抓住", claimed_ok=True)],
                final="success",
            )
            unclaimed = _episode("plain", [_ok("VLA_PICKING", "抓住")], final="success")
            self.assertFalse(ledger.evaluate(row["candidate_id"], [claimed])["passed"])
            self.assertEqual(
                ledger.evaluate(row["candidate_id"], [unclaimed])["reason"],
                "样本没有触发这条规则的失败条件",
            )
            rechecked = _episode(
                "rechecked",
                [
                    {
                        "phase": "VLA_PICKING",
                        "status": "failure",
                        "claimed_ok": True,
                        "verify_ok": False,
                        "detail": "桌上没有可乐",
                        "verify_detail": "桌上没有可乐",
                    },
                    _ok("重新观测抓取", "核对后手里有物体", claimed_ok=True),
                ],
                final="success",
            )
            self.assertTrue(ledger.evaluate(row["candidate_id"], [rechecked])["passed"])
            lied = _episode("lied", [{
                "phase": "VLA_PICKING",
                "status": "failure",
                "claimed_ok": True,
                "verify_ok": False,
                "detail": "桌上没有可乐",
            }])
            lied["task_id"] = "lied"
            report = ledger.evaluate(row["candidate_id"], [lied])
            self.assertFalse(report["passed"])
            self.assertEqual(report["autonomous_success"], 0)
            with self.assertRaises(ReleaseError):
                ledger.publish(row["candidate_id"])

    def test_snapshot_reaches_the_next_execution_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = ReleaseLedger(root)
            pick_source = _episode("src-pick", [_fail("VLA_PICKING", "未抓住")])
            nav_source = _episode("src-nav", [_fail("walk", "导航被挡住")])
            pick = ledger.add_candidate(
                rule=_TEMPLATE_RULES["抓取失败"],
                findings=[{"type": "抓取失败", "detail": "未抓住"}],
                package="reception",
                source_episode=pick_source,
            )
            nav = ledger.add_candidate(
                rule=_TEMPLATE_RULES["导航受阻"],
                findings=[{"type": "导航受阻", "detail": "挡住了"}],
                package="reception",
                source_episode=nav_source,
            )
            ledger.shadow(pick["candidate_id"], [_episode(
                "proof-pick", _triggered_pick(), final="success",
            )])
            ledger.publish(pick["candidate_id"])
            ledger.shadow(nav["candidate_id"], [_episode(
                "proof-nav", _triggered_nav(), final="success",
            )])
            published = ledger.publish(nav["candidate_id"])
            texts = [item["text"] for item in published["rules"]]
            self.assertEqual(texts, [_TEMPLATE_RULES["抓取失败"], _TEMPLATE_RULES["导航受阻"]])

            config = {
                "reflection": {"release_dir": root},
                "reception_real": {"dream_inspection_enabled": False},
            }
            port = RecordingPort()
            opened = TaskRuntime(KernelStore(os.path.join(root, "task-a")), port, config=config)
            opened.open_task("task-a")
            self.assertEqual(opened.record["release_id"], "rel-2")
            opened.drive()
            body = port.submits[0][1]["body"]
            frozen = navigation_body(
                type("Step", (), {"key": "table2"})(),
                "task-a",
                body["command_id"],
            )
            self.assertEqual(set(body), set(frozen))
            for banned in ("skill_version", "prompt", "checks", "distrust_claim"):
                self.assertNotIn(banned, body)
            attempt = opened.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][0]
            self.assertEqual(set(attempt["request"]["body"]), set(frozen))
            self.assertEqual(attempt["contract"]["bound_rules"][0]["evidence"], "reached")
            self.assertEqual(opened.state, "recovery_required")

            ledger.rollback("reception")
            self.assertEqual(
                [item["text"] for item in ledger.active("reception")["rules"]],
                [_TEMPLATE_RULES["抓取失败"]],
            )
            self.assertEqual(opened.record["release_id"], "rel-2")
            later_port = RecordingPort()
            later = TaskRuntime(KernelStore(os.path.join(root, "task-b")), later_port, config=config)
            later.open_task("task-b")
            later.drive()
            later_body = later_port.submits[0][1]["body"]
            self.assertEqual(set(later_body), set(frozen))
            later_attempt = later.record["steps"]["NAVIGATING_TO_TABLE2"]["attempts"][0]
            self.assertEqual(later_attempt["contract"]["bound_rules"], [])
            self.assertEqual(later.record["release_id"], "rel-1")
            again = ledger.add_candidate(
                rule=_TEMPLATE_RULES["抓取失败(重试后恢复)"],
                findings=[{"type": "抓取失败(重试后恢复)", "detail": "未抓住"}],
                package="reception",
                source_episode=pick_source,
            )
            ledger.shadow(again["candidate_id"], [_episode(
                "proof-again", _triggered_pick(), final="success",
            )])
            third = ledger.publish(again["candidate_id"])
            self.assertEqual(third["parent_id"], "rel-1")
            self.assertEqual(third["version_id"], "rel-3")
            self.assertEqual(ledger.rollback("reception")["version_id"], "rel-1")

    def test_bound_rule_changes_verifier_and_adapter_names_the_effect(self):
        verifier = Verifier()
        contract = SkillContract(
            package="reception",
            skill="navigate",
            version=CONTRACT_VERSION,
            task_id="task-abcdef123456",
            step_id="NAVIGATING_TO_TABLE2",
            attempt_id="NAVIGATING_TO_TABLE2-a1",
            goal="table2",
            object_id="",
            preconditions="",
            evidence="reached",
            failure_budget=0,
            deadline_sec=30,
            command_prefix="nav-table2",
            command_id="nav-table2-abcdef123456",
            postconditions=("reached",),
            handoff="",
            tools=("navigate",),
            requires_object_evidence=False,
            requires_safe_idle=False,
            request={"body": {"contract_version": CONTRACT_VERSION, "command_id": "nav-table2-abcdef123456"}},
            bound_rules=({"evidence": "reached", "distrust_claim": True},),
        )
        self.assertNotIn("checks", contract.request["body"])
        self.assertNotIn("distrust_claim", contract.request["body"])
        from kernel.contracts import ProgressEvent

        base = dict(
            task_id="task-abcdef123456",
            step_id="NAVIGATING_TO_TABLE2",
            attempt_id="NAVIGATING_TO_TABLE2-a1",
            command_id="nav-table2-abcdef123456",
            phase="NAVIGATING_TO_TABLE2",
            progress="succeeded",
            evidence_ref="NAVIGATING_TO_TABLE2-a1.jpg",
            action_ended=True,
            effect_ok=True,
            publisher_stopped=True,
            handoff_confirmed=False,
            timed_out=False,
            terminal="succeeded",
            safe_idle=True,
            error="",
        )
        missing = {
            "supports": True,
            "contradicts": False,
            "identity_ok": True,
            "time_ok": True,
            "grade": "",
            "identity": "nav-table2-abcdef123456",
        }
        self.assertEqual(verifier.judge(contract, ProgressEvent(evidence=missing, **base)), "FAIL")
        named = dict(missing)
        named["effect"] = "reached"
        self.assertEqual(verifier.judge(contract, ProgressEvent(evidence=named, **base)), "UNKNOWN")
        named["claimed_ok"] = False
        self.assertEqual(verifier.judge(contract, ProgressEvent(evidence=named, **base)), "FAIL")
        named["claimed_ok"] = True
        self.assertEqual(verifier.judge(contract, ProgressEvent(evidence=named, **base)), "PASS")
        unbound = SkillContract(**{**contract.__dict__, "request": {}, "bound_rules": ()})
        self.assertEqual(verifier.judge(unbound, ProgressEvent(evidence=missing, **base)), "PASS")

        raw = {
            "contract_version": CONTRACT_VERSION,
            "task_id": "task-abcdef123456",
            "command_id": "nav-table2-abcdef123456",
            "target_id": "table_2",
            "route_phase": "",
            "leg_index": 1,
            "state": "succeeded",
            "completed_at": "2026-08-27T14:00:00.000+08:00",
            "result": {"success": True, "reached": True, "navigation_stopped": True},
        }
        request = {
            "skill": "navigate",
            "task_id": "task-abcdef123456",
            "body": {
                "contract_version": CONTRACT_VERSION,
                "command_id": "nav-table2-abcdef123456",
                "task_id": "task-abcdef123456",
                "target_id": "table_2",
                "route_phase": "",
                "leg_index": 1,
            },
        }
        viewed = interpret_observation("nav-table2-abcdef123456", raw, request)
        self.assertEqual(viewed["evidence"]["effect"], "reached")
        self.assertNotIn("claimed_ok", viewed["evidence"])
        distrust = SkillContract(**{
            **contract.__dict__,
            "bound_rules": ({"evidence": "reached", "distrust_claim": True},),
        })
        self.assertEqual(
            verifier.judge(distrust, ProgressEvent(evidence=viewed["evidence"], **base)),
            "UNKNOWN",
        )

    def test_reflection_log_becomes_an_unevaluated_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            reflection = os.path.join(root, "reflections")
            release = os.path.join(root, "releases")
            old_dir = os.environ.get("FQPLANNER_REFLECTION_DIR")
            old_llm = os.environ.get("FQPLANNER_REFLECTION_LLM")
            os.environ["FQPLANNER_REFLECTION_DIR"] = reflection
            os.environ["FQPLANNER_REFLECTION_LLM"] = "off"
            try:
                episode = _episode("reflect-1", [_fail("VLA_PICKING", "未抓住")])
                maybe_reflect(
                    episode,
                    config={
                        "reflection": {
                            "write_sop": False,
                            "use_llm": False,
                            "release_dir": release,
                        },
                    },
                    quiet=True,
                )
                rows = ReleaseLedger(release)._load()["candidates"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "unevaluated")
                self.assertEqual(rows[0]["rule"], _TEMPLATE_RULES["抓取失败"])
                self.assertEqual(rows[0]["source_task_id"], "reflect-1")
                success = _episode("reflect-ok", [_ok("VLA_PICKING", "抓住")], final="success")
                maybe_reflect(
                    success,
                    config={"reflection": {"write_sop": False, "use_llm": False, "release_dir": release}},
                    quiet=True,
                )
                self.assertEqual(len(ReleaseLedger(release)._load()["candidates"]), 1)
            finally:
                if old_dir is None:
                    os.environ.pop("FQPLANNER_REFLECTION_DIR", None)
                else:
                    os.environ["FQPLANNER_REFLECTION_DIR"] = old_dir
                if old_llm is None:
                    os.environ.pop("FQPLANNER_REFLECTION_LLM", None)
                else:
                    os.environ["FQPLANNER_REFLECTION_LLM"] = old_llm

    def test_publish_does_not_edit_kernel_files(self):
        runtime_path = os.path.join(MASTER_DIR, "kernel", "runtime.py")
        with open(runtime_path, "rb") as handle:
            before = hashlib.sha256(handle.read()).hexdigest()
        with tempfile.TemporaryDirectory() as root:
            ledger = ReleaseLedger(root)
            row = ledger.add_candidate(
                rule=_TEMPLATE_RULES["抓取失败"],
                findings=[{"type": "抓取失败", "detail": "未抓住"}],
                package="reception",
                source_episode=_episode("src", [_fail("VLA_PICKING", "未抓住")]),
            )
            ledger.shadow(row["candidate_id"], [_episode(
                "proof", _triggered_pick(), final="success",
            )])
            ledger.publish(row["candidate_id"])
            self.assertEqual(set(os.listdir(root)), {"releases.json", "writer.lock"})
        with open(runtime_path, "rb") as handle:
            after = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(before, after)

    def test_concurrent_writes_and_stale_revision(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = ReleaseLedger(root)
            source = _episode("src-pick", [_fail("VLA_PICKING", "未抓住")])
            errors = []

            def add_one():
                try:
                    ledger.add_candidate(
                        rule=_TEMPLATE_RULES["抓取失败"],
                        findings=[{"type": "抓取失败", "detail": "未抓住"}],
                        package="reception",
                        source_episode=source,
                    )
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=add_one) for _ in range(10)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertEqual(errors, [])
            rows = ledger._load()["candidates"]
            self.assertEqual(len(rows), 10)
            self.assertEqual(len({row["candidate_id"] for row in rows}), 10)

            stale = ledger._load()
            ledger.add_candidate(
                rule=_TEMPLATE_RULES["抓取失败"],
                findings=[{"type": "抓取失败", "detail": "未抓住"}],
                package="reception",
                source_episode=source,
            )
            with ledger._writer():
                with self.assertRaises(StaleWrite):
                    ledger._commit_unlocked(stale)
            self.assertEqual(len(ledger._load()["candidates"]), 11)

    def test_shadow_report_and_status_stay_together(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = ReleaseLedger(root)
            source = _episode("src-pick", [_fail("VLA_PICKING", "未抓住")])
            row = ledger.add_candidate(
                rule=_TEMPLATE_RULES["抓取失败"],
                findings=[{"type": "抓取失败", "detail": "未抓住"}],
                package="reception",
                source_episode=source,
            )
            passed = _episode("after", _triggered_pick(), final="success")
            failed = _episode("before", list(reversed(_triggered_pick())), final="failure")
            errors = []

            def run(method, episodes):
                try:
                    method(row["candidate_id"], episodes)
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(ledger.shadow, [passed])),
                threading.Thread(target=run, args=(ledger.evaluate, [failed])),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertEqual(errors, [])
            saved = ledger._load()["candidates"][0]
            passed_flag = bool((saved.get("report") or {}).get("passed"))
            if saved["status"] == "shadow_passed":
                self.assertTrue(passed_flag)
                published = ledger.publish(row["candidate_id"])
                self.assertEqual(published["version_id"], "rel-1")
            else:
                self.assertFalse(passed_flag)
                with self.assertRaises(ReleaseError):
                    ledger.publish(row["candidate_id"])

            with ledger._writer():
                current = ledger._read_unlocked()
                current["candidates"][0]["status"] = "shadow_passed"
                current["candidates"][0]["report"] = {"passed": False, "submits": 0}
                ledger._commit_unlocked(current)
            with self.assertRaises(ReleaseError):
                ledger.publish(row["candidate_id"])


if __name__ == "__main__":
    unittest.main()
