#!/usr/bin/env python3
"""nav_task_watcher.py — 导航任务监听模板(跑在导航侧机器,需 ROS2 + Nav2 环境)。

给导航建图组的参考实现:盯着共享目录 tasks/ → 有新任务就发 NavigateToPose →
执行完按 g1_navigation_result/v1 写回 navigation_result.yaml。改个目录路径即可用。

用法(导航侧,Nav2 已 bringup、定位正常后):
  python3 nav_task_watcher.py --exchange /path/to/shared_dir [--poll 1.0]

⚠ 未测试模板:大脑侧无 ROS2 环境,本文件按契约编写、未实测,联调时如有问题联系毕艺恒。
"""

import argparse
import os
import time
from datetime import datetime

import yaml

import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose


def now_iso():
    return datetime.now().astimezone().isoformat()


class NavTaskWatcher(Node):
    def __init__(self, exchange_dir):
        super().__init__("fq_nav_task_watcher")
        self.exchange_dir = exchange_dir
        self.client = ActionClient(self, NavigateToPose, "navigate_to_pose")

    # ---------- 任务发现 ----------
    def pending_task(self):
        """读 latest 清单;任务存在且还没有 result 文件 → 待执行。"""
        manifest_path = os.path.join(self.exchange_dir, "tasks", "latest_task.yaml")
        if not os.path.isfile(manifest_path):
            return None
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = yaml.safe_load(f) or {}
        except Exception:
            return None  # 大脑可能正在写,下轮再读
        task_id = manifest.get("task_id")
        if not task_id:
            return None
        # 注意:清单里的 path 是大脑侧机器的路径,跨机无效 → 按本地共享目录重算
        task_dir = os.path.join(self.exchange_dir, "tasks", task_id)
        task_path = os.path.join(task_dir, "navigation_task.yaml")
        result_path = os.path.join(task_dir, "navigation_result.yaml")
        if not os.path.isfile(task_path) or os.path.isfile(result_path):
            return None
        with open(task_path, encoding="utf-8") as f:
            return task_dir, yaml.safe_load(f)

    # ---------- 结果写回 ----------
    def write_result(self, task_dir, task, status, started_at, message=""):
        data = {
            "schema_version": "g1_navigation_result/v1",
            "task_id": task["task"]["task_id"],
            "revision": task["task"]["revision"],
            "status": status,
            "started_at": started_at,
            "finished_at": now_iso(),
            "final_pose_xyt_rad": None,  # 可选:订阅 /amcl_pose 填真实终点位姿
            "position_error_m": None,
            "yaw_error_deg": None,
            "recovery_count": 0,
            "message": message,
        }
        result_path = os.path.join(task_dir, "navigation_result.yaml")
        tmp = result_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, result_path)  # 原子写,避免大脑读到半截
        self.get_logger().info(f"result -> {result_path}: {status}")

    # ---------- 执行 ----------
    def execute(self, task_dir, task):
        started_at = now_iso()
        pose_cfg = task["nav2_navigate_to_pose"]["goal"]["pose"]["pose"]

        goal = NavigateToPose.Goal()
        ps = PoseStamped()
        ps.header.frame_id = "map"
        ps.pose.position.x = float(pose_cfg["position"]["x"])
        ps.pose.position.y = float(pose_cfg["position"]["y"])
        ps.pose.position.z = 0.0
        ps.pose.orientation.x = float(pose_cfg["orientation"]["x"])
        ps.pose.orientation.y = float(pose_cfg["orientation"]["y"])
        ps.pose.orientation.z = float(pose_cfg["orientation"]["z"])
        ps.pose.orientation.w = float(pose_cfg["orientation"]["w"])
        goal.pose = ps

        if not self.client.wait_for_server(timeout_sec=10.0):
            self.write_result(task_dir, task, "REJECTED", started_at,
                              "navigate_to_pose action server 不可用")
            return

        send_future = self.client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, send_future)
        handle = send_future.result()
        if handle is None or not handle.accepted:
            self.write_result(task_dir, task, "REJECTED", started_at, "goal 被 Nav2 拒绝")
            return

        self.get_logger().info(f"executing {task['task']['task_id']} "
                               f"-> ({ps.pose.position.x:.3f}, {ps.pose.position.y:.3f})")
        result_future = handle.get_result_async()
        rclpy.spin_until_future_complete(self, result_future)
        status_code = result_future.result().status

        status = {
            GoalStatus.STATUS_SUCCEEDED: "SUCCEEDED",
            GoalStatus.STATUS_CANCELED: "CANCELED",
            GoalStatus.STATUS_ABORTED: "ABORTED",
        }.get(status_code, "ABORTED")
        self.write_result(task_dir, task, status, started_at)


def main():
    parser = argparse.ArgumentParser(description="FQPlanner 导航任务监听(导航侧)")
    parser.add_argument("--exchange", required=True, help="共享交换目录路径")
    parser.add_argument("--poll", type=float, default=1.0, help="轮询间隔(秒)")
    args = parser.parse_args()

    rclpy.init()
    node = NavTaskWatcher(args.exchange)
    node.get_logger().info(f"watching {args.exchange}/tasks/ ...")
    try:
        while rclpy.ok():
            pending = node.pending_task()
            if pending:
                node.execute(*pending)
            else:
                time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
