"""
move.py - 机器人底座移动控制

动作空间（12维）：
  action[7]  → forward（body X 轴，前进/后退）
  action[8]  → side（body Y 轴，左/右平移，正=左）
  action[9]  → yaw（原地旋转，正=逆时针）
  action[11] → 模式（正=底座模式）

body 坐标系随 yaw 旋转：
  yaw=0°:  body_X = 世界+X,  body_Y = 世界+Y
  yaw=90°: body_X = 世界+Y,  body_Y = 世界-X
"""

import sys

import numpy as np

# 由 server 设置:每个底盘步进后调用一次,用于命令执行中实时刷新四宫格(否则同步命令占住主循环,画面冻结)。
STEP_HOOK = None


def _normalize_angle_deg(angle):
    """角度归一化到 [-180, 180]"""
    while angle > 180:
        angle -= 360
    while angle < -180:
        angle += 360
    return angle


def _world_to_body(Vx_world, Vy_world, yaw_rad):
    """
    世界坐标系速度 → body 坐标系速度

    yaw=0° 时: body_X = 世界+X, body_Y = 世界+Y
    """
    c = np.cos(yaw_rad)
    s = np.sin(yaw_rad)
    Vx_body = Vx_world * c + Vy_world * s
    Vy_body = -Vx_world * s + Vy_world * c
    return Vx_body, Vy_body


def get_base_info(env):
    """
    获取底座全部运动相关数据

    Returns:
        dict:
            pos:       世界坐标 [x, y, z]
            yaw_deg:   朝向（度，0-360）
            yaw_rad:   朝向（弧度）
            qpos:      关节值 [forward, side, yaw]
            qvel:      关节速度 [forward, side, yaw]
            ctrl:      控制信号 [forward, side, yaw]
    """
    base_id = env.sim.model.body_name2id("mobilebase0_base")

    # 世界坐标
    pos = env.sim.data.body_xpos[base_id].copy()
    quat = env.sim.data.body_xquat[base_id]  # [w, x, y, z]
    w, x, y, z = quat
    yaw_rad = np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    yaw_deg = np.rad2deg(yaw_rad)

    # 关节值
    qpos = env.sim.data.qpos[0:3].copy()
    qvel = env.sim.data.qvel[0:3].copy()
    ctrl = env.sim.data.ctrl[7:10].copy()

    return {
        "pos": pos.tolist(),
        "yaw_deg": round(float(yaw_deg), 2),
        "yaw_rad": round(float(yaw_rad), 4),
        "qpos": qpos.tolist(),
        "qvel": qvel.tolist(),
        "ctrl": ctrl.tolist(),
    }


def move(env, Vx=0.0, Vy=0.0, Vw=0.0):
    """
    底座速度控制（一步）

    Args:
        env: 环境对象
        Vx:  前进速度 [-1, 1]，body X 轴方向（前进为正）
        Vy:  侧移速度 [-1, 1]，body Y 轴方向（左移为正）
        Vw:  旋转速度 [-1, 1]，逆时针为正

    Returns:
        dict: 执行后的底座状态（同 get_base_info）
    """
    Vx = np.clip(Vx, -1.0, 1.0)
    Vy = np.clip(Vy, -1.0, 1.0)
    Vw = np.clip(Vw, -1.0, 1.0)

    action = np.zeros(env.action_dim)
    action[7] = Vx
    action[8] = Vy
    action[9] = Vw
    action[11] = 1.0  # 底座模式

    env.step(action)
    if STEP_HOOK is not None:
        try:
            STEP_HOOK()
        except Exception:
            pass

    return get_base_info(env)


