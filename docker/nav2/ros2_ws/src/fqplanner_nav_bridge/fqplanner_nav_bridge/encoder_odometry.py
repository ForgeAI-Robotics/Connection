#!/usr/bin/env python3
"""Encoder odometry node for real FQPlanner differential-drive robot.

Reads wheel positions from Feetech STS3215 servos via serial bus,
computes differential-drive odometry, and publishes /odom + TF.
"""

from __future__ import annotations

import math
import time

import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from tf2_ros import TransformBroadcaster

# Feetech motor bus (from the existing motor_controller.py codebase)
import sys
import os

# Add serve_real path so we can import the motor bus
_SERVE_REAL = os.environ.get("FQPLANNER_SERVE_REAL", "")
if _SERVE_REAL and os.path.isdir(_SERVE_REAL):
    _backend_base = os.path.join(_SERVE_REAL, "backend", "base", "RealBase")
    if _backend_base not in sys.path:
        sys.path.insert(0, _backend_base)


class EncoderOdometry(Node):
    WHEEL_IDS = [9, 10]
    WHEEL_DIRECTIONS = [-1, 1]  # same as motor_controller.py
    STEPS_PER_REV = 4096.0

    def __init__(self):
        super().__init__("encoder_odometry")
        self.declare_parameter("port", "/dev/ttyACM0")
        self.declare_parameter("wheel_radius", 0.05)       # m
        self.declare_parameter("base_radius", 0.125)        # m (half wheelbase)
        self.declare_parameter("odom_frame", "odom")
        self.declare_parameter("base_frame", "base_link")
        self.declare_parameter("publish_rate", 20.0)        # Hz
        self.declare_parameter("publish_tf", True)

        self.wheel_radius = float(self.get_parameter("wheel_radius").value)
        self.base_radius = float(self.get_parameter("base_radius").value)
        self.odom_frame = str(self.get_parameter("odom_frame").value)
        self.base_frame = str(self.get_parameter("base_frame").value)

        # State
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0
        self.prev_left_deg: float | None = None
        self.prev_right_deg: float | None = None
        self.prev_time: float | None = None

        # ROS publishers
        self.odom_pub = self.create_publisher(Odometry, "odom", 10)
        self.tf_broadcaster: TransformBroadcaster | None = None
        if bool(self.get_parameter("publish_tf").value):
            self.tf_broadcaster = TransformBroadcaster(self)

        # Connect to motor bus
        self.bus = None
        self._connect_bus()

        rate = max(1.0, float(self.get_parameter("publish_rate").value))
        self.create_timer(1.0 / rate, self.update_odom)
        self.get_logger().info(
            f"Encoder odometry started: wheel_r={self.wheel_radius}m, "
            f"base_r={self.base_radius}m, rate={rate}Hz"
        )

    def _connect_bus(self):
        port = str(self.get_parameter("port").value)
        try:
            from motors import Motor, MotorNormMode
            from motors.feetech.feetech import FeetechMotorsBus

            self.bus = FeetechMotorsBus(
                port=port,
                motors={
                    "wheel_1": Motor(self.WHEEL_IDS[0], "sts3215", MotorNormMode.RANGE_M100_100),
                    "wheel_2": Motor(self.WHEEL_IDS[1], "sts3215", MotorNormMode.RANGE_M100_100),
                },
            )
            self.bus.connect()
            self.get_logger().info(f"Connected to Feetech bus on {port}")
        except Exception as exc:
            self.get_logger().error(f"Failed to connect motor bus: {exc}")
            self.bus = None

    def _read_positions(self):
        """Read both wheel encoder positions in degrees. Returns (left_deg, right_deg) or None."""
        if self.bus is None:
            return None
        try:
            positions = self.bus.sync_read("Present_Position", normalize=False)
            left = float(positions.get("wheel_1", 0))
            right = float(positions.get("wheel_2", 0))
            # Raw encoder: 0..4095 for one revolution
            # Convert to degrees: value / 4096 * 360
            left_deg = left / self.STEPS_PER_REV * 360.0
            right_deg = right / self.STEPS_PER_REV * 360.0
            return left_deg, right_deg
        except Exception as exc:
            self.get_logger().debug(f"Encoder read failed: {exc}")
            return None

    @staticmethod
    def _normalize_angle_delta(delta: float) -> float:
        """Wrap angle difference to [-180, 180] degrees."""
        while delta > 180.0:
            delta -= 360.0
        while delta < -180.0:
            delta += 360.0
        return delta

    def update_odom(self):
        now = time.monotonic()
        positions = self._read_positions()
        if positions is None:
            return

        left_deg, right_deg = positions

        if self.prev_left_deg is None:
            # First reading — just store and return
            self.prev_left_deg = left_deg
            self.prev_right_deg = right_deg
            self.prev_time = now
            return

        dt = now - self.prev_time
        if dt < 1e-6:
            return

        # Delta in degrees, then convert to radians, then to wheel travel distance
        d_left_deg = self._normalize_angle_delta(left_deg - self.prev_left_deg)
        d_right_deg = self._normalize_angle_delta(right_deg - self.prev_right_deg)

        # Apply wheel direction correction
        d_left_rad = math.radians(d_left_deg) * self.WHEEL_DIRECTIONS[0]
        d_right_rad = math.radians(d_right_deg) * self.WHEEL_DIRECTIONS[1]

        # Wheel travel distance (meters)
        ds_left = d_left_rad * self.wheel_radius
        ds_right = d_right_rad * self.wheel_radius

        # Differential drive kinematics
        ds = (ds_right + ds_left) / 2.0       # linear displacement
        dtheta = (ds_right - ds_left) / (2.0 * self.base_radius)  # angular displacement

        # Update pose
        self.x += ds * math.cos(self.yaw + dtheta / 2.0)
        self.y += ds * math.sin(self.yaw + dtheta / 2.0)
        self.yaw += dtheta

        # Velocities for twist
        vx = ds / dt
        vw = dtheta / dt

        self.prev_left_deg = left_deg
        self.prev_right_deg = right_deg
        self.prev_time = now

        # Publish
        stamp = self.get_clock().now().to_msg()
        self._publish_odom(stamp, vx, vw)
        if self.tf_broadcaster is not None:
            self._publish_tf(stamp)

    def _publish_odom(self, stamp, vx, vw):
        qx, qy, qz, qw = self._yaw_to_quat(self.yaw)

        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = self.odom_frame
        odom.child_frame_id = self.base_frame
        odom.pose.pose.position.x = self.x
        odom.pose.pose.position.y = self.y
        odom.pose.pose.orientation.x = qx
        odom.pose.pose.orientation.y = qy
        odom.pose.pose.orientation.z = qz
        odom.pose.pose.orientation.w = qw
        # Covariance: moderate confidence
        odom.pose.covariance[0] = 0.01   # x
        odom.pose.covariance[7] = 0.01   # y
        odom.pose.covariance[35] = 0.02  # yaw
        odom.twist.twist.linear.x = vx
        odom.twist.twist.angular.z = vw
        self.odom_pub.publish(odom)

    def _publish_tf(self, stamp):
        qx, qy, qz, qw = self._yaw_to_quat(self.yaw)
        tf = TransformStamped()
        tf.header.stamp = stamp
        tf.header.frame_id = self.odom_frame
        tf.child_frame_id = self.base_frame
        tf.transform.translation.x = self.x
        tf.transform.translation.y = self.y
        tf.transform.translation.z = 0.0
        tf.transform.rotation.x = qx
        tf.transform.rotation.y = qy
        tf.transform.rotation.z = qz
        tf.transform.rotation.w = qw
        self.tf_broadcaster.sendTransform(tf)

    @staticmethod
    def _yaw_to_quat(yaw: float):
        half = yaw * 0.5
        return 0.0, 0.0, math.sin(half), math.cos(half)

    def destroy_node(self):
        if self.bus is not None:
            try:
                self.bus.disconnect(disable_torque=False)
            except Exception:
                pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = EncoderOdometry()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
