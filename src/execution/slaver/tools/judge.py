"""
失败判定模块 - 模拟模型判断

工具执行失败后，随机决定下一步动作：
  > 0.4  → retry    （重试当前工具）
  0.01~0.4 → skip     （放弃当前子任务，继续下一个）
  < 0.01  → terminate （终止整个任务）
"""

import random
import sys


def judge_on_failure(tool_name: str, observation: str) -> str:
    """工具执行失败后调用，返回 "retry" / "skip" / "terminate"。"""
    r = random.random()
    if r >= 0.4:
        decision = "retry"
    elif r >= 0.01:
        decision = "skip"
    else:
        decision = "terminate"

    print(f"[Judge] tool={tool_name}, rand={r:.3f}, decision={decision}", file=sys.stderr)
    return decision
