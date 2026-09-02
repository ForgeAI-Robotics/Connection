"""nav_handoff.py - 我方 /nav {x,y,target_yaw} → 导航侧任务契约的转换与交接。

对齐导航建图组的文件契约(navigation_task.yaml / navigation_result_template.yaml,
schema g1_dream_navigation_task/v1 / g1_navigation_result/v1):
  - 我方(大脑)写一个带版本号的任务目录: <exchange>/tasks/<task_id>/navigation_task.yaml
    并原子更新 <exchange>/tasks/latest_task.yaml 清单(对齐他们 recommended_transfer)。
  - 导航侧执行后写回同目录 navigation_result.yaml(status: SUCCEEDED/ABORTED/...)。
  - 我方轮询 result 文件,映射成 {success, result} 返回给 robot_api。

朝向:内部 target_yaw 用「度」(waypoints.yaml 的 yaw_deg);契约用弧度 + 四元数,此处转换。
"""

import math
import os
import time
from datetime import datetime

import yaml

TERMINAL_STATUS = {"SUCCEEDED", "CANCELED", "ABORTED", "REJECTED"}


def yaw_rad_to_quat(yaw):
    return {"x": 0.0, "y": 0.0, "z": math.sin(yaw / 2.0), "w": math.cos(yaw / 2.0)}


def build_task(x, y, yaw_deg=None, position_tolerance_m=0.35, yaw_tolerance_deg=25.0,
               task_prefix="fq_brain_nav"):
    yaw_rad = math.radians(yaw_deg) if yaw_deg is not None else 0.0
    task_id = f"{task_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    return {
        "schema_version": "g1_dream_navigation_task/v1",
        "task": {
            "task_id": task_id,
            "revision": 1,
            "type": "brain_goal",
            "generated_at": datetime.now().astimezone().isoformat(),
            "execution_status": "ready_with_preconditions",
        },
        "frames": {
            "global_frame": "map",
            "robot_base_frame": "base_link",
            "pose_convention": "x_m, y_m, yaw_rad; yaw is counter-clockwise about +Z",
        },
        "goal": {
            "selection": "fqplanner workpoint",
            "pose_xyt_rad": [float(x), float(y), yaw_rad],
            "quaternion_xyzw": yaw_rad_to_quat(yaw_rad),
            "position_tolerance_m": position_tolerance_m,
            "yaw_tolerance_deg": yaw_tolerance_deg,
            "yaw_specified": yaw_deg is not None,
        },
        "nav2_navigate_to_pose": {
            "action_name": "/navigate_to_pose",
            "action_type": "nav2_msgs/action/NavigateToPose",
            "goal": {
                "pose": {
                    "header": {"frame_id": "map"},
                    "pose": {
                        "position": {"x": float(x), "y": float(y), "z": 0.0},
                        "orientation": yaw_rad_to_quat(yaw_rad),
                    },
                },
                "behavior_tree": "",
            },
        },
    }


def write_task(exchange_dir, task):
    """写版本目录 + 原子更新 latest 清单(对齐对方 update_policy.recommended_transfer)。"""
    task_id = task["task"]["task_id"]
    task_dir = os.path.join(exchange_dir, "tasks", task_id)
    os.makedirs(task_dir, exist_ok=True)
    task_path = os.path.join(task_dir, "navigation_task.yaml")
    with open(task_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(task, f, allow_unicode=True, sort_keys=False)

    manifest = {"task_id": task_id, "revision": task["task"]["revision"], "path": task_path}
    latest = os.path.join(exchange_dir, "tasks", "latest_task.yaml")
    tmp = latest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(manifest, f, allow_unicode=True)
    os.replace(tmp, latest)
    return task_dir


def poll_result(task_dir, timeout_sec=180.0, interval_sec=1.0):
    """轮询导航侧写回的 navigation_result.yaml,直到终态或超时。"""
    result_path = os.path.join(task_dir, "navigation_result.yaml")
    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        if os.path.isfile(result_path):
            try:
                with open(result_path, encoding="utf-8") as f:
                    last = yaml.safe_load(f) or {}
            except Exception:
                last = None  # 对方可能写到一半,下轮重读
            if last and last.get("status") in TERMINAL_STATUS:
                return last
        time.sleep(interval_sec)
    return last or {"status": "TIMEOUT", "message": f"{timeout_sec}s 内未收到导航结果"}


def result_to_response(result):
    status = (result or {}).get("status", "UNKNOWN")
    ok = status == "SUCCEEDED"
    return {
        "success": ok,
        "result": f"导航{'完成' if ok else '失败'}: status={status}"
                  + (f", {result.get('message')}" if result.get("message") else ""),
        "nav_status": status,
        "final_pose_xyt_rad": (result or {}).get("final_pose_xyt_rad"),
    }
