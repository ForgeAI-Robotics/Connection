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
        if package == 'generic' and runtime.record['execution_backend'] in {'body_real', 'body_simple'}:
            from brain.skills.body_plan import plan
            snapshot = runtime.record['selection']['body_capabilities']
            text = runtime.record['task_desc']
            if runtime.record['selection'].get('native_vla') is not None:
                from brain.skills.native_vla import plan as native_plan
                return native_plan(self.planner, text, runtime.record['selection']['native_vla'], snapshot)
            result = plan(self.planner, text, snapshot, rules=runtime.record.get('release_rules') or (),
                          experiences=self.experiences.load(text))
            if runtime.record['execution_backend'] == 'body_simple':
                runtime.port.prepare_plan(runtime.record['task_id'], result.steps, snapshot)
            return result
        if package == "reception":
            if runtime.record["execution_backend"] == "reception_mock":
                from brain.packages.reception_mock import plan
                return Plan(tuple(plan(runtime.port, options)), "接待业务流程交给统一 Runtime 管理。")
            from brain.packages.reception_skill import from_snapshot, load_skill
            selected = runtime.record.get("selection") or {}
            snapshot = selected.get("workflow")
            skill = from_snapshot(snapshot) if snapshot else load_skill(self.config)
            sop = selected.get("sop") or {}
            if selected.get('workflow_variant') == 'real_sim_manipulation':
                if (runtime.record['execution_backend'] != 'reception_real_sim_manipulation' or not skill.real_sop
                        or (sop.get('id'), sop.get('version')) !=
                           (skill.real_sop['id'] + '.sim_manipulation', skill.real_sop['version'])):
                    raise Rejected('模拟操控分支与任务快照或执行后端不一致')
                return Plan(skill.phases(real=True), '同一真机接待流程；导航真实，开门、抓取及操控效果核验为本地模拟。')
            if selected.get('workflow_variant') == 'real':
                if (runtime.record['execution_backend'] != 'reception_real' or not skill.real_sop
                        or (sop.get('id'), sop.get('version')) != (skill.real_sop['id'], skill.real_sop['version'])):
                    raise Rejected('真机接待流程与任务快照或执行后端不一致')
                return Plan(skill.phases(real=True), skill.real_sop['description'])
            if (runtime.record['execution_backend'] == 'reception_real' and skill.real_steps
                    and sop.get('id') == skill.sop['id']):
                raise Rejected('接待选择时的执行环境已变化，请重新选择当前真机 SOP')
            hybrid_backends = {skill.sop["id"] + ".sim_manipulation": "reception_real_sim_manipulation",
                               skill.sop["id"] + ".ros_sim_manipulation": "reception_ros_sim_manipulation"}
            if sop.get("id") in hybrid_backends:
                if (sop.get("version") != skill.sop["version"]
                        or runtime.record["execution_backend"] != hybrid_backends[sop["id"]]):
                    raise Rejected("混合接待 SOP 与流程快照或执行后端不一致")
                # No camera inspection or physical controller transfer in this mode.
                # The adapter verifies stopped navigation and simulated receipts at boundaries.
                return Plan(tuple(s for s in skill.phases() if s.kind != "inspect"),
                            "导航＋操控模拟：完整流程核验，导航环境以任务后端为准；模拟抓放不代表真实物体效果。")
            if sop.get("id") == skill.sop["id"] + ".nav_only":
                if sop.get("version") != skill.sop["version"]:
                    raise Rejected("仅导航 SOP 与流程快照版本不一致")
                return Plan(skill.phases(nav_only=True), "仅导航联调：执行 Skill 中的导航步骤，不派发抓取与放置。")
            if sop and (sop.get("id"), sop.get("version")) != (skill.sop["id"], skill.sop["version"]):
                raise Rejected("所选接待 SOP 与流程快照版本不一致")
            return Plan(skill.phases(), "从已校验的接待 Skill 定义展开步骤，交给统一 Runtime 管理。")
        if package == "look":
            from dataclasses import replace
            import math
            from brain.packages.look import PHASES
            timeout = float((self.config.get("brain") or {}).get("observation_timeout_sec", 30))
            if not math.isfinite(timeout) or timeout <= 0:
                raise Rejected("observation_timeout_sec 必须为正的有限秒数")
            return Plan(tuple(replace(step, deadline_sec=timeout) for step in PHASES),
                        "现场观察交给 Runtime，只读取现场，不下发身体动作。")
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
