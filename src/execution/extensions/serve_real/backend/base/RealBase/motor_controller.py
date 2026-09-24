#!/usr/bin/env python3
"""
双轮差速底盘运动控制器
使用Feetech STS3215舵机驱动
当前只实现前后移动
"""

import time
import numpy as np
from typing import Dict, Optional

from motors.feetech.feetech import FeetechMotorsBus, OperatingMode
from motors import Motor, MotorNormMode


class OmniWheelController:
    """双轮差速底盘控制器

    保留原接口名称以兼容 base_server, 当前只使用 vx 实现前后移动。
    """

    # 电机ID配置 - 修改这里即可更改所有电机ID
    WHEEL_IDS = [9, 10]
    WHEEL_DIRECTIONS = [-1, 1]  # 如果实测前进时某个轮子反转, 将对应值改为 -1

    # 轮子配置
    WHEEL_RADIUS = 0.05  # 轮子半径 (米)
    BASE_RADIUS = 0.125  # 从机器人中心到轮子的距离 (米)
    MAX_RAW_VELOCITY = 3000  # 最大原始速度值 (ticks)
    STEPS_PER_DEG = 4096.0 / 360.0  # 每度的步数
    VELOCITY_SCALE = 0.3  # 全局速度缩放因子 (0-1), 用于降低实际输出速度

    def __init__(
        self,
        port: str = "/dev/ttyACM0",
        wheel_radius: float = WHEEL_RADIUS,
        robot_radius: float = BASE_RADIUS,
        max_raw: int = MAX_RAW_VELOCITY,
        velocity_scale: float = VELOCITY_SCALE,
    ):
        """初始化底盘控制器

        Args:
            port: 串口端口
            wheel_radius: 轮子半径(米)
            robot_radius: 从机器人中心到轮子的距离(米)
            max_raw: 最大原始速度值(ticks),用于速度限制和缩放
            velocity_scale: 全局速度缩放因子(0-1), 用于降低实际输出速度
        """
        self.port = port
        self.wheel_radius = wheel_radius
        self.robot_radius = robot_radius
        self.max_raw = max_raw
        self.velocity_scale = velocity_scale
        self.base_bus = None

        # 当前轮子控制值 (deg/s)
        self.wheel_velocities = np.array([0.0, 0.0])

    @staticmethod
    def _degps_to_raw(degps: float) -> int:
        """将度/秒转换为原始速度值

        Args:
            degps: 角速度 (度/秒)

        Returns:
            原始速度值 (ticks), 范围 [-32768, 32767]
        """
        speed_in_steps = degps * OmniWheelController.STEPS_PER_DEG
        speed_int = int(round(speed_in_steps))
        # 限制在16位有符号整数范围内
        if speed_int > 0x7FFF:
            speed_int = 0x7FFF  # 32767
        elif speed_int < -0x8000:
            speed_int = -0x8000  # -32768
        return speed_int

    @staticmethod
    def _raw_to_degps(raw_speed: int) -> float:
        """将原始速度值转换为度/秒

        Args:
            raw_speed: 原始速度值 (ticks)

        Returns:
            角速度 (度/秒)
        """
        degps = raw_speed / OmniWheelController.STEPS_PER_DEG
        return degps

    def connect(self) -> bool:
        """连接底盘电机

        Returns:
            连接成功返回True,否则返回False
        """
        try:
            # 创建底盘电机总线
            self.base_bus = FeetechMotorsBus(
                port=self.port,
                motors={
                    "wheel_1": Motor(OmniWheelController.WHEEL_IDS[0], "sts3215", MotorNormMode.RANGE_M100_100),
                    "wheel_2": Motor(OmniWheelController.WHEEL_IDS[1], "sts3215", MotorNormMode.RANGE_M100_100),
                },
            )

            # 连接总线
            self.base_bus.connect()
            print(f"✓ 已连接到底盘电机总线: {self.port}")

            # 配置电机为速度控制模式
            with self.base_bus.torque_disabled():
                self.base_bus.configure_motors()
                for motor in self.base_bus.motors:
                    self.base_bus.write("Operating_Mode", motor, OperatingMode.VELOCITY.value)

            # 使能扭矩
            self.base_bus.enable_torque()

            return True

        except Exception as e:
            print(f"✗ 底盘连接失败: {e}")
            return False

    def disconnect(self):
        """断开底盘连接"""
        if self.base_bus is not None:
            try:
                # 停止所有电机
                self.stop()
                time.sleep(0.5)
                # 断开连接
                self.base_bus.disconnect(disable_torque=True)
                print(f"✓ 已断开底盘电机")
            except Exception as e:
                print(f"✗ 断开连接时出错: {e}")
            finally:
                self.base_bus = None

    def set_velocity(self, linear_speed: float, vx: float, vy: float, omega: float = 0.0) -> bool:
        """设置机器人速度 (归一化方向向量)

        Args:
            linear_speed: 速度大小 (m/s)
            vx: 机器人坐标系下x方向速度分量 (归一化, -1到1)
            vy: 机器人坐标系下y方向速度分量 (归一化, -1到1)
            omega: 旋转角速度 (rad/s), 正值=逆时针旋转, 负值=顺时针旋转

        Returns:
            成功返回True,失败返回False

        说明:
            vx, vy 是方向向量,会被归一化并乘以linear_speed
            omega 直接使用,不进行归一化

            例如:
                vx=1, vy=0 表示向前
                vx=0, vy=1 表示向左
                omega=1 表示逆时针旋转 1 rad/s
        """
        if self.base_bus is None:
            print("✗ 底盘未连接")
            return False

        try:
            # 归一化方向向量并乘以速度大小
            v_norm = np.sqrt(vx**2 + vy**2)
            if v_norm > 0:
                vx = vx / v_norm * linear_speed
                vy = vy / v_norm * linear_speed
            else:
                vx = 0
                vy = 0

            # 双轮差速当前只实现前后移动: vx > 0 前进, vx < 0 后退。
            # vy 和 omega 暂时忽略。
            wheel_angular_velocity = vx / self.wheel_radius
            wheel_angular_velocities = np.array([
                wheel_angular_velocity * self.WHEEL_DIRECTIONS[0],
                wheel_angular_velocity * self.WHEEL_DIRECTIONS[1],
            ])

            # 保存当前轮子速度
            self.wheel_velocities = wheel_angular_velocities

            # 应用到电机
            self._apply_wheel_velocities(wheel_angular_velocities)

            return True

        except Exception as e:
            print(f"✗ 设置速度失败: {e}")
            return False

    def set_velocity_raw(self, vx: float, vy: float, omega: float = 0.0) -> bool:
        """直接设置速度分量 (不归一化)

        Args:
            vx: x方向速度 (m/s)
            vy: y方向速度 (m/s)
            omega: 旋转角速度 (rad/s)

        Returns:
            成功返回True,失败返回False
        """
        if self.base_bus is None:
            print("✗ 底盘未连接")
            return False

        try:
            # 双轮差速当前只实现前后移动: vx > 0 前进, vx < 0 后退。
            # vy 和 omega 暂时忽略。
            wheel_angular_velocity = vx / self.wheel_radius
            wheel_angular_velocities = np.array([
                wheel_angular_velocity * self.WHEEL_DIRECTIONS[0],
                wheel_angular_velocity * self.WHEEL_DIRECTIONS[1],
            ])

            # 保存当前轮子速度
            self.wheel_velocities = wheel_angular_velocities

            # 应用到电机
            self._apply_wheel_velocities(wheel_angular_velocities)

            return True

        except Exception as e:
            print(f"✗ 设置速度失败: {e}")
            return False

    def _apply_wheel_velocities(self, wheel_angular_velocities: np.ndarray):
        """将轮子角速度应用到电机

        Args:
            wheel_angular_velocities: 2个轮子的角速度 (rad/s)

        说明:
            参考lerobot实现:
            1. 应用全局速度缩放因子
            2. 转换 rad/s -> deg/s
            3. 转换 deg/s -> raw ticks
            4. 如果任一轮子速度超过max_raw,按比例缩放所有轮子速度
        """
        # 1. 应用全局速度缩放因子
        wheel_angular_velocities = wheel_angular_velocities * self.velocity_scale

        # 2. 转换 rad/s -> deg/s
        wheel_degps = wheel_angular_velocities * (180.0 / np.pi)

        # 3. 转换为原始值 (用于检查是否需要缩放)
        raw_floats = [abs(degps) * self.STEPS_PER_DEG for degps in wheel_degps]
        max_raw_computed = max(raw_floats)

        # 4. 如果超过最大值,按比例缩放
        if max_raw_computed > self.max_raw:
            scale = self.max_raw / max_raw_computed
            wheel_degps = wheel_degps * scale

        # 5. 转换为原始整数并写入电机
        for i, degps in enumerate(wheel_degps):
            velocity_value = self._degps_to_raw(degps)
            motor_name = f"wheel_{i + 1}"
            self.base_bus.write("Goal_Velocity", motor_name, velocity_value, normalize=False)

    def wheel_velocities_to_body_velocity(
        self,
        wheel_velocities: np.ndarray
    ) -> dict:
        """将轮子速度转换为机器人速度

        Args:
            wheel_velocities: 2个轮子的角速度 (rad/s)

        Returns:
            机器人速度字典: {"vx": x方向速度(m/s), "vy": y方向速度(m/s), "omega": 角速度(rad/s)}
        """
        corrected = np.array([
            wheel_velocities[0] * self.WHEEL_DIRECTIONS[0],
            wheel_velocities[1] * self.WHEEL_DIRECTIONS[1],
        ])
        vx = float(np.mean(corrected) * self.wheel_radius)

        return {
            "vx": vx,
            "vy": 0.0,
            "omega": 0.0
        }

    def get_wheel_velocities(self) -> np.ndarray:
        """获取当前轮子控制速度

        Returns:
            numpy数组,包含2个轮子的角速度 (rad/s)
        """
        return self.wheel_velocities.copy()

    def stop(self) -> bool:
        """停止所有轮子

        Returns:
            成功返回True,失败返回False
        """
        if self.base_bus is None:
            print("✗ 底盘未连接")
            return False

        try:
            for wheel_id in self.WHEEL_IDS:
                wheel_index = self.WHEEL_IDS.index(wheel_id)
                motor_name = f"wheel_{wheel_index + 1}"
                self.base_bus.write("Goal_Velocity", motor_name, 0, normalize=False)

            self.wheel_velocities = np.array([0.0, 0.0])
            return True

        except Exception as e:
            print(f"✗ 停止失败: {e}")
            return False

    def control_wheel(self, wheel_id: int, direction: int, speed: int) -> Dict:
        """控制单个轮子转动 (兼容旧接口)

        Args:
            wheel_id: 电机ID (使用 WHEEL_IDS)
            direction: 转动方向 (1=正转, -1=反转)
            speed: 转动速度 (0-100, 0表示停止)

        Returns:
            执行结果字典
        """
        if self.base_bus is None:
            return {
                "success": False,
                "message": "底盘未连接",
                "wheel_id": wheel_id
            }

        if wheel_id not in self.WHEEL_IDS:
            return {
                "success": False,
                "message": f"无效的轮子ID: {wheel_id}, 必须是{self.WHEEL_IDS[0]} 或 {self.WHEEL_IDS[1]}",
                "wheel_id": wheel_id
            }

        if direction not in [1, -1]:
            return {
                "success": False,
                "message": f"无效的方向: {direction}, 必须是1或-1",
                "wheel_id": wheel_id
            }

        if not 0 <= speed <= 100:
            return {
                "success": False,
                "message": f"无效的速度: {speed}, 必须在0-100之间",
                "wheel_id": wheel_id
            }

        try:
            # 计算实际速度值 (考虑方向)
            actual_speed = direction * speed

            # 根据电机ID获取电机名称和索引
            wheel_index = self.WHEEL_IDS.index(wheel_id)
            motor_name = f"wheel_{wheel_index + 1}"

            # 设置速度
            velocity_value = int(actual_speed * 10.23)  # 100 * 10.23 = 1023

            self.base_bus.write("Goal_Velocity", motor_name, velocity_value, normalize=False)

            # 更新当前速度
            self.wheel_velocities[wheel_index] = actual_speed / 10.23

            direction_str = "正转" if direction > 0 else "反转"

            return {
                "success": True,
                "message": f"轮子{wheel_id} {direction_str}, 速度: {speed}%",
                "wheel_id": wheel_id,
                "direction": direction,
                "direction_str": direction_str,
                "speed": speed,
                "actual_speed": actual_speed
            }

        except Exception as e:
            return {
                "success": False,
                "message": f"控制失败: {str(e)}",
                "wheel_id": wheel_id
            }

    def stop_wheel(self, wheel_id: int) -> Dict:
        """停止指定轮子

        Args:
            wheel_id: 电机ID

        Returns:
            执行结果字典
        """
        return self.control_wheel(wheel_id, direction=1, speed=0)

    def get_wheel_speed(self, wheel_id: int) -> Dict:
        """获取指定轮子的当前速度

        Args:
            wheel_id: 电机ID

        Returns:
            速度信息字典
        """
        if wheel_id not in self.WHEEL_IDS:
            return {
                "success": False,
                "message": f"无效的轮子ID: {wheel_id}",
                "wheel_id": wheel_id
            }

        try:
            wheel_index = self.WHEEL_IDS.index(wheel_id)
            speed_rad_s = self.wheel_velocities[wheel_index]
            speed_percent = abs(speed_rad_s) / 10.0 * 100

            return {
                "success": True,
                "wheel_id": wheel_id,
                "speed": min(100, speed_percent),
                "direction": 1 if speed_rad_s >= 0 else -1,
                "direction_str": "正转" if speed_rad_s >= 0 else "反转"
            }

        except Exception as e:
            return {
                "success": False,
                "message": f"读取速度失败: {str(e)}",
                "wheel_id": wheel_id
            }

    def get_all_speeds(self) -> Dict:
        """获取所有轮子的当前速度

        Returns:
            所轮子速度信息
        """
        if self.base_bus is None:
            return {
                "success": False,
                "message": "底盘未连接"
            }

        try:
            speeds = {}
            for i, wheel_id in enumerate(self.WHEEL_IDS):
                speed_rad_s = self.wheel_velocities[i]
                speed_percent = abs(speed_rad_s) / 10.0 * 100

                speeds[f"wheel_{i + 1}"] = {
                    "wheel_id": wheel_id,
                    "speed": min(100, speed_percent),
                    "direction": 1 if speed_rad_s >= 0 else -1,
                    "direction_str": "正转" if speed_rad_s >= 0 else "反转"
                }

            return {
                "success": True,
                "speeds": speeds
            }

        except Exception as e:
            return {
                "success": False,
                "message": f"读取速度失败: {str(e)}"
            }


# 全局控制器实例
_motor_controller: Optional[OmniWheelController] = None


def get_motor_controller() -> OmniWheelController:
    """获取全局底盘控制器实例

    Returns:
        底盘控制器实例
    """
    global _motor_controller
    if _motor_controller is None:
        _motor_controller = OmniWheelController()
    return _motor_controller