def nav(env, x, y, target_yaw=None, Kp=2.5, Kd=0.3, pos_threshold=0.15, yaw_threshold=5.0, max_steps=500):
    """
    导航到世界坐标系目标点（全向 PD 控制，x/y/yaw 同时消除误差）

    Args:
        env:            环境对象
        x:              目标世界坐标 x
        y:              目标世界坐标 y
        target_yaw:     目标偏航角（度），None 表示不调整朝向
        Kp:             比例增益
        Kd:             微分增益
        pos_threshold:  位置误差阈值（米），默认 0.1
        yaw_threshold:  偏航角误差阈值（度），默认 3.0
        max_steps:      最大步数

    Returns:
        dict: 最终底座状态，含 "reached" 字段表示是否真正到达目标
    """
    prev_err_x = 0.0
    prev_err_y = 0.0
    prev_err_yaw = 0.0
    reached = False

    for _ in range(max_steps):
        info = get_base_info(env)
        x_now, y_now = info["pos"][0], info["pos"][1]

        err_x = x - x_now
        err_y = y - y_now
        pos_err = float(np.hypot(err_x, err_y))

        if pos_err < pos_threshold:
            if target_yaw is None:
                reached = True
                break
            yaw_err = _normalize_angle_deg(target_yaw - info["yaw_deg"])
            if abs(yaw_err) < yaw_threshold:
                reached = True
                break

        d_err_x = err_x - prev_err_x
        d_err_y = err_y - prev_err_y
        prev_err_x = err_x
        prev_err_y = err_y

        Vx_world = np.clip(Kp * err_x + Kd * d_err_x, -1.0, 1.0)
        Vy_world = np.clip(Kp * err_y + Kd * d_err_y, -1.0, 1.0)
        Vx_body, Vy_body = _world_to_body(Vx_world, Vy_world, info["yaw_rad"])

        Vw = 0.0
        if target_yaw is not None:
            err_yaw = _normalize_angle_deg(target_yaw - info["yaw_deg"])
            d_err_yaw = err_yaw - prev_err_yaw
            prev_err_yaw = err_yaw
            Vw = np.clip(Kp * (err_yaw / 90.0) + Kd * (d_err_yaw / 90.0), -1.0, 1.0)

        move(env, Vx=Vx_body, Vy=Vy_body, Vw=Vw)

    info = get_base_info(env)
    info["reached"] = reached
    return info


def follow_path(
    env,
    path,
    w=None,
    waypoint_threshold=0.18,
    goal_threshold=0.12,
    yaw_threshold=5.0,
    max_steps=3000,
    Kp_xy=1.4,
    Kd_xy=0.15,
    Kp_yaw=1.0,
    max_speed=0.45,
    max_turn=0.25,
):
    """
    Follow a global path with an omnidirectional PD controller.

    Converts path waypoints in world coordinates into MuJoCo body-frame
    velocity actions.
    """
    if not path:
        return {"success": False, "result": "空路径"}

    points = [(float(p["x"]), float(p["y"])) for p in path]
    index = 0
    prev_err_x = 0.0
    prev_err_y = 0.0

    for _ in range(max_steps):
        info = get_base_info(env)
        x_now, y_now = info["pos"][0], info["pos"][1]

        while index < len(points) - 1:
            dist = float(np.hypot(points[index][0] - x_now, points[index][1] - y_now))
            if dist > waypoint_threshold:
                break
            index += 1

        target_x, target_y = points[index]
        err_x = target_x - x_now
        err_y = target_y - y_now
        goal_err = float(np.hypot(points[-1][0] - x_now, points[-1][1] - y_now))

        if index == len(points) - 1 and goal_err < goal_threshold:
            break

        d_err_x = err_x - prev_err_x
        d_err_y = err_y - prev_err_y
        prev_err_x = err_x
        prev_err_y = err_y

        Vx_world = Kp_xy * err_x + Kd_xy * d_err_x
        Vy_world = Kp_xy * err_y + Kd_xy * d_err_y
        speed = float(np.hypot(Vx_world, Vy_world))
        if speed > max_speed:
            Vx_world = Vx_world / speed * max_speed
            Vy_world = Vy_world / speed * max_speed

        Vx_body, Vy_body = _world_to_body(Vx_world, Vy_world, info["yaw_rad"])
        move(env, Vx=Vx_body, Vy=Vy_body, Vw=0.0)

    # Final alignment: combined position+yaw using nav() so there is no
    # drift from a separate spin-then-correct sequence.
    target_yaw = float(w) if w is not None else None
    final_info = nav(
        env,
        points[-1][0], points[-1][1],
        target_yaw=target_yaw,
        Kp=2.0, Kd=0.2,
        pos_threshold=goal_threshold,
        yaw_threshold=yaw_threshold,
        max_steps=600,
    )
    final_err = float(np.hypot(
        points[-1][0] - final_info["pos"][0],
        points[-1][1] - final_info["pos"][1],
    ))
    if target_yaw is not None:
        yaw_err = abs(_normalize_angle_deg(target_yaw - final_info["yaw_deg"]))
        success = final_err < 0.30 and yaw_err < 15.0
    else:
        success = final_err < 0.30
    print(
        f"[follow_path] 到达误差={final_err:.3f}m yaw_err={abs(_normalize_angle_deg(target_yaw - final_info['yaw_deg'])):.1f}° 成功={success}"
        if target_yaw is not None else
        f"[follow_path] 到达误差={final_err:.3f}m 成功={success}",
        file=sys.stderr,
    )
    return {
        "success": success,
        "pos": final_info["pos"],
        "yaw": final_info["yaw_deg"],
        "goal_error": final_err,
        "waypoints": len(points),
    }
