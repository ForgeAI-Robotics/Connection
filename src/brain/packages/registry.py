"""Registered business packages and their fixed SOP boundaries."""

from __future__ import annotations

from contracts.tasks import KernelError
from brain.packages import desk, look, reception


SOPS = {
    "reception": reception.SOP,
    "look": {"id": "look.describe", "version": "1", "description": "只观察并描述现场，不移动物体。"},
    "desk": {"id": "desk.tidy", "version": "1", "description": "按当前桌面关系整理，展开已有技能并核验。"},
}
# Alternative SOPs of a package, reachable only by their exact triggers.
VARIANTS = {"reception": (reception.NAV_ONLY_SOP, reception.SIM_MANIPULATION_SOP,
                          reception.ROS_SIM_MANIPULATION_SOP)}


def reception_catalog(config):
    """One validated definition supplies both selection metadata and planning."""
    from brain.packages.reception_skill import load_skill
    skill = load_skill(config)
    variant = dict(reception.NAV_ONLY_SOP, id=skill.sop["id"] + ".nav_only",
                   version=skill.sop["version"],
                   description="仅导航联调：执行当前接待 Skill 的全部导航步骤，不派发抓取与放置。")
    hybrid = dict(reception.SIM_MANIPULATION_SOP, id=skill.sop["id"] + ".sim_manipulation",
                  version=skill.sop["version"])
    ros_hybrid = dict(reception.ROS_SIM_MANIPULATION_SOP, id=skill.sop["id"] + ".ros_sim_manipulation",
                      version=skill.sop["version"])
    return dict(SOPS, reception=skill.sop), {"reception": (variant, hybrid, ros_hybrid)}, skill


def match_name(task) -> str:
    if reception.matches(task):
        return reception.NAME
    if look.matches(task):
        return look.NAME
    if desk.matches(task):
        return desk.NAME
    return ""


def phases_for(name: str):
    if name == "reception":
        return reception.PHASES
    if name == "look":
        return look.PHASES
    raise KernelError(f"业务包没有可执行相位: {name}")


class PackagePolicy:
    def recovery_mode(self, name):
        # New SOPs opt in here; no model may invent a recovery policy.
        return {"generic": "reorder_remaining", "reception": "preserve_order"}.get(name, "disabled")

    def phases(self, name):
        return [] if name in {"generic", "desk"} else phases_for(name)

    def enabled(self, step, config):
        return not step.optional or reception.inspection_enabled(config)

    def physical(self, kind):
        from brain.skills.catalog import is_physical
        return is_physical(kind)

    def object_id(self, step):
        from contracts.tasks import OBJECT_ID
        return step.object_id or (OBJECT_ID if step.kind in {"navigate", "inspect", "pick", "place"} else "")

    def request(self, step, task_id, command_id, last_command):
        if step.kind == "navigate":
            body = reception.navigation_body(step, task_id, command_id)
        elif step.kind == "inspect":
            proof = step.navigation_step_id or "NAVIGATING_TO_TABLE2"
            body = reception.inspection_body(task_id, command_id, last_command(proof),
                                            object_id=self.object_id(step))
        elif step.kind in {"pick", "place"}:
            proof = step.navigation_step_id or ("NAVIGATING_TO_TABLE2" if step.kind == "pick" else "NAVIGATING_TO_TABLE1")
            body = reception.manipulation_body(step, task_id, command_id, last_command(proof))
        elif step.body:
            body = {"command_id": command_id, "task_id": task_id,
                    "subtask": step.key, "robot_name": step.robot_name}
        else:
            body = {"command_id": command_id, "task_id": task_id}
        return {"skill": step.kind, "task_id": task_id, "step_id": step.step_id,
                "deadline_sec": step.deadline_sec, "body": body}

    def effects(self, step, *, final):
        if not final and step.writes == "in_gripper":
            return {"holding": self.object_id(step), "object_location": "in_gripper"}
        if final and step.kind == "place" and step.writes:
            return {"object_location": step.writes, "holding": None}
        return {}
