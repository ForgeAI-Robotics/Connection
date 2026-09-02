"""
机器人技能统一入口
初始化 MCP 服务器，注册所有功能模块。
"""

import sys
import os
import signal
from mcp.server.fastmcp import FastMCP

from module.base import register_tools as register_base_tools
from module.grasp import register_tools as register_grasp_tools
from module.place import register_tools as register_place_tools
from module.camera import register_tools as register_camera_tools
from module.example import register_tools as register_example_tools
from module.raw import register_tools as register_raw_tools
from module.search import register_tools as register_search_tools
from module.tidy import register_tools as register_tidy_tools

mcp = FastMCP("robots")


def signal_handler(signum, frame):
    print(f"\n[skill.py] 收到信号 {signum}，正在退出...", file=sys.stderr)
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def register_all_modules():
    print("[skill.py] 开始注册机器人技能模块...", file=sys.stderr)

    register_base_tools(mcp)
    register_grasp_tools(mcp)
    register_place_tools(mcp)
    register_camera_tools(mcp)
    register_raw_tools(mcp)
    register_search_tools(mcp)
    register_tidy_tools(mcp)
    # register_example_tools(mcp)

    print("[skill.py] ✓ 所有模块注册完成", file=sys.stderr)


if __name__ == "__main__":
    print(f"[skill.py] 工作目录: {os.getcwd()}", file=sys.stderr)

    register_all_modules()

    print("[skill.py] MCP 服务器准备就绪，按 Ctrl+C 停止\n", file=sys.stderr)

    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        print("\n[skill.py] 服务器已停止", file=sys.stderr)
    except Exception as e:
        print(f"\n[skill.py] 服务器错误: {e}", file=sys.stderr)
        sys.exit(1)
