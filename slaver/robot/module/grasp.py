"""
抓取控制模块。

工具层只调用统一 robot_api，不关心当前后端是 MuJoCo、Isaac Sim、Gazebo
还是真实机器人桥接。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from robot_api.client import grasp_object as _grasp_object

_SERVE_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'serve'))
if _SERVE_PATH not in sys.path:
    sys.path.insert(0, _SERVE_PATH)
from scene.scene_memory import move_object as _move_object


def register_tools(mcp):

    @mcp.tool()
    async def grasp_object(object_name: str = None) -> str:
        """抓取物体。

        机器人抓取指定物体。仅当机器人当前没有抓取任何物体时使用。
        如果已经抓取了物体，需要先使用 release_object 释放。

        Args:
            object_name: 要抓取的物体名称（如 "mug"、"apple"）。一次只能抓取一个物体。

        Returns:
            抓取结果，成功或失败信息。
        """
        target = object_name or "unknown_object"
        print(f"[grasp] 开始抓取 '{target}'...", file=sys.stderr)

        result = _grasp_object(target)

        if result.get("success"):
            response = result.get("result", f"成功抓取 {target}")
            state = {"_status": "success"}
            if "real_arm" in result:
                state["real_arm"] = result["real_arm"]
            print(f"[grasp] ✓ {response}", file=sys.stderr)
            try:
                _move_object(target, "robot_hand")
            except Exception as e:
                print(f"[grasp] 记忆更新失败: {e}", file=sys.stderr)
            return json.dumps([response, state], ensure_ascii=False)
        else:
            msg = result.get("result", f"抓取 {target} 失败，请重试。")
            state = {"_status": "failure"}
            if "real_arm" in result:
                state["real_arm"] = result["real_arm"]
            print(f"[grasp] ✗ {msg}", file=sys.stderr)
            return json.dumps([msg, state], ensure_ascii=False)

    print("[grasp.py] 抓取控制模块已注册 (robot_api)", file=sys.stderr)
