"""Read existing scene sources into an explicit planning snapshot."""
import os as _os
import re as _re
import json
import yaml
from shared.paths import workspace_root, data_path
from contracts.planning import PlanningInput

def _planner_memory_mode() -> bool:
    """学习测试台开关(与 config/slaver.yaml use_realtime_coords 取反)。

    True  = 记忆/部分可观测模式:给 LLM 喂 belief 符号位置(不喂精确坐标)。
    False = 全可观测模式:喂实时精确坐标(原行为)。
    """
    try:
        cfg_path = _os.path.normpath(_os.path.join(
            str(workspace_root()), "config", "slaver.yaml"))
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return not bool(cfg.get("perception", {}).get("use_realtime_coords", True))
    except Exception:
        return False


def _belief_obj_locations() -> dict:
    """读 belief(scene_state.yaml),返回 {物体基名: 人类可读位置}。

    直接读文件避免跨进程 import 路径问题;失败返回 {}(物体一律按"位置未知"处理,
    navigate_to_target 会自动搜索,不影响正确性)。
    """
    try:
        state_path = _os.path.normpath(_os.path.join(
            str(workspace_root()),
            "data", "simulation", "mujoco",
            "scene_state.yaml"))
        with open(state_path, encoding="utf-8") as f:
            state = yaml.safe_load(f) or {}
    except Exception:
        return {}

    def _b(n):
        return _re.sub(r"\s*\d+$", "", str(n).strip().lower())

    out = {}
    for loc, info in (state.get("locations") or {}).items():
        fixture = (info or {}).get("fixture")
        for o in ((info or {}).get("objects") or []):
            if loc == "robot_hand":
                out[_b(o)] = "机器人手中(已抓取)"
            elif loc == "unknown":
                # 'unknown' 桶 = 尚未发现,别误标成"已发现"
                out[_b(o)] = "位置未知(尚未发现,导航时系统会自动搜索)"
            elif fixture:
                out[_b(o)] = f"{fixture} 区域(位置记忆:已发现)"
            else:
                # 真实工作点但无 fixture 标注:露出 nav_NNN 对 LLM 无意义,给通用提示
                out[_b(o)] = "已发现(位置记忆)"
    return out


def _dream_reviewed_navigation_context() -> dict:
    """Expose reviewed DREAM semantic targets to the LLM without exposing coordinates."""
    try:
        from execution.robot_api.config import load_robot_api_config
        if (load_robot_api_config().active_backend or "").lower() != "dream":
            return {}
        from execution.dream.reviewed_targets import load_sop
        sop = load_sop()
    except Exception:
        return {}

    reviewed = sop.get("reviewed_targets") or {}
    table2 = reviewed.get("table_2") or {}
    table1 = reviewed.get("table_1") or {}
    return {
        "table2": {
            "name": "table2",
            "target_id": "table_2",
            "type": "reviewed_navigation_target",
            "description": table2.get("purpose") or "二号桌审核导航点",
            "aliases": list(table2.get("aliases") or []),
        },
        "relay2": {
            "name": "relay2",
            "target_id": "door_1",
            "route_phase": "door_approach",
            "type": "reviewed_navigation_target",
            "description": "窄门外接近审核点；只能在固定接待Pipeline对应阶段使用",
        },
        "relay3": {
            "name": "relay3",
            "target_id": "door_1",
            "route_phase": "door_lateral_exit",
            "type": "reviewed_navigation_target",
            "description": "窄门横移进门审核点；必须在relay2成功后使用",
        },
        "table1": {
            "name": "table1",
            "target_id": "table_1",
            "type": "reviewed_navigation_target",
            "description": table1.get("purpose") or "一号桌审核导航点",
            "aliases": list(table1.get("aliases") or []),
        },
        "_dream_navigation_rules": {
            "说明": (
                "当前后端是DREAM真机导航。table2、relay2、relay3、table1都是已审核的有效语义导航目标，"
                "不能因为它们不在RoboCasa家具列表中而拒绝任务。"
            ),
            "规划要求": (
                "用户要求前往其中一个目标时，生成FQrobot的独立导航子任务，例如“导航到table2”；"
                "不要输出坐标，Slaver会把语义名称解析成审核坐标。"
                "如果任务要求先到table2再到table1，绝对不能直接生成table2→table1；必须拆成"
                "table2→relay2（door_approach）→relay3（door_lateral_exit）→table1四个顺序子任务。"
            ),
        },
    }



