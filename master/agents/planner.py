import os as _os
import re as _re
from typing import Any, Dict, Union

import yaml
from agents.prompts import MASTER_PLANNING_PLANNING
from agent.collaboration import Collaborator
from openai import AzureOpenAI, OpenAI
from robot_api.client import get_objects, get_scene as _get_scene


def _planner_memory_mode() -> bool:
    """学习测试台开关(与 slaver/config.yaml use_realtime_coords 取反)。

    True  = 记忆/部分可观测模式:给 LLM 喂 belief 符号位置(不喂精确坐标)。
    False = 全可观测模式:喂实时精确坐标(原行为)。
    """
    try:
        cfg_path = _os.path.normpath(_os.path.join(
            _os.path.dirname(__file__), "..", "..", "slaver", "config.yaml"))
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
            _os.path.dirname(__file__), "..", "..",
            "serve", "scene", "config", "scene_state.yaml"))
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
        from robot_api.config import load_robot_api_config
        if (load_robot_api_config().active_backend or "").lower() != "dream":
            return {}
        from serve_dream.reviewed_targets import load_sop
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


class GlobalTaskPlanner:
    """A tool planner to plan task into sub-tasks."""

    def __init__(
        self,
        config: Union[Dict, str] = None,
    ) -> None:
        self.collaborator = Collaborator.from_config(config["collaborator"])

        self.global_model: Any
        self.model_name: str
        self.global_model, self.model_name = self._get_model_info_from_config(
            config["model"]
        )

        self.profiling = config["profiling"]

    def _get_model_info_from_config(self, config: Dict) -> tuple:
        """Get the model info from config."""
        candidate = config["model_dict"]
        if candidate["cloud_model"] in config["model_select"]:
            if candidate["cloud_type"] == "azure":
                model_name = config["model_select"]
                model_client = AzureOpenAI(
                    azure_endpoint=candidate["azure_endpoint"],
                    azure_deployment=candidate["azure_deployment"],
                    api_version=candidate["azure_api_version"],
                    api_key=candidate["azure_api_key"],
                )
            elif candidate["cloud_type"] == "default":
                model_client = OpenAI(
                    base_url=candidate["cloud_server"],
                    api_key=candidate["cloud_api_key"],
                )
                model_name = config["model_select"]
            else:
                raise ValueError(f"Unsupported cloud type: {candidate['cloud_type']}")
            return model_client, model_name
        raise ValueError(f"Unsupported model: {config['model_select']}")

    def _init_config(self, config_path="config.yaml"):
        """Initialize configuration"""
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config

    def display_profiling_info(self, description: str, message: any):
        """
        Outputs profiling information if profiling is enabled.

        :param message: The content to be printed. Can be of any type.
        :param description: A brief title or description for the message.
        """
        if self.profiling:
            module_name = "master"  # Name of the current module
            print(f" [{module_name}] {description}:")
            print(message)

    def forward(self, task: str, history: list = None, experiences: str = "") -> str:
        """Get the sub-tasks from the task."""

        all_robots_name = self.collaborator.read_all_agents_name()
        all_robots_info = self.collaborator.read_all_agents_info()
        all_environments_info = self.collaborator.read_environment(name=None) or {}

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
                    '说明': '导航时使用以下简单名称，不要使用fixture原始名称',
                    'counter': {'工作点': 'counter_front', '可服务物体': ['apple', 'mug', 'pot', 'cup']},
                    'sink': {'工作点': 'island_north', '可服务物体': ['sink', 'sponge', 'bowl']},
                    'island': {'工作点': 'island_north', '可服务物体': ['island', 'bowl', 'sponge']},
                    'stove': {'工作点': 'stove_front', '可服务物体': ['stove']},
                }
                if memory_mode:
                    all_environments_info['_perception_note'] = (
                        "【部分可观测】position 是机器人的位置记忆(belief),不是精确坐标,可能过期。"
                        "取物体一律用 search_and_grasp([物体]):系统自动逐工作点搜索、发现后更新记忆、"
                        "并**当场把物体抓起**,你无需指定去哪找。"
                        "【严禁】用 navigate_to_target([物体]) 取物体——它只导航/发现、**绝不抓取**,"
                        "误用会让后续 place 因'未持有'而失败(cup→cabinet 卡死的根因)。"
                        "navigate_to_target 只用于去目标家具。"
                        "放置骨架恒为:search_and_grasp([物体]) → navigate_to_target([目标家具]) → place_on_top。"
                    )
        except Exception as e:
            print(f"[Planner] Warning: could not fetch scene: {e}")
        # ===== 注入结束 =====

        # 真机DREAM审核目标属于导航能力目录，而不是RoboCasa场景物体。
        # 单独注入给LLM，保留自然语言理解与Master任务拆解过程。
        all_environments_info.update(_dream_reviewed_navigation_context())

        content = MASTER_PLANNING_PLANNING.format(
            robot_name_list=all_robots_name,
            robot_tools_info=all_robots_info,
            task=task,
            scene_info=all_environments_info,
            experience_section=experiences
        )

        messages = self._build_messages(content, history)
        return self._call_llm(messages)

    def generate(self, prompt: str) -> str:
        """直接调用 LLM，不套任务规划模板。用于经验总结等场景。"""
        messages = [{"role": "user", "content": prompt}]
        return self._call_llm(messages)

    def _build_messages(self, content: str, history: list = None) -> list:
        """构建消息列表。"""
        messages = []
        if history:
            for msg in history:
                c = msg.get("content")
                if isinstance(c, list):
                    c = "".join(p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "") for p in c)
                elif c is None:
                    c = ""
                elif not isinstance(c, str):
                    c = str(c)
                messages.append({"role": msg["role"], "content": c})
        messages.append({"role": "user", "content": content})
        return messages

    def _call_llm(self, messages: list) -> str:
        """调用 LLM 并返回文本结果。"""
        self.display_profiling_info("messages", messages)

        from datetime import datetime
        start_inference = datetime.now()
        response = self.global_model.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=0.2,
            top_p=0.9,
            max_tokens=2048,
            seed=42,
        )
        end_inference = datetime.now()

        self.display_profiling_info(
            "inference time",
            f"inference start:{start_inference} end:{end_inference} during:{end_inference-start_inference}",
        )
        self.display_profiling_info("response", response)
        self.display_profiling_info("response.usage", response.usage)

        raw = response.choices[0].message.content
        if raw is None:
            raw = ""
        elif isinstance(raw, list):
            raw = "".join(
                part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                for part in raw
            )
        elif not isinstance(raw, str):
            raw = str(raw)
        return raw
