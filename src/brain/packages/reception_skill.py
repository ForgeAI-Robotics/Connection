"""Compile a declarative reception skill into the existing Runtime's steps.

This module never calls a model, submits an action, or edits a task. Schema 1
preserves the archived route; schema 2 validates operator-defined routes. Neither
schema overrides an executor's independent admission rules.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re

import yaml

from contracts.reception_lan import InvalidRequest, validate_request
from contracts.steps import StepSpec
from contracts.tasks import CONTRACT_VERSION, Rejected
from shared.paths import data_path


BASELINE_PATH = Path(__file__).with_name("definitions") / "reception_single_can_v1.yaml"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise Rejected("接待 Skill 的 YAML 键必须为字符串且不能重复")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise Rejected("接待 Skill 字段缺失或含未知字段：" + ", ".join(required))


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise Rejected("接待 Skill 标识无效：" + str(value))
    return value


def _display_name(value, key='display_name'):
    if key not in value:
        return ''
    name = value[key]
    if (not isinstance(name, str) or not name.strip() or len(name) > 120
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise Rejected(key + ' 必须为 1～120 字符的单行显示名称')
    return name.strip()


def navigation_fields(point):
    """Presentation metadata must never enter a NAV request or route comparison."""
    return {key: deepcopy(point[key]) for key in
            ('target_id', 'route_phase', 'leg_index', 'goal_xyt', 'motion_mode')}


def definition_hash(definition):
    raw = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class ReceptionSkill:
    definition: dict
    steps: tuple
    real_steps: tuple = ()

    @property
    def sop(self):
        return deepcopy(self.definition["sop"])

    @property
    def real_sop(self):
        definition = self.definition.get('real_workflow')
        return dict(deepcopy(definition['sop']), triggers=deepcopy(self.sop['triggers'])) if definition else None

    def snapshot(self):
        return {"sha256": definition_hash(self.definition), "definition": deepcopy(self.definition)}

    @property
    def navigation_points(self):
        return {key: navigation_fields(point) for key, point in self.definition['points'].items()}

    def phases(self, *, nav_only=False, real=False):
        if real:
            if nav_only or not self.real_steps:
                raise Rejected('当前 Skill 没有匹配的真机接待流程')
            return deepcopy(self.real_steps)
        steps = deepcopy(self.steps)
        if nav_only:
            return tuple(replace(step, handoff_before="") for step in steps if step.kind in {"local", "navigate"})
        return steps


def load_skill(config=None, *, path=None):
    configured = ((config or {}).get("reception_real") or {}).get("skill_file")
    source = data_path(path or configured) if path or configured else BASELINE_PATH
    try:
        if source.stat().st_size > 128 * 1024:
            raise Rejected("接待 Skill 文件过大")
        definition = yaml.load(source.read_text(encoding="utf-8"), Loader=_UniqueLoader)
        return compile_skill(definition)
    except (OSError, yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise Rejected(f"接待 Skill 读取或校验失败：{exc}") from exc


def from_snapshot(snapshot):
    _keys(snapshot, ("sha256", "definition"))
    skill = compile_skill(snapshot["definition"])
    if snapshot["sha256"] != definition_hash(skill.definition):
        raise Rejected("接待 Skill 快照摘要不一致")
    return skill


def validate_backend(skill, backend):
    """The legacy timed simulator only supports the original four legs."""
    if backend != "reception_protocol":
        return
    for point in skill.navigation_points.values():
        body = dict(contract_version=CONTRACT_VERSION, task_id="validate", command_id="validate",
                    frame_id="map", require_final_orientation=True, **deepcopy(point))
        try:
            validate_request("nav", body)
        except InvalidRequest as exc:
            raise Rejected(f"{backend} 只支持旧四段接待路线，不能执行当前 Skill；"
                           "需接入匹配的新导航执行端或显式选择旧 Skill 做仿真") from exc


def _validate_route_point(point):
    """Validate a configured waypoint, without claiming downstream approval."""
    if not isinstance(point["route_phase"], str):
        raise Rejected("route_phase 必须为字符串")
    if point["route_phase"]:
        _identifier(point["route_phase"])
    if type(point["leg_index"]) is not int or not 1 <= point["leg_index"] <= 64:
        raise Rejected("leg_index 必须为 1～64 的整数")
    goal = point["goal_xyt"]
    if (not isinstance(goal, list) or len(goal) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in goal)):
        raise Rejected("goal_xyt 必须为三个有限数，单位为米、米、弧度")
    if point["motion_mode"] not in ("forward_path", "lateral_path_aligned"):
        raise Rejected("未接入的导航运动模式")
    if point["motion_mode"] == "lateral_path_aligned" and (
            point["target_id"] != "door_1" or point["route_phase"] != "door_lateral_exit"):
        raise Rejected("横移仅支持 door_1 的 door_lateral_exit 段")


def compile_skill(definition):
    """Resolve all references and derive mandatory checks before any dispatch."""
    _keys(definition, ("schema_version", "contract_version", "sop", "parameters", "points", "steps"), ('real_workflow',))
    schema = definition["schema_version"]
    if type(schema) is not int or schema not in (1, 2):
        raise Rejected("不支持的接待 Skill schema_version")
    if definition["contract_version"] != CONTRACT_VERSION:
        raise Rejected("接待执行端尚未接入该合同版本")
    sop = definition["sop"]
    _keys(sop, ("id", "version", "description", "constraints", "triggers"))
    _identifier(sop["id"])
    for key in ("version", "description", "constraints"):
        if not isinstance(sop[key], str) or not sop[key].strip():
            raise Rejected("接待 Skill 的版本与范围说明不能为空")
    if (not isinstance(sop["triggers"], list) or not sop["triggers"]
            or any(not isinstance(value, str) or not value.strip() for value in sop["triggers"])
            or len(set(sop["triggers"])) != len(sop["triggers"])):
        raise Rejected("接待 Skill 触发词必须为非空、不重复的字符串列表")
    params = definition["parameters"]
    _keys(params, ("object_id", "quantity"))
    obj = _identifier(params["object_id"])
    if type(params["quantity"]) is not int or params["quantity"] != 1:
        raise Rejected("当前接待执行合同只支持单物体 quantity=1")
    points, actions = definition["points"], definition["steps"]
    if not isinstance(points, dict) or not points or len(points) > 64:
        raise Rejected("接待 Skill 需要 1～64 个登记导航点")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 64:
        raise Rejected("接待 Skill 需要 1～64 个动作步骤")
    for key, point in points.items():
        _identifier(key)
        _keys(point, ("target_id", "route_phase", "leg_index", "goal_xyt", "motion_mode"), ('display_name',))
        _display_name(point)
        _identifier(point["target_id"])
        _validate_route_point(point)
        # Schema 1 is the archived, exact four-leg contract. Schema 2 uses
        # operator-defined points; remote admission still owns route approval.
        body = dict(contract_version=CONTRACT_VERSION, task_id="validate", command_id="validate",
                    frame_id="map", require_final_orientation=True, **navigation_fields(point))
        try:
            if schema == 1:
                validate_request("nav", body)
        except InvalidRequest as exc:
            raise Rejected(f"导航点 {key} 不受当前 NAV 合同支持：{exc}") from exc

    steps = [StepSpec("INITIALIZING", "local"), StepSpec("FETCHING_WORLD", "local")]
    ids = {step.step_id for step in steps}
    prefixes, used_points, leg_order = set(), set(), []
    latest_nav = None
    previous_action = ""
    holding = False
    picks = places = 0
    for action in actions:
        _keys(action, ("id", "action", "command_prefix", "timeout_sec"),
              ("point", "after_navigation", "optional", "verification_id", 'display_name', 'verification_display_name'))
        display_name = _display_name(action)
        verification_name = _display_name(action, 'verification_display_name')
        step_id = _identifier(action["id"])
        prefix = _identifier(action["command_prefix"])
        kind = action["action"]
        if not isinstance(kind, str) or kind not in {"navigate", "inspect", "pick", "place"}:
            raise Rejected(f"动作尚未接入执行与核验：{kind}")
        if step_id in ids or prefix in prefixes:
            raise Rejected("接待 Skill 步骤 ID 或命令前缀重复")
        ids.add(step_id)
        prefixes.add(prefix)
        timeout = action["timeout_sec"]
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise Rejected("timeout_sec 必须为有限正数")
        if "optional" in action and (kind != "inspect" or type(action["optional"]) is not bool):
            raise Rejected("只有观察节点可以声明布尔 optional")
        if kind not in {"pick", "place"} and "verification_id" in action:
            raise Rejected("只有抓放节点可以声明 verification_id")
        if kind not in {'pick', 'place'} and 'verification_display_name' in action:
            raise Rejected('只有抓放节点可以声明 verification_display_name')
        fields = dict(step_id=step_id, kind=kind, prefix=prefix, deadline_sec=timeout, object_id=obj,
                      display_name=display_name)
        if kind == "navigate":
            point_key = action.get("point")
            if not isinstance(point_key, str) or point_key not in points or "after_navigation" in action:
                raise Rejected("导航步骤必须引用登记 point，不能声明 after_navigation")
            point = points[point_key]
            if point["motion_mode"] == "lateral_path_aligned" and (
                    latest_nav is None or latest_nav[1]["target_id"] != "door_1"
                    or latest_nav[1]["route_phase"] != "door_approach"):
                raise Rejected("横移出门前必须先导航到 door_approach")
            target_name = _display_name(point)
            title = ('横移到' if point['motion_mode'] == 'lateral_path_aligned' else '导航到') + target_name
            fields.update(key=point_key, navigation=navigation_fields(point), evidence="reached",
                          target_name=target_name, display_name=display_name or (title if target_name else ''),
                          requires_gate=latest_nav is None,
                          handoff_before="to_nav" if previous_action in {"pick", "place"} else "")
            latest_nav = (step_id, point)
            leg_order.append(point["leg_index"])
            used_points.add(point_key)
        else:
            if "point" in action or latest_nav is None or action.get("after_navigation") != latest_nav[0]:
                raise Rejected("观察和抓放必须引用最近的导航步骤 after_navigation")
            fields["navigation_step_id"] = latest_nav[0]
            target = latest_nav[1]["target_id"]
            fields['target_name'] = _display_name(latest_nav[1])
            if kind == "inspect":
                if latest_nav[1]["leg_index"] != 1:
                    raise Rejected("当前检查合同只支持取物桌导航后的检查")
                fields.update(evidence="inspected", optional=action.get("optional", False))
                if not display_name and fields['target_name']:
                    fields['display_name'] = '检查' + fields['target_name'] + '的物品'
            else:
                if (kind == "pick" and holding) or (kind == "place" and not holding):
                    raise Rejected("抓放顺序与持物前置条件不符")
                body = dict(contract_version=CONTRACT_VERSION, task_id="validate", command_id="validate",
                            operation=kind, object_id=obj, target_area=target,
                            navigation_proof={"dream_command_id": "validate", "target_id": target, "state": "succeeded"})
                try:
                    validate_request("vla", body)
                except InvalidRequest as exc:
                    raise Rejected(f"操控步骤 {step_id} 不受当前 VLA 合同支持：{exc}") from exc
                holding = kind == "pick"
                picks += kind == "pick"
                places += kind == "place"
                fields.update(target_area=target, evidence="object_held" if holding else "object_at_target",
                              handoff_before="to_vla", requires_object_evidence=True,
                              requires_safe_idle=not holding, writes="in_gripper" if holding else target)
        steps.append(StepSpec(**fields))
        if kind in {"pick", "place"}:
            verify_id = _identifier(action.get("verification_id", "VERIFY_" + step_id))
            if verify_id in ids:
                raise Rejected("接待 Skill 核验步骤 ID 重复")
            ids.add(verify_id)
            steps.append(StepSpec(verify_id, "verify", evidence=fields["evidence"], display_name=verification_name))
        if kind != "inspect":
            previous_action = kind
    expected_order = [1, 2, 3, 4] if schema == 1 else list(range(1, len(points) + 1))
    if leg_order != expected_order:
        raise Rejected("导航必须按连续 leg_index 顺序执行，且每个登记点恰好执行一次")
    if used_points != set(points):
        raise Rejected("接待 Skill 包含未被流程引用的导航点")
    if picks != 1 or places != 1 or holding or actions[-1]["action"] != "place":
        raise Rejected("单罐接待必须包含一次抓取、一次放置，并以放置完成")
    real_steps = _compile_real_workflow(definition, steps) if 'real_workflow' in definition else ()
    return ReceptionSkill(deepcopy(definition), tuple(steps), tuple(real_steps))


def _compile_real_workflow(definition, base_steps):
    """Open/grasp at one stance, then retain the configured delivery route."""
    from contracts.vla import validate_request as validate_native_request
    workflow = definition['real_workflow']
    _keys(workflow, ('sop', 'steps'))
    sop = workflow['sop']
    _keys(sop, ('id', 'version', 'description', 'constraints'))
    _identifier(sop['id'])
    if sop['id'] in {definition['sop']['id'] + suffix for suffix in
                    ('', '.nav_only', '.sim_manipulation', '.ros_sim_manipulation')}:
        raise Rejected('真机流程必须使用独立 SOP 身份')
    for key in ('version', 'description', 'constraints'):
        if not isinstance(sop[key], str) or not sop[key].strip():
            raise Rejected('真机 SOP 版本与范围不能为空')
    actions = workflow['steps']
    if (not isinstance(actions, list) or len(actions) < 3
            or any(not isinstance(a, dict) for a in actions)
            or [a.get('action') for a in actions[:3]] != ['navigate', 'open_fridge', 'grasp_cola']
            or any(a.get('action') != 'navigate' for a in actions[3:])):
        raise Rejected('真机接待先导航、开门、抓取，随后按登记路线运送；不插入关门或放置')
    route = [s.key for s in base_steps if s.kind == 'navigate']
    # v1 was already persisted by the earlier truncated implementation. Only
    # those historical snapshots may end at grasp; v2 retains the full route.
    legacy_short = sop['version'] == '1' and len(actions) == 3
    expected_route = route[:1] if legacy_short else route
    if [a.get('point') for a in actions if a['action'] == 'navigate'] != expected_route:
        raise Rejected('真机接待必须保留 Skill 的全部导航点及原顺序')
    steps = [deepcopy(s) for s in base_steps if s.kind == 'local']
    ids, prefixes = {s.step_id for s in steps}, set()
    navigation = None
    for index, action in enumerate(actions):
        kind = action['action']
        required = ('id', 'action', 'command_prefix', 'timeout_sec') + (
            ('point',) if kind == 'navigate' else ('object_id', 'after_navigation'))
        _keys(action, required, ('display_name',) if kind == 'navigate' else
              ('display_name', 'verification_id', 'verification_display_name'))
        step_id, prefix = _identifier(action['id']), _identifier(action['command_prefix'])
        if step_id in ids or prefix in prefixes:
            raise Rejected('真机流程步骤身份或命令前缀重复')
        ids.add(step_id); prefixes.add(prefix)
        timeout = action['timeout_sec']
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise Rejected('真机流程 timeout_sec 必须为有限正数')
        title = _display_name(action)
        if kind == 'navigate':
            key = action['point']
            if not isinstance(key, str):
                raise Rejected('真机流程必须引用登记 point')
            template = next((s for s in base_steps if s.kind == 'navigate' and s.key == key), None)
            if template is None:
                raise Rejected('真机流程必须引用已登记导航点')
            nav_step = replace(template, step_id=step_id, prefix=prefix, deadline_sec=timeout,
                               display_name=title or template.display_name, requires_gate=index == 0,
                               handoff_before='to_nav' if index == 3 else '', requires_vla_clearance=True,
                               required_holding=definition['parameters']['object_id'] if index > 2 else '',
                               requires_safe_idle=index == len(actions) - 1)
            if index == 0:
                navigation = nav_step
            steps.append(nav_step)
            continue
        if action['after_navigation'] != navigation.step_id:
            raise Rejected('开门与抓取必须引用同一个原导航步骤，不重新导航')
        obj = _identifier(action['object_id'])
        if kind == 'grasp_cola' and obj != definition['parameters']['object_id']:
            raise Rejected('真机抓取物体与 Skill 参数不一致')
        target = navigation.navigation['target_id']
        validate_native_request(dict(contract_version=CONTRACT_VERSION, task_id='validate', command_id='validate',
            operation=kind, object_id=obj, target_area=target,
            navigation_proof=dict(dream_command_id='validate', target_id=target, state='succeeded')))
        effect = 'door_open' if kind == 'open_fridge' else 'object_held'
        steps.append(StepSpec(step_id, kind, prefix=prefix, deadline_sec=timeout, object_id=obj,
            target_area=target, target_name=navigation.target_name, display_name=title,
            navigation_step_id=navigation.step_id, evidence=effect, handoff_before='to_vla',
            requires_object_evidence=True, requires_safe_idle=True,
            writes='in_gripper' if kind == 'grasp_cola' else ''))
        verify_id = _identifier(action.get('verification_id', 'VERIFY_' + step_id))
        if verify_id in ids:
            raise Rejected('真机核验步骤身份重复')
        ids.add(verify_id)
        steps.append(StepSpec(verify_id, 'verify', evidence=effect,
                              display_name=_display_name(action, 'verification_display_name')))
    return steps
