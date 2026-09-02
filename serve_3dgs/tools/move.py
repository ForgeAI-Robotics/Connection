"""move.py - Robot base movement control (MotrixSim backend)."""

import math
import numpy as np

_BASE_LINK_NAME = "YD_link"


def _normalize_angle(angle):
    return float(np.arctan2(np.sin(angle), np.cos(angle)))


def _normalize_angle_deg(angle):
    while angle > 180:
        angle -= 360
    while angle < -180:
        angle += 360
    return angle


def base_link_yaw_from_chassis_yaw(yaw_rad):
    return _normalize_angle(float(yaw_rad) + math.pi)


def base_link_yaw_deg_from_chassis_yaw_deg(yaw_deg):
    return _normalize_angle_deg(float(yaw_deg) + 180.0)


def _get_chassis_link(env):
    if hasattr(env, '_link_name_to_idx'):
        idx = env._link_name_to_idx.get(_BASE_LINK_NAME)
    else:
        try:
            idx = list(env.model.link_names).index(_BASE_LINK_NAME)
        except ValueError:
            idx = None
    if idx is None:
        return None, None
    return idx, env.model.get_link(idx)


def _find_actuator_idx(env, name: str) -> int:
    for i in range(env.model.num_actuators):
        act = env.model.get_actuator(i)
        if act.name == name:
            return i
    return -1


def _set_wheel_actuators(env, vx: float, wz: float):
    """Set forward/turn actuator controls for differential drive.

    BlueThink wheel actuators: ctrl[14:18] = ZQL(FR), ZHL(RR), YQL(FL), YHL(RL)
    Differential drive: right = vx+wz, left = vx-wz
    """
    # Map vx [-1,1] and wz [-1,1] to wheel speed range [-10, 10]
    wheel_scale = 10.0
    right = vx * wheel_scale + wz * wheel_scale
    left  = vx * wheel_scale - wz * wheel_scale

    for i in range(env.model.num_actuators):
        act = env.model.get_actuator(i)
        if act.name == "wheel_ZQL":
            act.set_ctrl(env.data, float(np.clip(right, -10, 10)))
        elif act.name == "wheel_ZHL":
            act.set_ctrl(env.data, float(np.clip(right, -10, 10)))
        elif act.name == "wheel_YQL":
            act.set_ctrl(env.data, float(np.clip(left, -10, 10)))
        elif act.name == "wheel_YHL":
            act.set_ctrl(env.data, float(np.clip(left, -10, 10)))


def get_base_info(env):
    chassis_idx, chassis_link = _get_chassis_link(env)
    if chassis_idx is None or chassis_link is None:
        return {
            "pos": [0.0, 0.0, 0.0],
            "yaw_deg": 0.0,
            "yaw_rad": 0.0,
            "base_link_yaw_deg": 0.0,
            "base_link_yaw_rad": math.pi,
            "qvel": [0.0, 0.0, 0.0],
        }

    poses = env.model.get_link_poses(env.data)
    pose = poses[0, chassis_idx] if poses.ndim == 3 else poses[chassis_idx]
    pos = pose[:3]
    qi, qj, qk, qw = pose[3], pose[4], pose[5], pose[6]

    yaw_rad = np.arctan2(2.0 * (qw * qk + qi * qj), 1.0 - 2.0 * (qj * qj + qk * qk))
    yaw_deg = np.rad2deg(yaw_rad)
    base_link_yaw_rad = base_link_yaw_from_chassis_yaw(yaw_rad)
    base_link_yaw_deg = base_link_yaw_deg_from_chassis_yaw_deg(yaw_deg)

    lin_vel = np.asarray(chassis_link.get_linear_velocity(env.data), dtype=float).reshape(-1)
    ang_vel = np.asarray(chassis_link.get_angular_velocity(env.data), dtype=float).reshape(-1)
    cos_y = math.cos(base_link_yaw_rad)
    sin_y = math.sin(base_link_yaw_rad)
    vx_world, vy_world = lin_vel[0], lin_vel[1]
    body_vx = vx_world * cos_y + vy_world * sin_y
    body_vy = -vx_world * sin_y + vy_world * cos_y
    wz = ang_vel[2]

    return {
        "pos": pos.tolist(),
        "yaw_deg": round(float(yaw_deg), 2),
        "yaw_rad": round(float(yaw_rad), 4),
        "base_link_yaw_deg": round(float(base_link_yaw_deg), 2),
        "base_link_yaw_rad": round(float(base_link_yaw_rad), 4),
        "qvel": [float(body_vx), float(body_vy), float(wz)],
    }


def set_base_velocity(env, vx=0.0, vy=0.0, wz=0.0):
    _set_wheel_actuators(env, vx, wz)


def move(env, Vx=0.0, Vy=0.0, Vw=0.0, steps=1):
    set_base_velocity(env, Vx, Vy, Vw)
    env.step(steps)
    return get_base_info(env)


def stop_base(env):
    set_base_velocity(env, 0.0, 0.0, 0.0)
    return get_base_info(env)


def nav(env, x, y, target_yaw=None, Kp=2.5, Kd=0.3, pos_threshold=0.1, yaw_threshold=3.0, max_steps=800):
    prev_heading_err = 0.0
    prev_pos_err = 0.0

    for _ in range(max_steps):
        info = get_base_info(env)
        x_now, y_now = info["pos"][0], info["pos"][1]
        yaw_now = info.get("base_link_yaw_rad", base_link_yaw_from_chassis_yaw(info["yaw_rad"]))

        err_x = x - x_now
        err_y = y - y_now
        pos_err = float(np.hypot(err_x, err_y))

        if pos_err < pos_threshold:
            if target_yaw is None:
                break
            current_yaw_deg = info.get(
                "base_link_yaw_deg",
                base_link_yaw_deg_from_chassis_yaw_deg(info["yaw_deg"]),
            )
            yaw_err_deg = _normalize_angle_deg(target_yaw - current_yaw_deg)
            if abs(yaw_err_deg) < yaw_threshold:
                break
            Vw = np.clip(Kp * (yaw_err_deg / 90.0), -1.0, 1.0)
            move(env, Vx=0.0, Vw=Vw)
            continue

        angle_to_target = np.arctan2(err_y, err_x)
        heading_err = _normalize_angle(angle_to_target - yaw_now)

        d_heading = heading_err - prev_heading_err
        prev_heading_err = heading_err
        d_pos = pos_err - prev_pos_err
        prev_pos_err = pos_err

        turn = np.clip(Kp * heading_err + Kd * d_heading, -1.0, 1.0)

        forward = np.clip(Kp * pos_err, -1.0, 1.0)
        forward *= max(0.0, np.cos(heading_err))
        forward = np.clip(forward + Kd * d_pos * 0.1, -1.0, 1.0)

        move(env, Vx=forward, Vw=turn)

    if target_yaw is not None:
        for _ in range(200):
            info = get_base_info(env)
            current_yaw_deg = info.get(
                "base_link_yaw_deg",
                base_link_yaw_deg_from_chassis_yaw_deg(info["yaw_deg"]),
            )
            yaw_err_deg = _normalize_angle_deg(target_yaw - current_yaw_deg)
            if abs(yaw_err_deg) < yaw_threshold:
                break
            Vw = np.clip(Kp * (yaw_err_deg / 90.0), -1.0, 1.0)
            move(env, Vx=0.0, Vw=Vw)

    return get_base_info(env)
