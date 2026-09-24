"""Candidate rules stay unevaluated until a gate publishes a package snapshot.

A snapshot is the whole set of promoted skill rules for one package. Rollback
selects the previous snapshot. This module does not edit kernel source.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from connection.contracts.tasks import KernelError, StaleWrite
from connection.brain.storage.tasks import _gate_for
from connection.brain.packages.registry import phases_for
from connection.brain.skills.catalog import by_id, registered_ids


class ReleaseError(RuntimeError):
    pass


_HUMAN = {"human", "manual", "人工", "人工完成"}
_SUCCESS_FINDINGS = {"顺利完成", "观察完成"}
# finding type -> (skill, distrust_claim, evidence). Empty skill means the
# lying step in the source episode decides it.
_EFFECTS = {
    "抓取失败": ("pick", False, "object_held"),
    "抓取失败(重试后恢复)": ("pick", False, "object_held"),
    "导航受阻": ("navigate", False, "reached"),
    "导航受阻(重试后恢复)": ("navigate", False, "reached"),
    "执行报告与实际不符": ("", True, ""),
    "执行报告与实际不符(放偏谎报)": ("place", True, "object_at_target"),
    "摆放姿态不良(朝向/间隔)": ("place", False, "object_at_target"),
    "终局摆放复核不合格": ("place", False, "object_at_target"),
}
_FINDING_ALIASES = {
    "抓取失败(重试后恢复)": "抓取失败",
    "导航受阻(重试后恢复)": "导航受阻",
    "执行报告与实际不符(放偏谎报)": "执行报告与实际不符",
}


from connection.brain.storage.releases import bound_release


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _templates():
    from connection.brain.learning.reflection import _TEMPLATE_RULES, generic_findings

    return _TEMPLATE_RULES, generic_findings


def _package_name(episode) -> str:
    if not isinstance(episode, dict):
        return ""
    named = str(episode.get("package") or "").strip()
    if named:
        return named
    task_type = str(episode.get("task_type") or "").strip()
    if task_type in {"reception", "look"}:
        return task_type
    return ""


def _step_skill(step: dict) -> str:
    phase = str(step.get("phase") or "").lower()
    detail = str(step.get("detail") or step.get("verify_detail") or "").lower()
    blob = f"{phase} {detail}"
    if any(token in blob for token in ("vla_placing", "place", "摆放", "放置")):
        return "place"
    if any(token in blob for token in ("vla_picking", "pick", "grasp", "抓取", "夹爪")):
        return "pick"
    if any(token in blob for token in ("inspect", "检查")):
        return "inspect"
    if any(token in blob for token in ("navigat", "导航", "walk", "行走", "过门")):
        return "navigate"
    if any(token in blob for token in ("describe", "拍照", "视野")):
        return "describe"
    return ""


def _is_human(step: dict) -> bool:
    if step.get("human_completed") is True:
        return True
    return str(step.get("status") or "") in _HUMAN


def _is_failure(step: dict) -> bool:
    if step.get("verify_ok") is False:
        return True
    return str(step.get("status") or "") in {"failure", "exception", "timeout", "unknown"}


def _false_claim(step: dict) -> bool:
    return step.get("claimed_ok") is True and step.get("verify_ok") is False


def _retry_indexes(steps) -> set[int]:
    """A later step of a phase that already failed is a retry.

    Reception episodes flatten every attempt into ordinary steps and do not
    carry an attempts field. The second success of that phase is not autonomous.
    """
    failed = set()
    indexes = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        phase = str(step.get("phase") or "").strip()
        if phase and phase in failed:
            indexes.add(index)
        if phase and _is_failure(step):
            failed.add(phase)
        if step.get("retried") is True:
            indexes.add(index)
        try:
            if int(step.get("attempts") or 1) > 1:
                indexes.add(index)
        except (TypeError, ValueError):
            indexes.add(index)
    return indexes


def _attributed(finding_type: str, episode) -> bool:
    _templates_map, findings_of = _templates()
    del _templates_map
    produced = {item.get("type") for item in findings_of(episode)}
    if finding_type in produced:
        return True
    alias = _FINDING_ALIASES.get(finding_type)
    return alias in produced if alias else False


def _evidence_for(skill: str, evidence: str) -> str:
    if evidence:
        return evidence
    entry = by_id(skill)
    if entry is None:
        raise ReleaseError(f"没有这个技能: {skill}")
    return entry.verifies


class ReleaseLedger:
    def __init__(self, root: str):
        self.root = str(Path(root).resolve())

    @contextmanager
    def _writer(self):
        """Same directory gate as the task ledger: one flock, re-entrant per process."""
        root = Path(self.root)
        gate = _gate_for(root)
        with gate.thread_lock:
            if gate.depth == 0:
                root.mkdir(parents=True, exist_ok=True)
                fd = os.open(root / "writer.lock", os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except Exception:
                    os.close(fd)
                    raise
                gate.fd = fd
            gate.depth += 1
            try:
                yield
            finally:
                gate.depth -= 1
                if gate.depth == 0 and gate.fd is not None:
                    fcntl.flock(gate.fd, fcntl.LOCK_UN)
                    os.close(gate.fd)
                    gate.fd = None

    def add_candidate(
        self,
        *,
        rule: str,
        findings,
        package: str,
        skill: str = "",
        source_episode=None,
    ) -> dict:
        text = str(rule or "").strip()
        if not text:
            raise ReleaseError("候选规则为空")
        if not isinstance(source_episode, dict) or not source_episode.get("task_id"):
            raise ReleaseError("缺少来源 episode")
        templates, _findings_of = _templates()
        matched = self._match_finding(text, findings, source_episode, templates, skill)
        finding, finding_type, skill_id, distrust, evidence = matched
        name = str(package or "").strip()
        if _package_name(source_episode) != name:
            raise ReleaseError("来源 episode 不属于这个业务包")
        try:
            phases_for(name)
        except KernelError as exc:
            raise ReleaseError(str(exc)) from exc
        if skill_id not in registered_ids():
            raise ReleaseError(f"没有这个技能: {skill_id}")
        with self._writer():
            data = self._read_unlocked()
            candidate_id = f"cand-{len(data['candidates']) + 1}"
            row = self._append_candidate(
                data, candidate_id, text, finding, finding_type, name, skill_id,
                evidence, distrust, source_episode,
            )
            self._commit_unlocked(data)
        return row

    def _append_candidate(
        self, data, candidate_id, text, finding, finding_type, name, skill_id,
        evidence, distrust, source_episode,
    ) -> dict:
        row = {
            "candidate_id": candidate_id,
            "rule": text,
            "finding_type": finding_type,
            "findings": [dict(finding)],
            "package": name,
            "skill": skill_id,
            "evidence": evidence,
            "distrust_claim": distrust,
            "source_task_id": str(source_episode.get("task_id") or ""),
            "status": "unevaluated",
            "report": None,
        }
        data["candidates"].append(row)
        return row

    def evaluate(self, candidate_id: str, episodes) -> dict:
        with self._writer():
            data = self._read_unlocked()
            row = self._candidate(data, candidate_id)
            if row["status"] == "published":
                raise ReleaseError("已发布的版本不能重新评测")
            report = self._score(row, episodes)
            row["report"] = report
            row["status"] = "evaluated" if report["passed"] else "rejected"
            self._commit_unlocked(data)
        return report

    def shadow(self, candidate_id: str, episodes, port=None) -> dict:
        del port  # 影子运行只读记录，不向端口提交
        with self._writer():
            data = self._read_unlocked()
            row = self._candidate(data, candidate_id)
            if row["status"] == "published":
                raise ReleaseError("已发布的版本不能重新评测")
            report = self._score(row, episodes)
            report["submits"] = 0
            row["report"] = report
            row["status"] = "shadow_passed" if report["passed"] else "rejected"
            self._commit_unlocked(data)
        return report

    def publish(self, candidate_id: str) -> dict:
        with self._writer():
            data = self._read_unlocked()
            row = self._candidate(data, candidate_id)
            version = self._publish_unlocked(data, row, candidate_id)
            self._commit_unlocked(data)
        return version

    def _publish_unlocked(self, data, row, candidate_id: str) -> dict:
        report = row.get("report") or {}
        if row["status"] != "shadow_passed" or report.get("passed") is not True:
            raise ReleaseError("只有影子运行通过的候选可以发布")
        previous = None
        active_id = (data.get("active") or {}).get(row["package"]) or ""
        for item in data["versions"]:
            if item["version_id"] == active_id:
                previous = item
        rules = [dict(item) for item in (previous or {}).get("rules") or []]
        incoming = {
            "skill": row["skill"],
            "finding_type": row["finding_type"],
            "text": row["rule"],
            "evidence": row["evidence"],
            "distrust_claim": bool(row.get("distrust_claim")),
            "candidate_id": candidate_id,
        }
        for item in rules:
            if item.get("text") == incoming["text"]:
                raise ReleaseError("当前版本已经包含这条规则")
            if item.get("skill") == incoming["skill"] and item.get("evidence") != incoming["evidence"]:
                raise ReleaseError("与当前版本的技能证据要求矛盾")
        rules.append(incoming)
        version_id = f"rel-{len(data['versions']) + 1}"
        version = {
            "version_id": version_id,
            "package": row["package"],
            "parent_id": active_id,
            "rules": rules,
            "candidate_id": candidate_id,
            "published_at": _now(),
        }
        data["versions"].append(version)
        data["active"][row["package"]] = version_id
        row["status"] = "published"
        return version

    def rollback(self, package: str) -> dict:
        with self._writer():
            data = self._read_unlocked()
            current = data["active"].get(package) or ""
            version = None
            for item in data["versions"]:
                if item["version_id"] == current and item.get("package") == package:
                    version = item
            if version is None:
                raise ReleaseError("没有活动版本")
            parent = str(version.get("parent_id") or "")
            known = {item["version_id"] for item in data["versions"] if item.get("package") == package}
            if not parent or parent not in known:
                raise ReleaseError("已经是最早版本")
            data["active"][package] = parent
            self._commit_unlocked(data)
            active_id = parent
        for item in self._load()["versions"]:
            if item["version_id"] == active_id:
                return item
        raise ReleaseError("没有活动版本")

    def active(self, package: str) -> dict | None:
        data = self._load()
        version_id = (data.get("active") or {}).get(package) or ""
        for item in data["versions"]:
            if item["version_id"] == version_id:
                return item
        return None

    def ingest_log(self, log_path: str) -> list:
        if not os.path.isfile(log_path):
            return []
        added = []
        with open(log_path, encoding="utf-8") as handle:
            lines = handle.readlines()
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            path = str(row.get("path") or "")
            if not path or not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            episode = payload.get("episode")
            added.extend(self.ingest_reflection(episode, payload))
        return added

    def ingest_reflection(self, episode, result) -> list:
        if not isinstance(episode, dict) or not isinstance(result, dict):
            return []
        templates, _findings_of = _templates()
        findings = list(result.get("findings") or [])
        by_text = {}
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            text = templates.get(str(finding.get("type") or ""))
            if text:
                by_text[text] = finding
        added = []
        seen = {
            (item.get("source_task_id"), item.get("rule"))
            for item in self._load()["candidates"]
        }
        for rule in result.get("new_rules") or []:
            text = str(rule or "").strip()
            finding = by_text.get(text)
            if finding is None:
                continue
            key = (str(episode.get("task_id") or ""), text)
            if key in seen:
                continue
            try:
                row = self.add_candidate(
                    rule=text,
                    findings=[finding],
                    package=_package_name(episode),
                    skill="",
                    source_episode=episode,
                )
            except ReleaseError:
                continue
            seen.add(key)
            added.append(row)
        return added

    def _match_finding(self, text, findings, source_episode, templates, skill):
        if not findings:
            raise ReleaseError("没有失败归因，不能进入评测")
        types = [str((item or {}).get("type") or "") for item in findings if isinstance(item, dict)]
        if types and all(item in _SUCCESS_FINDINGS or item not in _EFFECTS for item in types):
            if any(item in _SUCCESS_FINDINGS for item in types):
                raise ReleaseError("这不是失败归因")
            raise ReleaseError("这条归因没有可执行的技能版本")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            finding_type = str(finding.get("type") or "")
            if finding_type not in _EFFECTS:
                continue
            expected = templates.get(finding_type)
            if expected != text:
                continue
            if not _attributed(finding_type, source_episode):
                raise ReleaseError("失败归因和来源 episode 对不上")
            skill_id, distrust, evidence = _EFFECTS[finding_type]
            if not skill_id:
                skill_id = self._skill_from_claim(source_episode, skill)
            elif skill and str(skill).strip() != skill_id:
                raise ReleaseError("技能和失败归因不一致")
            evidence = _evidence_for(skill_id, evidence)
            return finding, finding_type, skill_id, distrust, evidence
        raise ReleaseError("规则和失败归因不一致")

    def _skill_from_claim(self, episode, skill: str) -> str:
        requested = str(skill or "").strip()
        found = []
        for step in episode.get("steps") or []:
            if isinstance(step, dict) and _false_claim(step):
                name = _step_skill(step)
                if name:
                    found.append(name)
        if requested:
            if requested not in found:
                raise ReleaseError("来源 episode 里没有这个技能的谎报")
            return requested
        if len(set(found)) == 1:
            return found[0]
        raise ReleaseError("执行报告不符必须写明技能")

    def _triggered(self, step: dict, *, skill: str, distrust: bool) -> bool:
        if _step_skill(step) != skill:
            return False
        if distrust:
            return _false_claim(step)
        return _is_failure(step) and not _false_claim(step)

    def _score(self, row, episodes) -> dict:
        skill = row.get("skill") or ""
        distrust = bool(row.get("distrust_claim"))
        source_id = str(row.get("source_task_id") or "")
        autonomous = 0
        excluded = 0
        false_claims = 0
        matched = 0
        unrelated = 0
        untriggered = 0
        early_success = 0
        for episode in episodes or []:
            if not isinstance(episode, dict):
                unrelated += 1
                continue
            if _package_name(episode) != row.get("package"):
                unrelated += 1
                continue
            if source_id and str(episode.get("task_id") or "") == source_id:
                unrelated += 1
                continue
            steps = [step for step in (episode.get("steps") or []) if isinstance(step, dict)]
            skill_steps = [(index, step) for index, step in enumerate(steps) if _step_skill(step) == skill]
            if not skill_steps:
                unrelated += 1
                continue
            trigger_at = next(
                (
                    index for index, step in skill_steps
                    if self._triggered(step, skill=skill, distrust=distrust)
                ),
                None,
            )
            if trigger_at is None:
                untriggered += 1
                continue
            matched += 1
            retries = _retry_indexes(steps)
            for index, step in skill_steps:
                if index <= trigger_at:
                    if (
                        index < trigger_at
                        and step.get("verify_ok") is True
                        and not self._triggered(step, skill=skill, distrust=distrust)
                    ):
                        early_success += 1
                    continue
                if _false_claim(step):
                    false_claims += 1
                    continue
                if _is_human(step) or index in retries:
                    excluded += 1
                    continue
                if step.get("verify_ok") is not True:
                    continue
                if distrust and step.get("claimed_ok") is not True:
                    continue
                autonomous += 1
        if false_claims:
            reason = "自报成功与核验不符"
            passed = False
        elif matched == 0 and untriggered:
            reason = "样本没有触发这条规则的失败条件"
            passed = False
        elif matched == 0:
            reason = "样本和候选的业务包或技能对不上"
            passed = False
        elif autonomous == 0 and early_success:
            reason = "核验成功出现在触发之前"
            passed = False
        elif autonomous == 0:
            reason = "没有可计入的自主成功"
            passed = False
        else:
            reason = ""
            passed = True
        return {
            "passed": passed,
            "reason": reason,
            "rule": row.get("rule") or "",
            "package": row.get("package") or "",
            "skill": skill,
            "matched_episodes": matched,
            "autonomous_success": autonomous,
            "excluded": excluded,
            "false_claims": false_claims,
            "unrelated": unrelated,
            "untriggered": untriggered,
            "submits": 0,
        }

    def _candidate(self, data, candidate_id: str) -> dict:
        for row in data["candidates"]:
            if row["candidate_id"] == candidate_id:
                return row
        raise ReleaseError(f"没有这个候选: {candidate_id}")

    def _load(self) -> dict:
        with self._writer():
            return self._read_unlocked()

    def _read_unlocked(self) -> dict:
        path = os.path.join(self.root, "releases.json")
        if not os.path.isfile(path):
            return {"candidates": [], "versions": [], "active": {}, "revision": 0}
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("candidates", [])
        data.setdefault("versions", [])
        data.setdefault("revision", 0)
        if not isinstance(data.get("active"), dict):
            data["active"] = {}
        return data

    def _commit_unlocked(self, data) -> None:
        disk = self._read_unlocked()
        expected = int(data.get("revision") or 0)
        actual = int(disk.get("revision") or 0)
        if expected != actual:
            raise StaleWrite(f"发布账本版本已变化: 期望 {expected}，实际 {actual}")
        data["revision"] = actual + 1
        path = os.path.join(self.root, "releases.json")
        temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
