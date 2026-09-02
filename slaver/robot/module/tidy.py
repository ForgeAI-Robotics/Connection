"""桌面整理技能级工具 —— 整理牛奶/可乐/笔筒/清垃圾。

demo「整理桌面」用。每个技能内部调 master/sop/skill_executor.execute_skill(走 robot_api →
当前后端,如 serve_desk mock / 将来 VLA),把该类物体归位/清走。子任务文本("整理牛奶"等)由
master demo 规划分支产出,slaver agent 匹配到这里对应的技能工具。
"""

import json
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
for _p in (_ROOT, os.path.join(_ROOT, "master", "sop")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from skill_executor import execute_skill, SKILLS  # noqa: E402


def _run(skill_name: str) -> str:
    label = SKILLS[skill_name]["label"]
    try:
        r = execute_skill(skill_name)
    except Exception as e:
        print(f"[tidy] {label} 异常: {e}", file=sys.stderr)
        return json.dumps([f"{label} 异常: {e}", {"_status": "failure"}], ensure_ascii=False)
    if r.get("success"):
        moved = r.get("moved", [])
        msg = (f"{label}完成,处理 {len(moved)} 个物体: {moved}" if moved
               else f"{label}: 已就位,无需处理")
        print(f"[tidy] ✓ {msg}", file=sys.stderr)
        return json.dumps([msg, {"_status": "success"}], ensure_ascii=False)
    msg = f"{label}失败: {r.get('fail_reason', '未知')}" + \
          (f"(物体 {r.get('fail_obj')})" if r.get("fail_obj") else "")
    print(f"[tidy] ✗ {msg}", file=sys.stderr)
    return json.dumps([msg, {"_status": "failure"}], ensure_ascii=False)


def register_tools(mcp):

    @mcp.tool()
    async def tidy_milk() -> str:
        """整理牛奶:把桌面上散落的牛奶盒归位到牛奶区。用于「整理牛奶」子任务。"""
        return _run("tidy_milk")

    @mcp.tool()
    async def tidy_cola() -> str:
        """整理可乐:把桌面上的可乐罐归位到饮料区。用于「整理可乐」子任务。"""
        return _run("tidy_cola")

    @mcp.tool()
    async def tidy_penholder() -> str:
        """整理笔筒:把笔筒归位到指定位置。用于「整理笔筒」子任务。"""
        return _run("tidy_penholder")

    @mcp.tool()
    async def clean_trash() -> str:
        """清理垃圾:把桌面上的纸巾团等垃圾丢进桌面垃圾桶。用于「清理垃圾」子任务。"""
        return _run("clean_trash")

    print("[tidy.py] 桌面整理技能已注册(整理牛奶/可乐/笔筒/清垃圾)", file=sys.stderr)
