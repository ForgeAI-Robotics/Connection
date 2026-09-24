"""Choose an existing business package. Unknown work stays on the old queue."""

from __future__ import annotations

from connection.contracts.tasks import KernelError
from connection.brain.packages import desk, look, reception


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
    def phases(self, name):
        return [] if name in {"generic", "desk"} else phases_for(name)

    def enabled(self, step, config):
        return not step.optional or reception.inspection_enabled(config)

    def physical(self, kind):
        from connection.brain.skills.catalog import is_physical
        return is_physical(kind)

    def object_id(self, step):
        from connection.contracts.tasks import OBJECT_ID
        return step.object_id or (OBJECT_ID if step.kind in {"navigate", "inspect", "pick", "place"} else "")

    def request(self, step, task_id, command_id, last_command):
        if step.kind == "navigate":
            body = reception.navigation_body(step, task_id, command_id)
        elif step.kind == "inspect":
            body = reception.inspection_body(task_id, command_id, last_command("NAVIGATING_TO_TABLE2"))
        elif step.kind in {"pick", "place"}:
            proof = "NAVIGATING_TO_TABLE2" if step.kind == "pick" else "NAVIGATING_TO_TABLE1"
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
        if final and step.writes == "table_1":
            return {"object_location": "table_1", "holding": None}
        return {}
