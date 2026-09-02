import asyncio
import json
import logging
import os
import threading
from datetime import datetime
import uuid
from typing import Dict, List, Optional
import re

import yaml
from dotenv import load_dotenv
from agents.planner import GlobalTaskPlanner
from agent.collaboration import Collaborator

# Load .env from project root
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
load_dotenv(os.path.join(_project_root, '.env'))

# Bypass system proxy for API calls
os.environ['NO_PROXY'] = '*'


class TaskQueue:
    """Master 维护的可变任务队列，支持执行过程中增量插入新子任务。"""

    def __init__(self, subtask_list: list):
        self.tasks = []
        self._next_order = 0
        for task in subtask_list:
            order = int(task.get("subtask_order", 0))
            self._next_order = max(self._next_order, order)
            self.tasks.append({
                "order": order,
                "robot_name": task.get("robot_name"),
                "subtask": task.get("subtask"),
                "done": False,
            })

    def get_next_undone(self) -> Optional[Dict]:
        for task in self.tasks:
            if not task["done"]:
                return task
        return None

    def mark_done(self, task: Dict, status: str = "success", result: str = ""):
        task["done"] = True
        task["status"] = status  # "success" | "failure" | "exception" | "timeout"
        task["result"] = result  # actual observation/response from slaver

    def append_tasks(self, new_subtasks: list):
        for task in new_subtasks:
            self._next_order += 1
            self.tasks.append({
                "order": self._next_order,
                "robot_name": task.get("robot_name"),
                "subtask": task.get("subtask"),
                "done": False,
                "inserted": True,   # 失败重规划插入到队尾的子任务(前端标"↻ 失败重插")
            })

    def remove_pending_tasks(self, subtask_descriptions: list):
        """移除未完成的子任务（按描述匹配）。"""
        self.tasks = [
            t for t in self.tasks
            if t["done"] or not any(
                desc in t["subtask"] for desc in subtask_descriptions
            )
        ]

    def get_completed(self) -> List[Dict]:
        return [t for t in self.tasks if t["done"]]

    def get_remaining(self) -> List[Dict]:
        return [t for t in self.tasks if not t["done"]]

    def all_done(self) -> bool:
        return all(t["done"] for t in self.tasks)


