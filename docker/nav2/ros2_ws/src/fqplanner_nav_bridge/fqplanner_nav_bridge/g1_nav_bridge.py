#!/usr/bin/env python3
"""HTTP /nav facade for the G1control ROS2 navigation stack (topic-based, no action).

大脑(robot_api)走 HTTP;G1control(YeLuo-123/G1control 的 dijkstra_planner + nav2point)是纯 ROS2
topic。这个桥做翻译:
  POST /nav {x,y,target_yaw}  → 发 /g1pilot/goal(PoseStamped, map系) + 使能 /g1pilot/auto_enable,
     然后订阅 /lidar_odometry/pose_fixed 轮询,进 xy/yaw 容差并稳定 settle 秒 → {success:true}(同步)。
     ↑ 这套栈没有 NavigateToPose action 的完成回调,所以"到达"要桥自己看 odom 判(nav2point goal_tol≈0.1)。
  GET  /base_status           → 读最新 /lidar_odometry/pose_fixed → {pos:[x,y,0], yaw_deg}。
  其他                         → 501(抓/放/截图归 VLA 组后端)。

真机与仿真(navigation_demo / mujoco office)同一套 topic,桥不变。
用法(在能 source ROS2 + 跑得到那些 topic 的机器上):
  source /opt/ros/<distro>/setup.bash && source <G1control ws>/install/setup.bash
  python3 g1_nav_bridge.py            # 默认监听 0.0.0.0:5102
可调参数(rclpy --ros-args -p 名:=值): http_port / xy_tolerance / yaw_tolerance_deg / nav_timeout / settle_time
"""

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import QoSProfile
from std_msgs.msg import Bool


def yaw_to_quat(yaw):
    h = yaw * 0.5
    return 0.0, 0.0, math.sin(h), math.cos(h)


def yaw_from_quat(x, y, z, w):
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def norm_deg(a):
    return (float(a) + 180.0) % 360.0 - 180.0