class PlanningInputs:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.transport = transport

    def snapshot(self, task, backend, port, experiences="", rules=()):
        if backend == 'simple_o7':
            return port.planning_input(task, experiences, rules)
        from execution.robot_api.client import get_objects, get_scene as _get_scene
        if backend.startswith("slaver:"):
            transport = self.transport()
            all_robots_name = list(transport.collaborator.read_all_agents_name() or [])
            all_robots_info = transport.collaborator.read_all_agents_info() or {}
            all_environments_info = transport.collaborator.read_environment(name=None) or {}
        else:
            all_robots_name = ["FQrobot"]
            all_robots_info = {"FQrobot": {"tools": ["抓取物体", "放置物体到目标区", "清理垃圾", "整理牛奶", "整理可乐", "整理笔筒"]}}
            settings = self.config.get("profile") or {}
            profile = data_path(settings.get("path", "config/scene/profile.yaml") if isinstance(settings, dict) else settings)
            raw = yaml.safe_load(profile.read_text()) if profile.is_file() else {}
            all_environments_info = {it["name"]: it for it in (raw or {}).get("scene", [])}
        if backend == "desk":
            all_environments_info = {"backend": "desk", "objects": port._read_world(), "zones": port._read_zones()}
            all_environments_info["execution_rules"] = (
                "固定桌面，所有物体和区域均在同一操作范围内，没有导航能力，不生成导航步骤。"
                "抓取写成‘抓取 milk_1’，放置写成‘放置 milk_1 到 milk_area’，"
                "也可直接选已有技能‘整理牛奶’等，由大脑展开。"
                "已持有的物体直接放置；已在目标区且已释放的物体无需重复抓取。"
            )
        elif backend == 'slaver:mujoco_3dgs':
            # This scene has its own robot, objects and geometry. RoboCasa's
            # kitchen waypoints and memory must not leak into its plans.
            scene = _get_scene()
            if not isinstance(scene, dict) or scene.get('success') is False or not scene.get('robot'):
                from contracts.tasks import Rejected
                raise Rejected('3DGS 场景状态不可用')
            all_environments_info = {'backend': 'mujoco_3dgs', **scene,
                'execution_rules': '仅使用本场景实际存在的物体和家具。没有厨房工作点、counter、sink。'
                '坐标导航子任务写成“导航到 x,y”或“导航到 x,y,yaw”，坐标内不留空格，yaw 单位为度。'
                '抓取写成“抓取 bottle”，直接调用 grasp_object；不要调用依赖厨房工作点的 search_and_grasp。'
                '没有可放置表面时不得凭空生成桌面、牛奶区或放置技能。'}
        else:
            # ===== 注入真实场景信息 =====
            try:
                scene_data = _get_scene()
                if scene_data and scene_data.get("observation"):
                    # ALFWorld 模式：用实时 snapshot 替换静态 profile，忽略 MuJoCo 工作点
                    receptacles_list = list((scene_data.get("fixtures") or {}).keys())

                    all_environments_info = {
                        "backend": "alfworld",
                        "game_objective": scene_data.get("task", ""),  # ALFWorld 内置目标（仅供参考）
                        "observation": scene_data.get("observation", ""),
                        "holding": scene_data.get("holding"),
                        "receptacles": receptacles_list,
                        "objects_visible": list((scene_data.get("objects") or {}).keys()),
                        "admissible_commands": scene_data.get("admissible_commands", []),
                        "_alfworld_rules": (
                            "【ALFWorld规划规则】任务遵循固定骨架（对齐ALFRED gold plan），"
                            "每个子任务对应一个动作，严禁把导航和操作合并到一个子任务。\n"
                            f"可放置的目标位置/家具: {receptacles_list}。\n"
                            "1) 【找并取物体】生成一个子任务：'搜索并抓取 [物体]'。"
                            "系统会自动遍历所有位置、打开容器、找到即取走，你无需指定在哪找。\n"
                            "   【严禁】生成'搜索 drawer 1''搜索 cabinet 2'这类逐个容器的子任务——"
                            "一个物体只需一个'搜索并抓取'子任务。\n"
                            "   【严禁】单独生成 take/抓取子任务——'搜索并抓取'已包含取走动作。\n"
                            "2) 【处理物体】仅当任务要求时，按类型各生成'导航'+'操作'两个独立子任务：\n"
                            "   - 清洗clean： '导航到 sinkbasin 1' → '执行raw_action: clean [物体] 1 with sinkbasin 1'\n"
                            "   - 加热heat：  '导航到 microwave 1' → '执行raw_action: heat [物体] 1 with microwave 1'\n"
                            "   - 冷却cool：  '导航到 fridge 1'    → '执行raw_action: cool [物体] 1 with fridge 1'\n"
                            "   - 切片slice： '执行raw_action: slice [物体] 1 with knife 1'\n"
                            "   - 照亮看look（task含look/examine ... in light）：'导航到 [台灯]' → '执行raw_action: toggle [台灯] 1'\n"
                            "3) 【放置】'导航到 [目标]' → '执行raw_action: move [物体] 1 to [目标] 1'。\n"
                            "   若目标是带门容器（safe/box/fridge/microwave/cabinet/drawer），"
                            "放置前先加一个'执行raw_action: open [目标] 1'子任务。\n"
                            "4) 实例号一律写 1，系统会自动校正成实际持有/存在的实例。\n"
                            "5) 【两个物体】put two X in Y：完整重复两轮：\n"
                            "   第1轮：搜索并抓取 X | 导航到 Y |（必要时 open Y）| 执行raw_action: move X 1 to Y 1\n"
                            "   第2轮：搜索并抓取 X 排除 Y | 导航到 Y | 执行raw_action: move X 1 to Y 1\n"
                            "   第2轮的搜索子任务必须写成'搜索并抓取 X 排除 Y'（Y是放置位置，如 shelf 1），"
                            "否则会把刚放好的那个又取回来导致失败。\n"
                            "示例 'put a hot mug in coffeemachine':\n"
                            "  搜索并抓取 mug | 导航到 microwave 1 | 执行raw_action: heat mug 1 with microwave 1 | "
                            "导航到 coffeemachine 1 | 执行raw_action: move mug 1 to coffeemachine 1\n"
                            "示例 'put a cd in safe':\n"
                            "  搜索并抓取 cd | 导航到 safe 1 | 执行raw_action: open safe 1 | "
                            "执行raw_action: move cd 1 to safe 1"
                        ),
                    }
                else:
                    # MuJoCo 模式：在静态 profile 基础上注入位置信息。
                    #   memory_mode=True (use_realtime_coords=false): 喂 belief 符号位置,不喂坐标
                    #   memory_mode=False: 喂实时精确坐标(全可观测,原行为)
                    memory_mode = _planner_memory_mode()
                    belief = _belief_obj_locations() if memory_mode else {}
                    sim_objects = get_objects()
                    if isinstance(sim_objects, dict) and sim_objects.get("success") is not False:
                        import json as _json
                        for obj_name, obj_data in sim_objects.items():
                            if isinstance(obj_data, dict) and "pos" in obj_data:
                                # profile 里没有的物体(如换了 3dgs 场景的 bottle/maojin)也纳入,
                                # master 才认得【当前后端】实际存在的物体,不用每次改 profile.yaml。
                                all_environments_info.setdefault(obj_name, {"name": obj_name, "type": "object", "graspable": True})
                                info = _json.loads(all_environments_info[obj_name]) if isinstance(all_environments_info[obj_name], str) else all_environments_info[obj_name]
                                if memory_mode:
                                    # 只取物体名和持有状态(机器人知道自己拿了什么),位置一律来自 belief;
                                    # belief 没有 = 尚未探索发现 → "位置未知"
                                    base = _re.sub(r"\s*\d+$", "", obj_name.strip().lower())
                                    info["position"] = belief.get(base, "位置未知(尚未发现,导航时系统会自动搜索)")
                                else:
                                    pos = obj_data.get("pos", [])
                                    info["position"] = f"({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})" if pos else "unknown"
                                info["grasped"] = obj_data.get("grasped", False)
                                all_environments_info[obj_name] = info
                    all_environments_info['_navigation_guide'] = {
                        '说明': '使用当前场景的准确物体或家具名称；不要编造工作点。放到 plate 就导航到 plate，不要替换为笼统的 counter。',
                        'fixtures': (scene_data or {}).get('fixtures', {}),
                    }
                    if memory_mode:
                        all_environments_info['_perception_note'] = (
                            "【部分可观测】position 是机器人的位置记忆(belief),不是精确坐标,可能过期。"
                            "取物体一律用 search_and_grasp([物体]):系统自动逐工作点搜索、发现后更新记忆、"
                            "并**当场把物体抓起**,你无需指定去哪找。"
                            "【严禁】用 navigate_to_target([物体]) 取物体——它只导航/发现、**绝不抓取**,"
                            "误用会让后续 place 因'未持有'而失败(cup→cabinet 卡死的根因)。"
                            "navigate_to_target 用于去放置目标(家具或 plate 等承接物)。"
                            "放置骨架恒为:search_and_grasp([物体]) → navigate_to_target([放置目标]) → place_on_top。"
                        )
            except Exception as e:
                print(f"[Planner] Warning: could not fetch scene: {e}")
            # ===== 注入结束 =====

            # 真机DREAM审核目标属于导航能力目录，而不是RoboCasa场景物体。
            # 单独注入给LLM，保留自然语言理解与Master任务拆解过程。
            all_environments_info.update(_dream_reviewed_navigation_context())


        from brain.storage.tasks import KernelStore
        from brain.kernel.memory import event_window_settings
        limit, ttl = event_window_settings(self.config)
        manual = KernelStore(workspace_root() / 'data/scene').recent_events(limit=limit, ttl_sec=ttl)
        if manual:
            all_environments_info['_human_observation_clues'] = manual
        return PlanningInput(task, tuple(all_robots_name), all_robots_info,
                             all_environments_info, experiences, tuple(rules))