class GlobalAgent:
    def __init__(self, config_path="config.yaml"):
        """Initialize GlobalAgent"""
        self._init_config(config_path)
        self._init_logger(self.config["logger"])
        self.collaborator = Collaborator.from_config(self.config["collaborator"])
        self.planner = GlobalTaskPlanner(self.config)
        self.listening_robots = set()
        self.conversation_history = []
        self.max_history = 5
        self.terminated_tasks = set()
        self.pending_scene_changes = []
        self._scene_changes_lock = threading.Lock()
        self.current_task_queue: Optional[TaskQueue] = None
        self.current_task_id: Optional[str] = None
        self.current_task_desc: Optional[str] = None
        self._reception_running = False           # 接待 skill 是否在跑(get_task_status 的 all_done 用)
        self._reception_state = None               # 真机接待runtime/verified状态
        self._last_subtask_status = None  # Slaver 返回的子任务状态
        self._last_subtask_result = None  # Slaver 返回的子任务结果（含 VLM 描述）
        self._memory_dir = os.path.join(os.path.dirname(__file__), '..', 'memory')
        self._experience_file = os.path.join(self._memory_dir, 'experiences.md')
        self._skills_dir = os.path.join(self._memory_dir, 'skills')
        self._exploration_rate = self.config.get('experience', {}).get('exploration_rate', 0.8)
        # Env override so the benchmark can lower it (EXPLORATION_RATE=0.1) without editing
        # the tracked config. High exploration tells the LLM to ignore past experience,
        # which would flatten a learning curve — keep it low for the bench.
        _env_expl = os.environ.get('EXPLORATION_RATE')
        if _env_expl not in (None, ''):
            try:
                self._exploration_rate = float(_env_expl)
            except ValueError:
                pass
        self._pending_failure = None   # 等待人工录入的失败信息
        self._pending_success = None   # 等待人工决定是否录入的成功信息
        self._dispatch_token = 0       # 每次新任务自增，旧 dispatch thread 检测到变化后退出

        self.logger.info(f"Configuration loaded from {config_path} ...")
        self.logger.info(f"Master Configuration:\n{self.config}")

        self._init_scene(self.config["profile"])
        self._start_listener()
        self._start_scene_change_listener()

    @staticmethod
    def _is_dream_backend() -> bool:
        try:
            from robot_api.config import load_robot_api_config
            return (load_robot_api_config().active_backend or "").lower() == "dream"
        except Exception:
            return False

    def _init_logger(self, logger_config):
        self.logger = logging.getLogger(logger_config["master_logger_name"])
        logger_file = logger_config["master_logger_file"]
        os.makedirs(os.path.dirname(logger_file), exist_ok=True)
        file_handler = logging.FileHandler(logger_file)

        if logger_config["master_logger_level"] == "DEBUG":
            self.logger.setLevel(logging.DEBUG)
            file_handler.setLevel(logging.DEBUG)
        elif logger_config["master_logger_level"] == "INFO":
            self.logger.setLevel(logging.INFO)
            file_handler.setLevel(logging.INFO)
        elif logger_config["master_logger_level"] == "WARNING":
            self.logger.setLevel(logging.WARNING)
            file_handler.setLevel(logging.WARNING)
        elif logger_config["master_logger_level"] == "ERROR":
            self.logger.setLevel(logging.ERROR)
            file_handler.setLevel(logging.ERROR)

        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)

    def _init_config(self, config_path="config.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            raw = f.read()
        raw = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), raw)
        self.config = yaml.safe_load(raw)

    def _init_scene(self, scene_config):
        path = scene_config["path"]
        if not os.path.exists(path):
            self.logger.error(f"Scene config file {path} does not exist.")
            raise FileNotFoundError(f"Scene config file {path} not found.")
        with open(path, "r", encoding="utf-8") as f:
            self.scene = yaml.safe_load(f)

        scenes = self.scene.get("scene", [])
        for scene_info in scenes:
            scene_name = scene_info.pop("name", None)
            if scene_name:
                self.collaborator.record_environment(scene_name, json.dumps(scene_info))
            else:
                print("Warning: Missing 'name' in scene_info:", scene_info)

    def _handle_register(self, robot_name: Dict) -> None:
        robot_info = self.collaborator.read_agent_info(robot_name)
        if robot_name in self.listening_robots:
            return

        self.logger.info(
            f"AGENT_REGISTRATION: {robot_name} \n {json.dumps(robot_info)}"
        )

        channel_r2b = f"{robot_name}_to_FQPlanner"
        threading.Thread(
            target=lambda: self.collaborator.listen(channel_r2b, self._handle_result),
            daemon=True,
            name=channel_r2b,
        ).start()
        self.listening_robots.add(robot_name)

        self.logger.info(
            f"FQPlanner has listened to [{robot_name}] by channel [{channel_r2b}]"
        )

    def _handle_result(self, data: str):
        data = json.loads(data)

        robot_name = data.get("robot_name")
        subtask_handle = data.get("subtask_handle")
        subtask_result = data.get("subtask_result")
        terminated = data.get("terminated", False)
        task_id = data.get("task_id")
        status = data.get("status")  # success/failure/none/exception/timeout

        if robot_name and subtask_handle and subtask_result:
            self.logger.info(
                f"================ Received result from {robot_name} ================"
            )
            self.logger.info(f"Subtask: {subtask_handle}\nResult: {subtask_result}\nStatus: {status}")
            if (
                task_id
                and self.current_task_id
                and str(task_id) != str(self.current_task_id)
            ):
                self.logger.warning(
                    "[STALE_RESULT] 忽略旧任务结果: "
                    f"result_task_id={task_id}, current_task_id={self.current_task_id}"
                )
                self.collaborator.update_agent_busy(robot_name, False)
                return
            if terminated:
                self.logger.warning(f"[TERMINATE] Task {task_id} terminated by judge")
                if task_id:
                    self.terminated_tasks.add(task_id)
            # 存储最后一次子任务状态和结果，供 _dispath_subtasks_async 使用
            self._last_subtask_status = status
            self._last_subtask_result = subtask_result
            self.logger.info(
                "===================================================================="
            )
            self.collaborator.update_agent_busy(robot_name, False)

        else:
            self.logger.warning("[WARNING] Received incomplete result data")
            self.logger.info(
                f"================ Received result from {robot_name} ================"
            )
            self.logger.info(f"Subtask: {subtask_handle}\nResult: {subtask_result}")
            self.logger.info(
                "===================================================================="
            )
            # 即使结果不完整也要释放 busy，否则 wait_agents_free 永远不返回
            if robot_name:
                self.collaborator.update_agent_busy(robot_name, False)

    def _extract_json(self, input_string):
        if not isinstance(input_string, str):
            self.logger.warning(f"[_extract_json] received non-string input: {type(input_string)}")
            return None

        json_match = re.search(r"```json\n(.*?)\n```", input_string, flags=re.DOTALL)
        if json_match:
            json_str = json_match.group(1).strip()
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                self.logger.warning(f"Failed to parse JSON from markdown: {e}")
                return None

        try:
            return json.loads(input_string)
        except json.JSONDecodeError:
            try:
                brace_start = input_string.find("{")
                brace_end = input_string.rfind("}") + 1
                if brace_start != -1 and brace_end != 0:
                    json_str = input_string[brace_start:brace_end]
                    return json.loads(json_str)
            except json.JSONDecodeError as e:
                self.logger.warning(f"Could not parse JSON from string content: {e}")
        return None

    def _get_skill_name(self, task_desc: str) -> str:
        if isinstance(task_desc, list):
            task_desc = " ".join(str(x) for x in task_desc)
        elif not isinstance(task_desc, str):
            task_desc = str(task_desc)
        task_lower = (task_desc or "").lower()
        if any(w in task_lower for w in ['抓', 'grasp', '拿', '捡', '取']):
            return 'grasp'
        elif any(w in task_lower for w in ['放', 'place', '放置', '放到', '放在']):
            return 'place'
        elif any(w in task_lower for w in ['导航', 'navigate', '去', '移动到']):
            return 'navigate'
        else:
            return 'multi_step'

    def _start_listener(self):
        threading.Thread(
            target=lambda: self.collaborator.listen(
                "AGENT_REGISTRATION", self._handle_register
            ),
            daemon=True,
        ).start()
        self.logger.info("Started listening for robot registrations...")

    def _start_scene_change_listener(self):
        """监听场景变化频道，实时接收 SceneDetector 推送的变化。"""
        threading.Thread(
            target=lambda: self.collaborator.listen(
                "scene_changes", self._handle_scene_change
            ),
            daemon=True,
            name="scene_changes_listener",
        ).start()
        self.logger.info("Started listening for scene changes...")

    def _handle_scene_change(self, data: str):
        """处理实时推送的场景变化，累积到 pending_scene_changes。"""
        try:
            change = json.loads(data)
            with self._scene_changes_lock:
                self.pending_scene_changes.append(change)
            self.logger.info(f"[SceneChanges] Real-time: {change}")
        except (json.JSONDecodeError, TypeError) as e:
            self.logger.warning(f"[SceneChanges] Failed to parse: {e}")

    def reasoning_and_subtasks_is_right(self, reasoning_and_subtasks: dict) -> bool:
        if not isinstance(reasoning_and_subtasks, dict):
            return False

        if "subtask_list" not in reasoning_and_subtasks:
            return False

        try:
            worker_list = {
                subtask["robot_name"]
                for subtask in reasoning_and_subtasks["subtask_list"]
                if isinstance(subtask, dict) and "robot_name" in subtask
            }

            robots_list = set(self.collaborator.read_all_agents_name())
            return worker_list.issubset(robots_list)

        except (TypeError, KeyError):
            return False

    def _is_desk_tidy_task(self, task) -> bool:
        """demo「整理桌面」任务识别 → 走关系判断规划分支(不经通用 planner)。"""
        t = task if isinstance(task, str) else (task[0] if task else "")
        return any(k in t for k in ("整理桌面", "桌面整理", "清洁会议室", "整理会议室", "收拾桌"))

    def _plan_desk_tidy(self, task) -> Dict:
        """demo 规划:vlm_judge 关系判断(当前图 vs 标准图,关系变了才动) → 技能 subtask_list。
        复用 master/sop 关系对比闭环。返回 {reasoning_explanation, subtask_list}。"""
        from sop.classify import load_sop
        from sop.run_loop import needed_skills_from_vlm, SKILLS
        sop = load_sop()
        needed, no_skill, keeps, vlm_result = needed_skills_from_vlm(sop)
        judged = "; ".join(
            f"{it.get('object')}[{it.get('current_rel', '')}→{it.get('goal_rel', '')}]:{it.get('action')}"
            for it in vlm_result)
        reasoning = (
            f"读 SOP + 对比桌面当前图/标准图,按物体空间关系判断(关系变了才动): {judged}。"
            f" → 需执行: {[SKILLS[s]['label'].split('(')[0] for s in needed]};"
            f" 已就位/保留跳过: {keeps}。"
            + (f" 判断需处理但无对应技能(需人): {no_skill}。" if no_skill else ""))
        subtask_list = [
            {"robot_name": "FQrobot",
             "subtask": SKILLS[s]["label"].split("(")[0].strip(),
             "subtask_order": i + 1}
            for i, s in enumerate(needed)]
        self.logger.info(f"[demo] 关系判断 → {len(subtask_list)} 子任务: "
                         f"{[st['subtask'] for st in subtask_list]}")
        return {"reasoning_explanation": reasoning, "subtask_list": subtask_list}

    def _is_reception_task(self, task) -> bool:
        """会议接待任务识别 → 走接待 skill(自带 SOP+经验的复合技能),不经通用 planner。"""
        from sop.reception_skill import is_reception_task
        return is_reception_task(task)

    def get_task_preflight(self, task) -> Dict:
        """Return a read-only readiness report for a prospective top-level task."""

        if not self._is_reception_task(task):
            return {
                "ready": True,
                "required": False,
                "task_type": "generic",
                "blockers": [],
            }

        real_config = dict(self.config.get("reception_real") or {})
        requested_mode = str(os.environ.get("RECEPTION_MODE") or "").strip().lower()
        reception_mode = requested_mode or (
            "real" if real_config.get("enabled") is True else "mock"
        )
        if reception_mode != "real":
            return {
                "ready": True,
                "required": False,
                "task_type": "reception_mock",
                "blockers": [],
            }

        from sop.reception_real import check_reception_real_preflight

        return check_reception_real_preflight(real_config)

    def _run_reception_skill(self, task, task_id, refresh) -> Dict:
        """接待 skill:master 不拆子任务,整体调用它自跑补货闭环(每步重新确认、失败按现状恢复、
        事后反思)。on_step 把每个子任务动态 append 进 current_task_queue → 前端 task_status
        复用显示(🧠思考 + 子任务✓)。补几罐运行时才知道,所以队列空起、边跑边填。"""
        import threading
        import uuid as _uuid
        from sop.reception_skill import run_reception_skill, RECEPTION_SKILL, skill_card

        # A. 忽略重复触发:上一次接待还没跑完,就忽略新的"开始接待"
        # (防连发、防两个后台线程同时改同一个虚拟世界打架)。等它跑完 _reception_running 复位后,再说才会开新的。
        if self._reception_running:
            msg = "已有一次接待正在进行,忽略这次重复触发(等它跑完,再说“开始接待”才会开新的一次)"
            self.logger.info(f"[reception] 忽略重复触发(上一次接待未结束): {self.current_task_desc}")
            return {"reasoning_explanation": msg, "subtask_list": [], "ignored": True}

        task_id = task_id or str(_uuid.uuid4()).replace("-", "")
        card = skill_card()
        real_config = dict(self.config.get("reception_real") or {})
        requested_mode = str(os.environ.get("RECEPTION_MODE") or "").strip().lower()
        reception_mode = requested_mode or (
            "real" if real_config.get("enabled") is True else "mock")
        if reception_mode == "real":
            reasoning = (
                "识别到接待任务 → 在Master内执行真机固定单链路："
                "DREAM table2 → VLA抓取 → VLM+LLM判真 → relay2 → relay3 → "
                "table1 → VLA放置 → VLM+LLM判真。任一步失败立即停止。")
        else:
            reasoning = (
                f"识别到接待任务 → 调用【接待 skill】(自带 SOP {card.get('sop_version', '')} + "
                f"{len(card.get('learned_rules', []))} 条经验规则,当前为mock流程)。"
                f"流程:{RECEPTION_SKILL['description']}")

        # 空队列,子任务由 skill 运行时动态 append(补几罐运行时才知道)
        self.current_task_queue = TaskQueue([])
        self.current_task_id = task_id
        self.current_task_desc = task if isinstance(task, str) else (task[0] if task else "开始接待")
        self.current_reasoning = reasoning
        self._reception_running = True
        self._reception_state = None
        self._pending_failure = None
        self._pending_success = None
        self.logger.info(f"[reception] 走接待 skill: {self.current_task_desc}")

        def _on_step(no, phase, detail, status):
            q = self.current_task_queue
            if q is None:
                return
            q.tasks.append({
                "order": no, "robot_name": RECEPTION_SKILL["robot_name"],
                "subtask": phase, "done": True, "status": status,
                "result": detail, "inserted": False,
            })
            self.logger.info(f"[reception] 子任务{no} {status}: {phase} — {detail}")

        def _on_state(state):
            self._reception_state = state

        def _worker():
            try:
                run_reception_skill(
                    task=self.current_task_desc,
                    task_id=task_id,
                    mode=reception_mode,
                    real_config=real_config,
                    on_step=_on_step,
                    on_state=_on_state,
                )
            except Exception as exc:
                self.logger.error(f"[reception] skill 执行异常: {exc}")
                _on_step(99, "接待任务异常", str(exc), "exception")
            finally:
                self._reception_running = False

        threading.Thread(target=_worker, daemon=True, name="reception_skill").start()
        return {"reasoning_explanation": reasoning, "subtask_list": []}

    def publish_global_task(self, task: str, refresh: bool, task_id: str) -> Dict:
        """Publish a global task to all Agents"""
        self.logger.info(f"Publishing global task: {task}")

        # 每条新顶层任务独立规划，清空历史避免旧任务计划干扰 LLM
        self.conversation_history = []

        if self._is_reception_task(task):
            # 会议接待:走接待 skill(自带 SOP+经验,内部自跑补货闭环),不经通用 planner
            return self._run_reception_skill(task, task_id, refresh)

        if self._is_desk_tidy_task(task):
            # demo「整理桌面」:关系判断→技能规划,不经通用 planner(ALFWorld 骨架)
            reasoning_and_subtasks = self._plan_desk_tidy(task)
            response = json.dumps(reasoning_and_subtasks, ensure_ascii=False)
            self.logger.info(f"[demo] 桌面整理规划: {reasoning_and_subtasks}")
        else:
            experiences = self._load_experiences(task=task)
            response = self.planner.forward(task, self.conversation_history, experiences)
            self.logger.info(f"Raw response from planner: {response}")
            reasoning_and_subtasks = self._extract_json(response)

            attempt = 0
            while (not self.reasoning_and_subtasks_is_right(reasoning_and_subtasks)) and (
                attempt < self.config["model"]["model_retry_planning"]
            ):
                self.logger.warning(
                    f"Attempt {attempt + 1} to extract JSON failed. Retrying..."
                )
                response = self.planner.forward(task, history=None, experiences=experiences)
                reasoning_and_subtasks = self._extract_json(response)
                attempt += 1

        if self._is_dream_backend() and isinstance(reasoning_and_subtasks, dict):
            from agents.dream_route_policy import enforce_dream_route_plan
            reasoning_and_subtasks = enforce_dream_route_plan(
                task, reasoning_and_subtasks)
            response = json.dumps(reasoning_and_subtasks, ensure_ascii=False)

        self.logger.info(f"Received reasoning and subtasks:\n{reasoning_and_subtasks}")
        if not reasoning_and_subtasks or not isinstance(reasoning_and_subtasks, dict):
            raise RuntimeError(
                f"Planner returned invalid JSON after {attempt} retries. "
                f"Raw response (last 200 chars): ...{response[-200:]}"
            )
        subtask_list = reasoning_and_subtasks.get("subtask_list", [])

        def _ensure_str(v):
            if v is None:
                return ""
            if isinstance(v, list):
                return "".join(p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "") for p in v)
            return v if isinstance(v, str) else str(v)

        self.conversation_history.append({"role": "user", "content": _ensure_str(task)})
        self.conversation_history.append({"role": "assistant", "content": _ensure_str(response)})
        if len(self.conversation_history) > self.max_history * 2:
            self.conversation_history = self.conversation_history[-self.max_history * 2:]

        task_queue = TaskQueue(subtask_list)

        task_id = task_id or str(uuid.uuid4()).replace("-", "")
        self.current_task_queue = task_queue
        self.current_task_id = task_id
        self.current_task_desc = task if isinstance(task, str) else (task[0] if task else "")
        self.current_reasoning = reasoning_and_subtasks.get("reasoning_explanation", "")

        # 使旧 dispatch thread 失效，并重置共享状态
        self._dispatch_token += 1
        my_token = self._dispatch_token
        self._pending_failure = None
        self._pending_success = None
        for agent_name in self.collaborator.read_all_agents_name():
            self.collaborator.update_agent_busy(agent_name, False)

        threading.Thread(
            target=asyncio.run,
            args=(self._dispath_subtasks_async(task, task_id, task_queue, refresh, my_token),),
            daemon=True,
        ).start()

        return reasoning_and_subtasks

    async def _dispath_subtasks_async(
        self,
        task: str,
        task_id: str,
        task_queue: TaskQueue,
        refresh: bool,
        dispatch_token: int = 0,
    ):
        """逐个发送子任务，每个完成后检查场景变化并增量规划。"""
        robot_name = None
        task_had_failure = False  # 任一非拍照子任务失败则置 True

        def _superseded():
            """检查是否被新任务取代。"""
            return self._dispatch_token != dispatch_token

        while not task_queue.all_done():
            if _superseded():
                self.logger.info(f"[Dispatch] Token 已过期，退出旧任务 dispatch")
                return
            if task_id in self.terminated_tasks:
                self.logger.warning(f"[TERMINATE] Stopping task {task_id}")
                self.terminated_tasks.discard(task_id)
                break

            current = task_queue.get_next_undone()
            if not current:
                break

            robot_name = current["robot_name"]
            subtask_data = {
                "task_id": task_id,
                "task": current["subtask"],
                "order": "true",
            }
            if refresh:
                self.collaborator.clear_agent_status(robot_name)

            # 重置状态
            self._last_subtask_status = None
            self._last_subtask_result = None

            self.logger.info(f"Sending: {current['subtask']}")
            # 先标 busy 再发消息，防止机器人响应过快导致 busy flag 竞争
            self.collaborator.update_agent_busy(robot_name, True)
            self.collaborator.send(
                f"fqplanner_to_{robot_name}", json.dumps(subtask_data)
            )
            self.collaborator.wait_agents_free([robot_name])

            if _superseded():
                self.logger.info(f"[Dispatch] 等待期间被新任务取代，退出")
                return

            subtask_status = self._last_subtask_status or "success"
            subtask_result = self._last_subtask_result or ""
            # "navigated" is an ALFWorld intermediate status — the slaver finished at a
            # navigation step without a subsequent action.  Treat it as success so the
            # frontend shows ✓ and task_had_failure stays clean.
            if subtask_status == "navigated":
                subtask_status = "success"
            task_queue.mark_done(current, status=subtask_status, result=subtask_result)

            # Real DREAM route legs are strict dependencies.  A failed leg may
            # never be skipped while a later navigation leg is dispatched.
            if (
                subtask_status in ("failure", "exception", "timeout")
                and self._is_dream_backend()
            ):
                task_had_failure = True
                remaining = task_queue.get_remaining()
                for pending in remaining:
                    task_queue.mark_done(
                        pending,
                        status="failure",
                        result=(
                            "DREAM前置子任务失败，严格串行安全合同已阻止后续任务："
                            f"{current['subtask']}"
                        ),
                    )
                self.logger.warning(
                    "[DREAM fail-fast] 前置子任务失败；已阻止后续 %d 个子任务",
                    len(remaining),
                )
                break

            # ALFWorld 搜索阶段：result 文本中出现目标物体名时，跳过剩余搜索子任务。
            # 如果 slaver 触发了自动取走（[Auto-take]），同时跳过后续显式 take 子任务。
            if subtask_status == "success" and "搜索" in current["subtask"] and subtask_result:
                try:
                    import re as _re
                    _m = _re.search(r'寻找\s+(\S+)', current["subtask"])
                    if _m:
                        _target = _m.group(1).strip().rstrip('.,，。')
                        if _target.lower() in str(subtask_result).lower():
                            # 只跳过「当前轮」的剩余搜索子任务（遇到第一个非搜索任务就停）
                            # 这样两轮搜索+放置的计划中，第二轮搜索不会被误跳过
                            remaining_search = []
                            for _t in task_queue.get_remaining():
                                if "搜索" in _t["subtask"]:
                                    remaining_search.append(_t)
                                else:
                                    break  # 遇到导航/操作子任务，当前轮结束
                            if remaining_search:
                                self.logger.info(
                                    f"[ALFWorld] 在 '{current['subtask']}' 结果中找到目标 '{_target}'，"
                                    f"跳过本轮剩余 {len(remaining_search)} 个搜索子任务，robot 留在当前位置"
                                )
                                for t in remaining_search:
                                    task_queue.mark_done(
                                        t, status="success",
                                        result=f"目标 '{_target}' 已在其他位置找到，跳过此搜索"
                                    )
                            # Auto-take 场景：slaver 在搜索阶段已经取走了目标物体，
                            # 后续显式 "执行raw_action: take X" 子任务会失败（take 不在 admissible 里）。
                            # 检测到 [Auto-take] 标记时，把显式 take 子任务也标为成功跳过。
                            if "[Auto-take]" in str(subtask_result):
                                # 只跳过当前轮（直到第一个非搜索任务）中的显式 take 子任务
                                _remaining_cur = []
                                for _t2 in task_queue.get_remaining():
                                    if "搜索" in _t2["subtask"]:
                                        _remaining_cur.append(_t2)
                                    else:
                                        _remaining_cur.append(_t2)
                                        break
                                remaining_take = [
                                    t for t in _remaining_cur
                                    if "raw_action" in t["subtask"].lower()
                                    and "take" in t["subtask"].lower()
                                    and _target.lower() in t["subtask"].lower()
                                ]
                                if remaining_take:
                                    self.logger.info(
                                        f"[ALFWorld] Auto-take 已取走 '{_target}'，"
                                        f"跳过显式 take 子任务: {[t['subtask'] for t in remaining_take]}"
                                    )
                                    for t in remaining_take:
                                        task_queue.mark_done(
                                            t, status="success",
                                            result=f"目标 '{_target}' 已在搜索阶段自动取走，跳过此抓取步骤"
                                        )
                except Exception:
                    pass

            # 子任务失败/异常时，拍照诊断（但拍照任务本身失败不再触发拍照，避免死循环）
            is_camera_task = "拍照" in current["subtask"]
            if self._last_subtask_status in ("failure", "exception", "timeout") and not is_camera_task:
                task_had_failure = True
                # demo「整理桌面」:失败技能直接重插到队尾重做(不走相机诊断/LLM 重规划);
                # 用 inserted 判断避免无限重插——重插的那次再失败,就不再重插了。
                if self._is_desk_tidy_task(task):
                    if not current.get("inserted"):
                        task_queue.append_tasks([{
                            "robot_name": current["robot_name"],
                            "subtask": current["subtask"],
                        }])
                        self.logger.info(f"[demo] 子任务失败 → 重插队尾重做: {current['subtask']}")
                    continue
                self.logger.info(f"[Camera] 子任务状态={self._last_subtask_status}，拍照诊断...")
                if self.config.get('camera', {}).get('enabled', True):
                    self._send_camera_task(
                        robot_name, task_id,
                        f"拍照诊断：刚执行'{current['subtask']}'失败（{self._last_subtask_status}），检查当前场景状态"
                    )

                # VLM 发现异常时，让 Master LLM 决定如何调整
                if self._last_subtask_result and "abnormal" in self._last_subtask_result:
                    self.logger.info(f"[Camera] VLM 发现异常，触发重规划...")
                    vlm_feedback = self._last_subtask_result
                    new_tasks, remove_tasks = self._incremental_replan(
                        task, task_queue, [], vlm_feedback=vlm_feedback
                    )
                    if remove_tasks:
                        task_queue.remove_pending_tasks(remove_tasks)
                        self.logger.info(f"[Camera] 移除任务: {remove_tasks}")
                    if new_tasks:
                        task_queue.append_tasks(new_tasks)
                        self.logger.info(f"[Camera] 新增任务: {[t['subtask'] for t in new_tasks]}")

                # ALFWorld 模式：仅在关键步骤（clean/place/heat/put）失败时中止
                # 搜索子任务（检查容器）即使失败也不中止，继续检查下一个
                _critical_keywords = ("clean", "清洁", "清洗", "place", "放置", "heat", "加热",
                                      "put", "cool", "冷却", "slice", "切", "sinkbasin",
                                      "countertop", "take", "抓取", "拿起")
                _is_critical = any(kw in current["subtask"].lower()
                                   for kw in _critical_keywords)
                if _is_critical:
                    try:
                        from robot_api.client import check_success
                        won_result = check_success()
                        if won_result.get("won") is not None:  # ALFWorld 有 won 信号
                            remaining = task_queue.get_remaining()
                            if remaining:
                                self.logger.info(
                                    f"[Dispatch] 关键子任务失败，中止剩余 {len(remaining)} 个子任务"
                                )
                                for t in remaining:
                                    task_queue.mark_done(
                                        t, status="failure",
                                        result=f"前置关键步骤失败（{current['subtask']}），已跳过"
                                    )
                            break
                    except Exception:
                        pass

            if task_id in self.terminated_tasks:
                self.logger.warning(f"[TERMINATE] Task {task_id} terminated, stopping")
                self.terminated_tasks.discard(task_id)
                break

            # 每个子任务完成后，检查是否有场景变化需要增量规划
            with self._scene_changes_lock:
                changes = self.pending_scene_changes[:]
                self.pending_scene_changes.clear()

            if changes:
                new_tasks, remove_tasks = self._incremental_replan(task, task_queue, changes)
                if remove_tasks:
                    task_queue.remove_pending_tasks(remove_tasks)
                    self.logger.info(
                        f"[IncrementalReplan] Removed tasks: {remove_tasks}"
                    )
                if new_tasks:
                    task_queue.append_tasks(new_tasks)
                    self.logger.info(
                        f"[IncrementalReplan] Added {len(new_tasks)} new tasks: "
                        f"{[t['subtask'] for t in new_tasks]}"
                    )

        # 所有子任务完成后，拍照验证最终场景（仅当 dispatch 未被取代时）
        if (
            robot_name
            and task_queue.all_done()
            and not _superseded()
            and not task_had_failure
        ):
            self.logger.info("[Camera] 所有子任务完成，拍照验证最终场景...")
            if self.config.get('camera', {}).get('enabled', True):
                self._send_camera_task(
                    robot_name, task_id,
                    f"拍照验证：原始任务'{task}'的所有子任务已完成，检查最终场景是否符合预期"
                )
            if self._last_subtask_result and "abnormal" in self._last_subtask_result:
                self.logger.warning(f"[Camera] 最终验证发现异常: {self._last_subtask_result}")
            else:
                self.logger.info("[Camera] 最终验证通过，场景正常")

        self.logger.info(f"Task_id ({task_id}) [{task}] all done.")

        if not _superseded():
            all_tasks = task_queue.tasks
            task_desc = task if isinstance(task, str) else str(task)
            completed_cnt = len(task_queue.get_completed())
            total_cnt = len(all_tasks)

            # ── 自动学习：用后端 won 信号做最终裁判 ──────────────────────────────
            self._auto_learn(task_desc, task_had_failure)
            # ─────────────────────────────────────────────────────────────────────

            if not task_queue.all_done() or task_had_failure:
                self._pending_failure = {
                    "task_id": task_id,
                    "task_desc": task_desc,
                    "completed": completed_cnt,
                    "total": total_cnt,
                }
                self.logger.info(f"[Experience] Task failed, waiting for human input")
            else:
                self._pending_success = {
                    "task_id": task_id,
                    "task_desc": task_desc,
                    "completed": completed_cnt,
                    "total": total_cnt,
                }
                self.logger.info(f"[Experience] Task succeeded, asking user for optional experience")

    def _send_camera_task(self, robot_name: str, task_id: str, description: str):
        """发送拍照子任务并等待完成。"""
        subtask_data = {
            "task_id": task_id,
            "task": description,
            "order": "true",
        }
        self._last_subtask_status = None
        self._last_subtask_result = None
        self.collaborator.update_agent_busy(robot_name, True)
        self.collaborator.send(
            f"fqplanner_to_{robot_name}", json.dumps(subtask_data)
        )
        self.collaborator.wait_agents_free([robot_name])
        self.logger.info(f"[Camera] 拍照完成，状态: {self._last_subtask_status}")

    def get_task_status(self) -> Dict:
        """返回当前任务的执行状态，供前端查询。"""
        if not self.current_task_queue:
            return {"active": False}

        q = self.current_task_queue
        tasks = []
        for t in q.tasks:
            tasks.append({
                "order": t["order"],
                "robot_name": t["robot_name"],
                "subtask": t["subtask"],
                "done": t["done"],
                "status": t.get("status"),  # None | "success" | "failure" | "exception" | "timeout"
                "inserted": t.get("inserted", False),
            })

        failed = any(
            t["done"] and t.get("status") in ("failure", "exception", "timeout")
            for t in tasks
        )
        reception_running = getattr(self, "_reception_running", False)
        result = {
            "active": True,
            "task_id": self.current_task_id,
            "task": self.current_task_desc,
            "reasoning": getattr(self, "current_reasoning", ""),
            "all_done": q.all_done() and not reception_running,
            "failed": failed,
            "total": len(tasks),
            "completed": len([t for t in tasks if t["done"]]),
            "subtask_list": tasks,
        }
        if self._reception_state is not None:
            result["reception_state"] = self._reception_state
        # ALFWorld: expose ground-truth won signal separately from subtask status
        try:
            from robot_api.client import check_success
            won_result = check_success()
            if won_result.get("won") is not None:
                result["won"] = won_result["won"]
        except Exception:
            pass
        return result

    def _incremental_replan(
        self, original_task: str, task_queue: TaskQueue, changes: list, vlm_feedback: str = None
    ) -> tuple:
        """增量规划：返回需要新增和移除的子任务。
        vlm_feedback: VLM 拍照诊断的反馈（可选），包含场景异常描述。
        """
        changes_summary = self._summarize_changes(changes)

        # 读取当前场景状态
        all_env = self.collaborator.read_environment(None)
        scene_str = "无"
        if all_env:
            parts = []
            for key, val in all_env.items():
                if key == "robot":
                    continue
                if isinstance(val, str):
                    val = json.loads(val)
                contains = val.get("contains")
                if isinstance(contains, list):
                    desc = val.get("description", key)
                    parts.append(f"  {key}（{desc}）: {', '.join(contains) if contains else '空'}")
            scene_str = "\n".join(parts) if parts else "无"

        completed = task_queue.get_completed()
        remaining = task_queue.get_remaining()

        completed_str = "\n".join(f"  - {t['subtask']} (done)" for t in completed) or "  无"
        remaining_str = "\n".join(f"  - {t['subtask']}" for t in remaining) or "  无"

        replan_prompt = f"""原始任务：{original_task}

当前场景状态：
{scene_str}

已完成的子任务：
{completed_str}

剩余的子任务：
{remaining_str}

场景变化：{changes_summary}
{f"VLM 视觉反馈：{vlm_feedback}" if vlm_feedback else ""}

请根据当前场景状态和场景变化，判断需要调整的子任务：
- 如果有新物体出现需要处理，在 new_subtasks 中添加
- 如果某个剩余子任务的目标已不存在（如物体从场景中消失），在 remove_subtasks 中列出对应的子任务描述
- 如果场景变化不需要额外操作（如机器人自己抓取导致的移除），两个列表都为空
- 如果 VLM 视觉反馈显示场景异常（如物体掉落、位置不对），在 new_subtasks 中添加修正子任务

输出格式：
{{
    "reasoning": "判断理由",
    "new_subtasks": [
        {{"robot_name": "FQrobot", "subtask": "xxx"}}
    ],
    "remove_subtasks": [
        "要移除的子任务描述"
    ]
}}
不需要操作时，两个列表都为空列表。"""

        try:
            response = self.planner.forward(replan_prompt, self.conversation_history)
            self.logger.info(f"[IncrementalReplan] LLM response: {response}")

            result = self._extract_json(response)
            if result:
                new = result.get("new_subtasks") or []
                remove = result.get("remove_subtasks") or []
                return new, remove
        except Exception as e:
            self.logger.error(f"[IncrementalReplan] Error: {e}")

        return [], []

    def _summarize_changes(self, changes: list) -> str:
        """将场景变化列表格式化为自然语言摘要。"""
        summary_parts = []
        for change in changes:
            loc = change.get("location", "未知位置")
            added = change.get("added", [])
            removed = change.get("removed", [])
            parts = []
            if added:
                parts.append(f"新增了 {', '.join(added)}")
            if removed:
                parts.append(f"移除了 {', '.join(removed)}")
            if parts:
                summary_parts.append(f"{loc} 中" + "，".join(parts))
        return "；".join(summary_parts) if summary_parts else "无变化"

    def _load_experiences(self, task: str = "") -> str:
        os.makedirs(self._skills_dir, exist_ok=True)
        skill_name = self._get_skill_name(task or self.current_task_desc or "")

        parts = []
        skill_file = os.path.join(self._skills_dir, f'{skill_name}.md')
        if os.path.exists(skill_file):
            with open(skill_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if content:
                parts.append(f"### {skill_name} 专项经验\n{content}")

        if skill_name != 'multi_step':
            multi_file = os.path.join(self._skills_dir, 'multi_step.md')
            if os.path.exists(multi_file):
                with open(multi_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if content:
                    parts.append(f"### 复合任务经验\n{content}")

        if not parts:
            return ""

        exploration_hint = ""
        if self._exploration_rate > 0:
            explore_pct = int(self._exploration_rate * 100)
            refer_pct = 100 - explore_pct
            exploration_hint = f"\n> 参考策略：{refer_pct}% 借鉴以下经验，{explore_pct}% 自由探索新方案。"

        return f"\n\n## 过往经验（请参考）：{exploration_hint}\n" + "\n\n".join(parts)

    def _auto_learn(self, task_desc: str, task_had_failure: bool) -> None:
        """Query backend won signal; auto-generate and save experience without human gate."""
        try:
            from robot_api.client import check_success, get_scene_state
            won_result = check_success()
            won = won_result.get("won") if isinstance(won_result, dict) else None
            if won is None:
                return  # backend doesn't support success check (MuJoCo etc.)

            # Get final observation for richer experience note
            note = ""
            try:
                snap = get_scene_state()
                if snap:
                    obs = snap.get("observation") or ""
                    holding = snap.get("holding")
                    note = obs[:300]
                    if holding:
                        note = f"手持: {holding}. " + note
                    elif "not carrying" in obs.lower() or "你没有" in obs:
                        note = "执行结束时手持为空（物体未被成功拾取）. " + note
            except Exception:
                pass

            if not won:
                self.logger.info(f"[AutoLearn] won=False → 自动记录失败经验")
                self._pending_failure = {"task_id": "", "task_desc": task_desc,
                                         "completed": 0, "total": 0}
                self.save_experience(exp_type="negative", note=note or "任务未完成")
            else:
                self.logger.info(f"[AutoLearn] won=True → 自动记录成功经验")
                self._pending_success = {"task_id": "", "task_desc": task_desc,
                                         "completed": 0, "total": 0}
                self.save_experience(exp_type="positive", note=note or "任务成功完成")
        except Exception as e:
            self.logger.warning(f"[AutoLearn] 自动学习失败，跳过: {e}")

    def classify_and_save_failure_experience(self, raw_input: str) -> dict:
        """LLM 将人工输入的失败经验归类到对应 skill 文件的避免规则中。"""
        os.makedirs(self._skills_dir, exist_ok=True)
        task_desc = (self._pending_failure or {}).get('task_desc', self.current_task_desc or '未知任务')
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        from agents.prompts import EXPERIENCE_CLASSIFY
        prompt = EXPERIENCE_CLASSIFY.format(task_desc=task_desc, raw_input=raw_input)
        try:
            response = self.planner.generate(prompt)
            result = self._extract_json(response)
            if not result:
                raise ValueError("LLM returned invalid JSON")
            skill = result.get('skill', 'multi_step')
            rule = result.get('rule', raw_input)
            if skill not in ('navigate', 'grasp', 'place', 'multi_step'):
                skill = 'multi_step'
        except Exception as e:
            self.logger.warning(f"[Experience] LLM classification failed: {e}")
            skill = self._get_skill_name(task_desc)
            rule = raw_input

        skill_file = os.path.join(self._skills_dir, f'{skill}.md')
        entry = f"\n### {date_str}\n- 任务：{task_desc}\n- 规则：{rule}\n"

        if os.path.exists(skill_file):
            with open(skill_file, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            content = f"# {skill} Skill 经验库\n\n## 避免规则\n"

        if '## 避免规则' not in content:
            content += '\n## 避免规则\n'
        pos = content.find('## 避免规则') + len('## 避免规则')
        content = content[:pos] + entry + content[pos:]

        with open(skill_file, 'w', encoding='utf-8') as f:
            f.write(content)

        self._pending_failure = None
        self.logger.info(f"[Experience] Saved failure rule to {skill}.md: {rule}")
        return {"success": True, "skill": skill, "rule": rule}

    def classify_and_save_success_experience(self, raw_input: str) -> dict:
        """LLM 将人工输入的正向经验归类到对应 skill 文件的正向经验区块中。"""
        os.makedirs(self._skills_dir, exist_ok=True)
        task_desc = (self._pending_success or {}).get('task_desc', self.current_task_desc or '未知任务')
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        from agents.prompts import EXPERIENCE_CLASSIFY_SUCCESS
        prompt = EXPERIENCE_CLASSIFY_SUCCESS.format(task_desc=task_desc, raw_input=raw_input)
        try:
            response = self.planner.generate(prompt)
            result = self._extract_json(response)
            if not result:
                raise ValueError("LLM returned invalid JSON")
            skill = result.get('skill', 'multi_step')
            tip = result.get('tip', raw_input)
            if skill not in ('navigate', 'grasp', 'place', 'multi_step'):
                skill = 'multi_step'
        except Exception as e:
            self.logger.warning(f"[Experience] LLM classification failed: {e}")
            skill = self._get_skill_name(task_desc)
            tip = raw_input

        skill_file = os.path.join(self._skills_dir, f'{skill}.md')
        entry = f"\n### {date_str}\n- 任务：{task_desc}\n- 策略：{tip}\n"

        if os.path.exists(skill_file):
            with open(skill_file, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            content = f"# {skill} Skill 经验库\n\n## 正向经验\n\n## 避免规则\n"

        section = '## 正向经验'
        if section not in content:
            content = content.replace('## 避免规则', f'{section}\n\n## 避免规则')
        pos = content.find(section) + len(section)
        content = content[:pos] + entry + content[pos:]

        with open(skill_file, 'w', encoding='utf-8') as f:
            f.write(content)

        self._pending_success = None
        self.logger.info(f"[Experience] Saved success tip to {skill}.md: {tip}")
        return {"success": True, "skill": skill, "tip": tip}

    def save_experience(self, task_id: str = "", exp_type: str = None, note: str = ""):
        """保存经验到对应 skill 文件，note 是失败原因或成功备注"""
        os.makedirs(self._skills_dir, exist_ok=True)
        if exp_type is None:
            exp_type = task_id if task_id in ("positive", "negative") else "positive"

        task_desc = self.current_task_desc or "未知任务"
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        subtask_summary = "无子任务记录"
        if self.current_task_queue:
            lines = []
            for t in self.current_task_queue.tasks:
                status_label = t.get("status", "未执行") if t["done"] else "未执行"
                result_preview = str(t.get("result", "")).strip()[:120]
                result_part = f" → {result_preview}" if result_preview else ""
                lines.append(f"  - [{status_label}] {t['subtask']}{result_part}")
            subtask_summary = "\n".join(lines)

        # 从子任务结果里提取「物体→位置」并持久化，供 search_and_grasp 优先搜索
        self._update_location_memory(subtask_summary)

        feedback_type = "正向（方案有效）" if exp_type == "positive" else "负向（方案有问题）"
        user_note_section = f"用户备注（失败原因）：{note}" if note else ""

        from agents.prompts import EXPERIENCE_GENERATION
        prompt = EXPERIENCE_GENERATION.format(
            task=task_desc,
            subtask_summary=subtask_summary,
            feedback_type=feedback_type,
            user_note_section=user_note_section,
        )

        try:
            response = self.planner.generate(prompt)
            experience_text = response.strip()
            if experience_text.startswith("```"):
                experience_text = re.sub(r"^```\w*\n?", "", experience_text)
                experience_text = experience_text.rstrip("`").strip()
            if not experience_text:
                experience_text = note or ("此方案有效，可复用" if exp_type == "positive" else "此方案有问题，需避免")
        except Exception as e:
            self.logger.warning(f"[Experience] LLM generation failed: {e}")
            experience_text = note or ("此方案有效，可复用" if exp_type == "positive" else "此方案有问题，需避免")

        # 写入对应 skill 文件
        skill_name = self._get_skill_name(task_desc)
        skill_file = os.path.join(self._skills_dir, f'{skill_name}.md')

        if exp_type == "positive":
            section = "## 正向经验"
            entry = f"\n### {date_str} {task_desc}\n- 任务：{task_desc}\n- 教训：{experience_text}\n"
        else:
            section = "## 避免规则"
            entry = f"\n### {date_str} {task_desc}\n- 任务：{task_desc}\n- 规则：{experience_text}\n"

        if os.path.exists(skill_file):
            with open(skill_file, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            content = f"# {skill_name} Skill 经验库\n\n## 正向经验\n\n## 避免规则\n"

        section_pos = content.find(section)
        if section_pos == -1:
            content += f"\n{section}\n{entry}"
        else:
            insert_pos = section_pos + len(section)
            content = content[:insert_pos] + "\n" + entry + content[insert_pos:]

        with open(skill_file, 'w', encoding='utf-8') as f:
            f.write(content)

        self.logger.info(f"[Experience] Saved {exp_type} to {skill_name}.md: {experience_text}")
        return {"success": True, "skill": skill_name, "message": f"经验已保存到 {skill_name}.md", "experience": experience_text}

    def _update_location_memory(self, subtask_summary: str) -> None:
        """从子任务结果里解析 'You pick up X from Y' 并写入 location_memory.json。"""
        import json as _json
        location_file = os.path.join(os.path.dirname(self._skills_dir), "location_memory.json")
        try:
            mem = _json.loads(open(location_file, encoding="utf-8").read()) if os.path.exists(location_file) else {}
        except Exception:
            mem = {}
        updated = False
        # 匹配 "You pick up the plate 1 from the shelf 2."
        for m in re.finditer(
            r"pick up the (\S+)\s+\d+\s+from the ([\w ]+?\d+)",
            subtask_summary, re.IGNORECASE
        ):
            obj = m.group(1).strip().lower()   # "plate"
            loc = m.group(2).strip().lower()   # "shelf 2"
            mem[obj] = {"location": loc, "updated": datetime.now().strftime("%Y-%m-%d")}
            self.logger.info(f"[Location] {obj} → {loc}")
            updated = True
        if updated:
            try:
                with open(location_file, "w", encoding="utf-8") as f:
                    _json.dump(mem, f, indent=2, ensure_ascii=False)
            except Exception as e:
                self.logger.warning(f"[Location] 写入失败: {e}")

    def get_experiences(self) -> str:
        """返回经验库全文（供前端展示）。"""
        parts = []
        if os.path.exists(self._experience_file):
            with open(self._experience_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                parts.append(content)

        if os.path.isdir(self._skills_dir):
            for skill in ("navigate", "grasp", "place", "multi_step"):
                path = os.path.join(self._skills_dir, f"{skill}.md")
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    parts.append(content)

        return "\n\n".join(parts)