class G1NavBridge(Node):
    def __init__(self):
        super().__init__("fqplanner_g1_nav_bridge")
        self.declare_parameter("http_host", "0.0.0.0")
        self.declare_parameter("http_port", 5102)
        self.declare_parameter("map_frame", "map")
        self.declare_parameter("goal_topic", "/g1pilot/goal")
        self.declare_parameter("odom_topic", "/lidar_odometry/pose_fixed")
        self.declare_parameter("auto_enable_topic", "/g1pilot/auto_enable")
        self.declare_parameter("nav_timeout", 120.0)
        self.declare_parameter("xy_tolerance", 0.20)        # nav2point goal_tol≈0.1,留余量
        self.declare_parameter("yaw_tolerance_deg", 180.0)  # nav2point 不控终点朝向 → 默认不卡 yaw
        self.declare_parameter("settle_time", 1.0)          # 进容差后再稳定这么久才算到达

        self.map_frame = str(self.get_parameter("map_frame").value)
        self.nav_timeout = float(self.get_parameter("nav_timeout").value)
        self.xy_tol = float(self.get_parameter("xy_tolerance").value)
        self.yaw_tol = float(self.get_parameter("yaw_tolerance_deg").value)
        self.settle = float(self.get_parameter("settle_time").value)

        qos = QoSProfile(depth=10)
        self.pub_goal = self.create_publisher(
            PoseStamped, str(self.get_parameter("goal_topic").value), qos)
        self.pub_enable = self.create_publisher(
            Bool, str(self.get_parameter("auto_enable_topic").value), qos)
        self.create_subscription(
            Odometry, str(self.get_parameter("odom_topic").value), self.cb_odom, qos)

        self._lock = threading.Lock()
        self._pose = None   # (x, y, yaw_rad, wall_time)

        host = str(self.get_parameter("http_host").value)
        port = int(self.get_parameter("http_port").value)
        self.server = ThreadingHTTPServer((host, port), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.get_logger().info(
            f"G1 nav bridge on http://{host}:{port} → goal={self.get_parameter('goal_topic').value}, "
            f"odom={self.get_parameter('odom_topic').value}")

    def cb_odom(self, msg: Odometry):
        p = msg.pose.pose.position
        q = msg.pose.pose.orientation
        with self._lock:
            self._pose = (float(p.x), float(p.y), yaw_from_quat(q.x, q.y, q.z, q.w), time.time())

    def get_pose(self):
        with self._lock:
            return self._pose

    # ---------- HTTP ----------

    def _handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.split("?", 1)[0] == "/base_status":
                    bridge.h_base_status(self)
                else:
                    bridge.h_501(self, "GET " + self.path)

            def do_POST(self):
                if self.path.split("?", 1)[0] == "/nav":
                    bridge.h_nav(self)
                else:
                    bridge.h_501(self, "POST " + self.path)

        return Handler

    def _read(self, h):
        n = int(h.headers.get("Content-Length", "0") or "0")
        return json.loads(h.rfile.read(n).decode("utf-8")) if n > 0 else {}

    def _write(self, h, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def h_501(self, h, what):
        self._write(h, 501, {"success": False,
                             "result": f"G1 nav bridge 只提供 /nav 和 /base_status;{what} 归 VLA 后端"})

    def h_base_status(self, h):
        p = self.get_pose()
        if p is None:
            self._write(h, 503, {"success": False, "result": "还没收到 /lidar_odometry/pose_fixed"})
            return
        x, y, yaw, _ = p
        self._write(h, 200, {"pos": [x, y, 0.0], "yaw_deg": math.degrees(yaw) % 360.0, "yaw_rad": yaw})

    def h_nav(self, h):
        try:
            code, obj = self.navigate(self._read(h))
            self._write(h, code, obj)
        except Exception as exc:
            self._write(h, 500, {"success": False, "result": f"G1 导航失败: {exc}"})

    def navigate(self, payload):
        if payload.get("x") is None or payload.get("y") is None:
            return 500, {"success": False, "result": "/nav 需要 x/y 坐标"}
        x = float(payload["x"])
        y = float(payload["y"])
        yaw = float(payload.get("target_yaw", payload.get("yaw", 0.0)) or 0.0)
        if abs(yaw) > 2 * math.pi:     # 自适应:>2π 当角度,转弧度
            yaw = math.radians(yaw)

        goal = PoseStamped()
        goal.header.frame_id = self.map_frame
        goal.header.stamp = self.get_clock().now().to_msg()
        goal.pose.position.x = x
        goal.pose.position.y = y
        qx, qy, qz, qw = yaw_to_quat(yaw)
        goal.pose.orientation.x = qx
        goal.pose.orientation.y = qy
        goal.pose.orientation.z = qz
        goal.pose.orientation.w = qw
        self.pub_enable.publish(Bool(data=True))
        self.pub_goal.publish(goal)
        self.get_logger().info(f"发目标 ({x:.2f}, {y:.2f}, {math.degrees(yaw):.0f}°)")

        # topic 栈无到达回调 → 订阅 odom 轮询判到达
        t0 = time.time()
        in_tol_since = None
        while time.time() - t0 < self.nav_timeout:
            p = self.get_pose()
            if p is not None:
                px, py, pyaw, _ = p
                xy = math.hypot(px - x, py - y)
                yaw_err = abs(norm_deg(math.degrees(pyaw) - math.degrees(yaw)))
                if xy <= self.xy_tol and yaw_err <= self.yaw_tol:
                    in_tol_since = in_tol_since or time.time()
                    if time.time() - in_tol_since >= self.settle:
                        return 200, {"success": True, "result": "G1 导航到达",
                                     "pos": [px, py, 0.0], "yaw_deg": math.degrees(pyaw) % 360.0,
                                     "xy_error": xy, "target": [x, y]}
                else:
                    in_tol_since = None
            time.sleep(0.1)
        return 500, {"success": False, "result": f"G1 导航超时({self.nav_timeout:.0f}s 未到达)",
                     "target": [x, y]}

    def destroy_node(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = G1NavBridge()
    try:
        rclpy.spin(node)      # 主线程收 odom;HTTP 在自己线程 publish + 读缓存 pose
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
