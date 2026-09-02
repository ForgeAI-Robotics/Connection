#!/usr/bin/env python3
"""HTTP /nav facade that sends goals to Nav2 and proxies other backend calls."""

from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.node import Node


def yaw_to_quat(yaw: float):
    half = yaw * 0.5
    return 0.0, 0.0, math.sin(half), math.cos(half)


def normalize_angle_deg(angle: float) -> float:
    return (float(angle) + 180.0) % 360.0 - 180.0


class Nav2GoalBridge(Node):
    def __init__(self):
        super().__init__("fqplanner_nav2_goal_bridge")
        self.declare_parameter("backend_url", "http://127.0.0.1:5001")
        self.declare_parameter("http_host", "127.0.0.1")
        self.declare_parameter("http_port", 5102)
        self.declare_parameter("map_frame", "map")
        self.declare_parameter("nav_timeout", 120.0)
        self.declare_parameter("proxy_timeout", 5.0)
        self.declare_parameter("success_xy_tolerance", 0.15)
        self.declare_parameter("success_yaw_tolerance_deg", 10.0)
        # 真机部署设 false:成功校验走 backend /base_status 是仿真专用,真机后面没有仿真后端
        self.declare_parameter("verify_final_pose", True)

        self.backend_url = str(self.get_parameter("backend_url").value).rstrip("/")
        self.http_host = str(self.get_parameter("http_host").value)
        self.http_port = int(self.get_parameter("http_port").value)
        self.map_frame = str(self.get_parameter("map_frame").value)
        self.nav_timeout = float(self.get_parameter("nav_timeout").value)
        self.proxy_timeout = float(self.get_parameter("proxy_timeout").value)
        self.success_xy_tolerance = float(self.get_parameter("success_xy_tolerance").value)
        self.success_yaw_tolerance_deg = float(self.get_parameter("success_yaw_tolerance_deg").value)
        self.verify_final_pose = bool(self.get_parameter("verify_final_pose").value)
        self.action_client = ActionClient(self, NavigateToPose, "navigate_to_pose")
        self.server = None
        self.server_thread = None
        self.start_http_server()

    def start_http_server(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                bridge.get_logger().debug(fmt % args)

            def do_GET(self):
                bridge.proxy(self, "GET")

            def do_POST(self):
                if self.path.split("?", 1)[0] == "/nav":
                    bridge.handle_nav(self)
                else:
                    bridge.proxy(self, "POST")

        self.server = ThreadingHTTPServer((self.http_host, self.http_port), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.get_logger().info(
            f"Nav2 goal bridge listening on http://{self.http_host}:{self.http_port}, "
            f"proxy={self.backend_url}"
        )

    def read_json_body(self, handler):
        length = int(handler.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = handler.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def write_json(self, handler, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def request_json(self, method: str, endpoint: str, payload=None, timeout=None):
        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = self.backend_url + endpoint
        data = None
        headers = {}
        if method == "POST":
            data = json.dumps(payload or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout or self.proxy_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def proxy(self, handler, method):
        body = None
        headers = {}
        if method == "POST":
            body_obj = self.read_json_body(handler)
            body = json.dumps(body_obj).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = self.backend_url + handler.path
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.proxy_timeout) as resp:
                data = resp.read()
                handler.send_response(resp.status)
                handler.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)
        except urllib.error.HTTPError as exc:
            self.write_json(handler, exc.code, {"success": False, "result": str(exc)})
        except Exception as exc:
            self.write_json(handler, 503, {"success": False, "result": f"后端代理失败: {exc}"})

    def handle_nav(self, handler):
        try:
            payload = self.read_json_body(handler)
            result = self.navigate(payload)
            status = 200 if result.get("success") else 500
            self.write_json(handler, status, result)
        except Exception as exc:
            self.write_json(handler, 500, {"success": False, "result": f"Nav2 导航失败: {exc}"})

    def navigate(self, payload):
        if payload.get("x") is None or payload.get("y") is None:
            return {"success": False, "result": "Nav2 /nav 需要 x/y 坐标"}
        x = float(payload["x"])
        y = float(payload["y"])
        yaw = payload.get("target_yaw", payload.get("yaw", 0.0))
        yaw = float(yaw or 0.0)
        if abs(yaw) > 2 * math.pi:
            yaw = math.radians(yaw)

        if not self.action_client.wait_for_server(timeout_sec=5.0):
            return {"success": False, "result": "Nav2 navigate_to_pose action 不可用"}

        goal_msg = NavigateToPose.Goal()
        goal_msg.pose = PoseStamped()
        goal_msg.pose.header.frame_id = self.map_frame
        goal_msg.pose.header.stamp = self.get_clock().now().to_msg()
        goal_msg.pose.pose.position.x = x
        goal_msg.pose.pose.position.y = y
        qx, qy, qz, qw = yaw_to_quat(yaw)
        goal_msg.pose.pose.orientation.x = qx
        goal_msg.pose.pose.orientation.y = qy
        goal_msg.pose.pose.orientation.z = qz
        goal_msg.pose.pose.orientation.w = qw

        send_future = self.action_client.send_goal_async(goal_msg)
        if not self.wait_future(send_future, 5.0):
            return {"success": False, "result": "Nav2 goal 发送超时"}
        goal_handle = send_future.result()
        if goal_handle is None or not goal_handle.accepted:
            return {"success": False, "result": "Nav2 goal 被拒绝"}

        result_future = goal_handle.get_result_async()
        if not self.wait_future(result_future, self.nav_timeout):
            return {"success": False, "result": "Nav2 导航超时"}
        result = result_future.result()
        if result is None:
            return {"success": False, "result": "Nav2 导航超时"}
        status = int(result.status)
        if status == 4:
            if not self.verify_final_pose:
                return {"success": True, "result": "Nav2 导航完成(SUCCEEDED)",
                        "target": [x, y], "target_yaw_deg": math.degrees(yaw)}
            return self.final_result_from_base_status(x, y, math.degrees(yaw))
        return {"success": False, "result": f"Nav2 导航失败，status={status}"}

    def final_result_from_base_status(self, target_x: float, target_y: float, target_yaw_deg: float):
        try:
            status = self.request_json("GET", "/base_status")
        except Exception as exc:
            return {"success": False, "result": f"Nav2 成功但读取真实底盘坐标失败: {exc}"}

        pos = status.get("pos") or [target_x, target_y, 0.0]
        yaw_deg = float(status.get("base_link_yaw_deg", status.get("yaw", target_yaw_deg)))
        dx = float(pos[0]) - target_x
        dy = float(pos[1]) - target_y
        xy_error = math.hypot(dx, dy)
        yaw_error_deg = abs(normalize_angle_deg(yaw_deg - target_yaw_deg))

        if xy_error > self.success_xy_tolerance or yaw_error_deg > self.success_yaw_tolerance_deg:
            return {
                "success": False,
                "result": (
                    "Nav2 导航结束但真实误差超限: "
                    f"位置误差 {xy_error:.3f}m / {self.success_xy_tolerance:.2f}m, "
                    f"角度误差 {yaw_error_deg:.1f}° / {self.success_yaw_tolerance_deg:.1f}°"
                ),
                "pos": pos,
                "yaw": yaw_deg,
                "target": [target_x, target_y, 0.0],
                "target_yaw": target_yaw_deg,
                "xy_error": xy_error,
                "yaw_error_deg": yaw_error_deg,
            }

        return {
            "success": True,
            "result": "Nav2 导航成功",
            "pos": pos,
            "yaw": yaw_deg,
            "target": [target_x, target_y, 0.0],
            "target_yaw": target_yaw_deg,
            "xy_error": xy_error,
            "yaw_error_deg": yaw_error_deg,
        }

    @staticmethod
    def wait_future(future, timeout_sec):
        event = threading.Event()
        future.add_done_callback(lambda _: event.set())
        return event.wait(timeout=timeout_sec)

    def destroy_node(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = Nav2GoalBridge()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
