# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Dict, List, Optional

# Bypass system proxy for API calls
os.environ['NO_PROXY'] = '*'

# 配置日志级别，抑制MCP服务器的详细日志输出
logging.getLogger("mcp").setLevel(logging.WARNING)

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import yaml
from slaver.agents.models import AzureOpenAIServerModel, OpenAIServerModel
from slaver.agents.slaver_agent import ToolCallingAgent
from agent.collaboration import Collaborator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from slaver.tools.utils import Config
from slaver.tools.tool_matcher import ToolMatcher
from slaver.tools.monitoring import SceneDetector

config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
config = Config.load_config(config_path)
collaborator = Collaborator.from_config(config=config["collaborator"])


class RobotManager:
    """Centralized robot management system with task handling and collaboration"""

    def __init__(self):
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self.collaborator = collaborator
        self.heartbeat_interval = 60
        self.lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self.model, self.model_path = self._gat_model_info_from_config()
        self.tools = None
        self.threads = []
        self.loop = asyncio.get_event_loop()
        self.robot_name = None

        # Initialize tool matcher with configuration
        self.tool_matcher = ToolMatcher(
            max_tools=config["tool"]["matching"]["max_tools"],
            min_similarity=config["tool"]["matching"]["min_similarity"]
        )

        # Initialize scene detector
        self.scene_detector = SceneDetector(collaborator)

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        print(f"Received signal {signum}, shutting down...")
        self._shutdown_event.set()

    async def _safe_cleanup(self):
        if hasattr(self, "session") and self.session:
            await self.cleanup()

        for thread in self.threads:
            if thread.is_alive():
                thread.join(timeout=1.0)

    def _gat_model_info_from_config(self):
        """Initial model"""
        candidate = config["model"]["model_dict"]
        if candidate["cloud_model"] in config["model"]["model_select"]:
            if candidate["cloud_type"] == "azure":
                model_client = AzureOpenAIServerModel(
                    model_id=config["model"]["model_select"],
                    azure_endpoint=candidate["azure_endpoint"],
                    azure_deployment=candidate["azure_deployment"],
                    api_key=candidate["azure_api_key"],
                    api_version=candidate["azure_api_version"],
                    support_tool_calls=config["tool"]["support_tool_calls"],
                    profiling=config["profiling"],
                )
                model_name = config["model"]["model_select"]
            elif candidate["cloud_type"] == "default":
                model_client = OpenAIServerModel(
                    api_key=candidate["cloud_api_key"],
                    api_base=candidate["cloud_server"],
                    model_id=candidate["cloud_model"],
                    support_tool_calls=config["tool"]["support_tool_calls"],
                    profiling=config["profiling"],
                )
                model_name = config["model"]["model_select"]
            else:
                raise ValueError(f"Unsupported cloud type: {candidate['cloud_type']}")
            return model_client, model_name
        raise ValueError(f"Unsupported model: {config['model']['model_select']}")

    def handle_task(self, data: str) -> None:
        """Process incoming tasks with thread-safe operation"""
        if self._shutdown_event.is_set():
            return

        data = json.loads(data)
        task_data = {
            "task": data.get("task"),
            "task_id": data.get("task_id"),
            "refresh": data.get("refresh"),
            "order_flag": data.get("order_flag", "false"),
        }
        with self.lock:
            future = asyncio.run_coroutine_threadsafe(
                self._execute_task(task_data), self.loop
            )
            future.result()

    @staticmethod
    def _match_tool_by_keyword(task: str) -> str:
        """根据子任务动词关键词匹配工具，返回工具名或 None。
        导航类子任务里经常会出现"为抓取/放置做准备"，这时动词目标仍然是导航。
        """
        # ALFWorld 操作子任务：格式为 执行raw_action: <命令> （精确匹配，避免误伤搜索子任务）
        if '执行raw_action:' in task:
            return "raw_action"

        # ALFWorld 搜索抓取子任务：格式为 "搜索并抓取 X"。必须在"抓取"规则之前匹配，
        # 否则会被误判成 grasp_object。映射到 search_and_grasp 后 filtered_tools 只剩它一个，
        # slaver LLM 拿不到 raw_action，从根上杜绝"把 search_and_grasp 语法塞进 raw_action"。
        if '搜索' in task:
            return "search_and_grasp"

        navigation_keywords = ["导航", "前往", "走到", "移动到", "到达", "靠近"]
        if any(kw in task for kw in navigation_keywords):
            return "navigate_to_target"

        rules = [
            (["拍照", "截图", "拍张"], "capture_image"),
            (["放置", "放到", "搁到"], "place_on_top"),
            (["抓取", "拿起", "取走", "拾起", "捡起"], "grasp_object"),
        ]
        for keywords, tool_name in rules:
            if any(kw in task for kw in keywords):
                return tool_name
        return None

    async def _execute_task(self, task_data: Dict) -> None:
        """Internal task execution logic"""
        if self._shutdown_event.is_set():
            return

        os.makedirs("./.log", exist_ok=True)

        # Clear previous task status from Redis to avoid state pollution
        self.collaborator.clear_agent_status(self.robot_name)

        task = task_data["task"]

        # Bind every DREAM navigation leg to the Master task id without asking
        # the LLM to invent or copy transaction metadata.  The MCP robot skill
        # runs in a sibling process and reads this small atomic context file.
        try:
            from robot_api.config import load_robot_api_config
            if (load_robot_api_config().active_backend or "").lower() == "dream":
                context_dir = os.path.abspath(os.path.join(
                    os.path.dirname(__file__), '..', 'serve_dream', 'runtime'
                ))
                os.makedirs(context_dir, exist_ok=True)
                context_path = os.path.join(context_dir, 'current_agent_task.json')
                temporary = context_path + '.tmp'
                with open(temporary, 'w', encoding='utf-8') as handle:
                    json.dump({
                        'task_id': str(task_data.get('task_id') or ''),
                        'subtask': str(task or ''),
                        'updated_wall_time': time.time(),
                    }, handle, ensure_ascii=False, indent=2)
                os.replace(temporary, context_path)
        except Exception as exc:
            print(f"[slaver] DREAM task context write failed: {exc}", file=sys.stderr)

        # 优先用关键词匹配工具，匹配不到再走语义匹配
        matched_tool_name = self._match_tool_by_keyword(task)
        if matched_tool_name:
            # Compound tasks (e.g. "导航到X并清洗Y") need both navigate_to_target AND raw_action.
            # If the task has a conjunction word "并" after a navigation keyword, include both tools
            # so the agent can execute navigation then the subsequent raw action in one loop.
            is_compound_nav = (
                matched_tool_name == "navigate_to_target"
                and "并" in task
            )
            if is_compound_nav:
                include_names = {"navigate_to_target", "raw_action"}
                filtered_tools = [tool for tool in self.tools
                                   if tool.get("function", {}).get("name") in include_names]
            else:
                filtered_tools = [tool for tool in self.tools
                               if tool.get("function", {}).get("name") == matched_tool_name]
            if not filtered_tools:
                filtered_tools = self.tools  # 工具名不存在，回退全部
        else:
            matched_tools = self.tool_matcher.match_tools(task)
            if matched_tools:
                matched_tool_names = [tool_name for tool_name, _ in matched_tools]
                filtered_tools = [tool for tool in self.tools
                               if tool.get("function", {}).get("name") in matched_tool_names]
            else:
                filtered_tools = self.tools

        # ALFWorld search tasks need more steps (navigate to each receptacle + check/take)
        try:
            from robot_api.config import load_robot_api_config
            _is_alf = any(
                b.name == "alfworld" and b.enabled and b.required
                for b in load_robot_api_config().backends
            )
        except Exception:
            _is_alf = False

        agent = ToolCallingAgent(
            tools=filtered_tools,
            verbosity_level=2,
            model=self.model,
            model_path=self.model_path,
            log_file="./.log/agent.log",
            robot_name=self.robot_name,
            collaborator=self.collaborator,
            tool_executor=self.session.call_tool,
            max_steps=30 if _is_alf else 20,
        )

        result = await agent.run(task)
        # result may be a tuple (result_str, terminated_bool) or just a string
        if isinstance(result, tuple):
            result, terminated = result
        else:
            terminated = False
        self._send_result(
            robot_name=self.robot_name,
            task=task,
            task_id=task_data["task_id"],
            result=result,
            tool_call=agent.tool_call,
            terminated=terminated,
            status=agent._last_status,
        )

    def _send_result(
        self, robot_name: str, task: str, task_id: str, result: Dict, tool_call: List, terminated: bool = False, status: str = None
    ) -> None:
        """Send task results to collaboration channel"""
        if self._shutdown_event.is_set():
            return

        channel = f"{robot_name}_to_FQPlanner"
        payload = {
            "robot_name": robot_name,
            "subtask_handle": task,
            "subtask_result": result,
            "tools": tool_call,
            "task_id": task_id,
            "terminated": terminated,
            "status": status,  # success/failure/none/exception/timeout
        }
        self.collaborator.send(channel, json.dumps(payload))

    def _heartbeat_loop(self, robot_name) -> None:
        """Continuous heartbeat signal emitter"""
        key = robot_name
        while not self._shutdown_event.is_set():
            try:
                self.collaborator.agent_heartbeat(key, seconds=60)
                time.sleep(30)
            except Exception as e:
                if not self._shutdown_event.is_set():
                    print(f"Heartbeat error: {e}")
                break

    async def connect_to_robot(self):
        """Connect to an MCP server"""

        call_type = config["robot"]["call_type"]

        if call_type == "local":
            robot_path = config["robot"]["path"]
            if not os.path.isabs(robot_path):
                robot_path = os.path.join(os.path.dirname(__file__), robot_path)
            server_params = StdioServerParameters(
                command=sys.executable,
                args=[os.path.join(robot_path, "skill.py")],
                env=None,
            )
            mcp_client = stdio_client(server_params)

        if call_type == "remote":
            mcp_client = streamablehttp_client(config["robot"]["path"] + "/mcp")

        stdio_transport = await self.exit_stack.enter_async_context(mcp_client)
        if call_type == "local":
            self.stdio, self.write = stdio_transport
        if call_type == "remote":
            self.stdio, self.write, _ = stdio_transport
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(self.stdio, self.write)
        )

        await self.session.initialize()

        # init robot with initial position and coordinates
        self.collaborator.record_environment(
            "robot", json.dumps({
                "position": "entrance",
                "coordinates": [0.0, 0.0, 0.0],
                "holding": None,
                "status": "idle"
            })
        )

        # List available tools
        response = await self.session.list_tools()

        # 根据模型类型选择工具格式
        # robobrain 使用原有格式，其他模型使用 OpenAI 标准格式
        use_openai_format = not self.model_path.startswith("robobrain")

        if use_openai_format:
            # OpenAI 标准格式 (用于 Qwen 等模型)
            self.tools = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.inputSchema,
                    },
                }
                for tool in response.tools
            ]
        else:
            # RoboBrain 原有格式
            self.tools = [
                {
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                    },
                    "input_schema": tool.inputSchema,
                }
                for tool in response.tools
            ]
        # 只打印工具名称列表，不打印详细描述
        tool_names = [tool["function"]["name"] for tool in self.tools]
        print(f"Connected to robot with {len(self.tools)} tools: {', '.join(tool_names)}")

        # Train the tool matcher with the available tools
        self.tool_matcher.fit(self.tools)

        """Complete robot registration with thread management"""
        robot_name = config["robot"]["name"]
        self.robot_name = robot_name
        register = {
            "robot_name": robot_name,
            "robot_tool": self.tools,
            "robot_state": "idle",
            "timestamp": int(datetime.now().timestamp()),
        }
        with self.lock:
            # Registration thread
            self.collaborator.register_agent(
                robot_name, json.dumps(register), expire_second=60
            )

            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                daemon=True,
                args=(robot_name,),
                name=f"heartbeat_{robot_name}",
            )
            heartbeat_thread.start()
            self.threads.append(heartbeat_thread)

            # Command listener thread
            channel_b2r = f"fqplanner_to_{robot_name}"
            listener_thread = threading.Thread(
                target=lambda: self.collaborator.listen(channel_b2r, self.handle_task),
                daemon=True,
                name=channel_b2r,
            )
            listener_thread.start()
            self.threads.append(listener_thread)

            self.scene_detector.start()

    async def cleanup(self):
        """Clean up resources"""
        self._shutdown_event.set()
        await self.exit_stack.aclose()


async def main():
    robot_manager = RobotManager()
    try:
        print("connecting to robot...")
        await robot_manager.connect_to_robot()
        print("connection success")

        while not robot_manager._shutdown_event.is_set():
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await robot_manager._safe_cleanup()
        print("Cleanup completed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Program terminated by user")
        sys.exit(0)
