"""Business plan selection around the common planner."""
from contracts.tasks import Rejected
from contracts.planning import Plan

class PlanningService:
    def __init__(self, config, planner, inputs, experiences, desk_planner=None):
        self.config, self.planner, self.inputs, self.experiences = config, planner, inputs, experiences
        if desk_planner is None:
            from brain.packages.desk_relations import plan_relations
            desk_planner = plan_relations
        self.desk_planner = desk_planner

    def plan(self, runtime, options):
        package = runtime.package_name
        if package == "reception":
            if runtime.record["execution_backend"] == "reception_mock":
                from brain.packages.reception_mock import plan
                return Plan(tuple(plan(runtime.port, options)), "接待业务流程交给统一 Runtime 管理。")
            from brain.packages.reception import NAV_ONLY_PHASES, NAV_ONLY_SOP, PHASES
            sop = (runtime.record.get("selection") or {}).get("sop") or {}
            if sop.get("id") == NAV_ONLY_SOP["id"]:
                return Plan(NAV_ONLY_PHASES, "仅导航联调：依次执行接待四段导航，不派发抓取与放置。")
            return Plan(tuple(PHASES), "接待业务流程交给统一 Runtime 管理。")
        if package == "look":
            from brain.packages.look import PHASES
            return Plan(tuple(PHASES), "现场观察交给 Runtime，只读取现场，不下发身体动作。")
        if package == "desk":
            planned = self.desk_planner(runtime.record["task_desc"])
            if runtime.record["execution_backend"] == "desk":
                from brain.packages.desk import plan_steps
                return Plan(tuple(plan_steps(planned, runtime.port)), planned.get("reasoning_explanation", ""))
        else:
            text = runtime.record["task_desc"]
            snapshot = self.inputs.snapshot(text, runtime.record["execution_backend"], runtime.port,
                self.experiences.load(text), runtime.record.get("release_rules") or ())
            planned = self.planner.plan(snapshot)
            if runtime.record["execution_backend"] == "slaver:dream":
                from brain.packages.navigation_policy import enforce_dream_route_plan
                planned = enforce_dream_route_plan(text, planned)
        if runtime.record["execution_backend"] == "simple_o7":
            from brain.adapters.simple_o7 import plan_steps
            return plan_steps(planned, runtime.port.deadline)
        from brain.packages.generic import steps_from_subtasks
        kind = "sim" if runtime.record["execution_backend"] == "desk" else "slaver"
        subtasks = planned.get("subtask_list") or []
        check_skills = []
        if kind == "sim":
            from brain.adapters.execution import parse_sim_action
            from brain.packages.desk import normalize_subtasks
            from execution.robot_api.desk import SKILLS, pending_objects
            world, zones = runtime.port._read_world(), runtime.port._read_zones()
            expanded = []
            for item in subtasks:
                action = parse_sim_action(item.get("subtask"))
                if action and action[0] == "skill":
                    name = action[1]
                    check_skills.append(name)
                    for obj in pending_objects(name, world, zones):
                        if world[obj].get("grasped") is not True:
                            expanded.append(dict(item, subtask=f"抓取 {obj}"))
                        expanded.append(dict(item, subtask=f"放置 {obj} 到 {SKILLS[name]['zone']}"))
                else:
                    expanded.append(item)
            subtasks = normalize_subtasks(expanded, world, zones)
        steps = steps_from_subtasks(subtasks, kind=kind)
        from contracts.steps import StepSpec
        steps.extend(StepSpec(f"CHECK_SKILL_{i}", "desk_check", key=name, evidence="desk_tidy")
                     for i, name in enumerate(check_skills, 1))
        if kind == "slaver":
            from dataclasses import replace
            steps = [replace(step, deadline_sec=float((self.config.get("brain") or {}).get("subtask_wait_timeout_sec", 120))) for step in steps]
        if runtime.record["execution_backend"] in {"slaver:real", "slaver:dream"}:
            from dataclasses import replace
            steps = [replace(step, requires_gate=True) for step in steps]
        if not steps:
            raise Rejected("没有可执行步骤或可核验的完成条件")
        return Plan(tuple(steps), planned.get("reasoning_explanation", ""))
