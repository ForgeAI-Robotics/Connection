"""
raw_action — 执行任意合法命令（ALFWorld 专用）。
用于 open/close/heat/cool/clean/toggle 等 navigate/grasp/place 之外的动作。
命令必须来自当前场景的 admissible_commands 列表，否则 ALFWorld 会回应 "Nothing happens."
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

import urllib.request
import urllib.error


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

from robot_api.config import load_robot_api_config


def _raw_post(command: str) -> dict:
    cfg = load_robot_api_config()
    for backend in cfg.backends:
        if not backend.enabled:
            continue
        url = f"{backend.url}/raw"
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"command": command}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=backend.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.URLError:
            continue
        except Exception as e:
            return {"success": False, "result": str(e)}
    return {"success": False, "result": "没有可用后端支持 /raw"}


def register_tools(mcp):

    @mcp.tool()
    async def raw_action(command: str) -> str:
        """执行一条合法的文本命令（ALFWorld 模式专用）。

        适用于 open、close、heat、cool、clean、toggle、examine 等
        navigate_to_target / grasp_object / place_on_top 无法覆盖的动作。
        命令字符串必须与场景 admissible_commands 中的某一条完全一致。

        Args:
            command: 合法命令字符串，例如 "open drawer 1"、"close cabinet 2"、
                     "heat apple 1 with microwave 1"、"clean sponge 1 with sink 1"

        Returns:
            包含执行结果的 JSON 字符串。
        """
        print(f"[raw] 执行命令: '{command}'", file=sys.stderr)
        result = _raw_post(command)
        obs = result.get("result", "")
        won = result.get("won", False)
        if result.get("success") is False or obs == "Nothing happens.":
            msg = obs or "命令未被接受（不在合法动作列表中）"
            print(f"[raw] ✗ {msg}", file=sys.stderr)
            return json.dumps([msg, {"_status": "failure"}])
        response = obs or f"命令 '{command}' 已执行"
        print(f"[raw] ✓ {response}", file=sys.stderr)
        suffix = " [TASK COMPLETE]" if won else ""
        # Only physical manipulation commands are terminal (they change object state or
        # achieve the sub-goal). Observation / navigation / access commands keep the loop
        # alive so the slaver can search multiple locations in one subtask.
        cmd = _norm(command)
        _TERMINAL = ("take ", "clean ", "heat ", "cool ", "put ", "move ", "slice ", "use ")
        is_terminal = won or any(cmd.startswith(p) for p in _TERMINAL)
        status = "success" if is_terminal else "navigated"
        return json.dumps([response + suffix, {"_status": status, "won": won}])

    print("[raw.py] raw_action 工具已注册", file=sys.stderr)
