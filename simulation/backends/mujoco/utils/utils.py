"""
utils.py - 场景工具函数
提供固定场景创建、家具/物体查询、机器人状态获取等功能
与 demo_kitchen_scenes.py 完全一致的环境创建和交互方式
"""

import json
import numpy as np
import robosuite
import robocasa  # 必须导入，触发 Kitchen 环境注册到 robosuite
import robocasa.macros as macros
from robosuite.controllers import load_composite_controller_config

# 下面这些只在交互式遥操作(init_device/run_interactive)时才需要;headless serve 用不到,
# 懒导入避免某些 robocasa 版本缺这些模块时整个 utils 加载失败。


# ============================================================
# 场景创建
# ============================================================

def create_scene(scene_dir="scene", seed=42):
    """
    从本地 scene/ 目录创建固定的厨房场景环境

    Args:
        scene_dir:  场景配置目录（包含 layout 和 style YAML）
        seed:       随机种子，保证场景可复现

    Returns:
        env: MyKitchen 环境对象（已包裹 EnclosingWallRenderWrapper）
    """
    import os
    from scene.make_scene import MyKitchen

    # headless(默认):无交互 viewer、开 offscreen 渲染(截图/segmentation 用);
    # 不套 EnclosingWallRenderWrapper(那是交互 viewer 的半透明墙+热键,无头不需要,
    # 且套了会挡住直接 env.model/env.set_object_pos 等 helper 访问)。
    env = MyKitchen(
        scene_dir=os.path.abspath(scene_dir),
        robots="PandaOmron",
        controller_configs=load_composite_controller_config(robot="PandaOmron"),
        has_renderer=False,
        has_offscreen_renderer=True,
        render_camera=None,
        ignore_done=True,
        use_camera_obs=False,
        control_freq=20,
        renderer="mujoco",
        translucent_robot=False,
        seed=seed,
    )
    return env


def init_device(env, device_type="keyboard"):
    """
    初始化控制设备（与 demo 一致）

    Args:
        env: 环境对象
        device_type: "keyboard" 或 "spacemouse"

    Returns:
        device: 控制设备对象
    """
    if device_type == "keyboard":
        from robosuite.devices import Keyboard
        return Keyboard(env=env, pos_sensitivity=4.0, rot_sensitivity=4.0)
    elif device_type == "spacemouse":
        from robosuite.devices import SpaceMouse
        return SpaceMouse(
            env=env,
            pos_sensitivity=4.0,
            rot_sensitivity=4.0,
            vendor_id=macros.SPACEMOUSE_VENDOR_ID,
            product_id=macros.SPACEMOUSE_PRODUCT_ID,
        )
    else:
        raise ValueError(f"不支持的设备类型: {device_type}")


def run_interactive(env, device):
    """
    启动交互式遥操作循环
    通过 collect_human_trajectory 驱动渲染和机器人控制

    按 Q 退出当前回合

    Args:
        env: 环境对象
        device: 控制设备（由 init_device 返回）
    """
    from robocasa.scripts.collect_demos import collect_human_trajectory
    collect_human_trajectory(
        env,
        device,
        "right",              # 控制右臂
        "single-arm-opposed",  # 单臂对立配置
        mirror_actions=True,
        render=True,          # mjviewer 模式下 render 参数实际不生效，但保持与 demo 一致
        max_fr=30,
        print_info=False,
    )


# ============================================================
# 家具查询
# ============================================================

def get_all_fixtures(env):
    """获取场景中所有家具的名称列表"""
    return list(env.fixtures.keys())


def get_fixture_pos(env, name):
    """获取家具位置，支持模糊匹配"""
    results = {}
    for n, fxtr in env.fixtures.items():
        if name in n:
            results[n] = np.array(fxtr.pos, dtype=float)
    return results


def get_fixture_size(env, name):
    """获取家具尺寸 [宽, 深, 高]，支持模糊匹配"""
    results = {}
    for n, fxtr in env.fixtures.items():
        if name in n:
            results[n] = np.array(fxtr.size, dtype=float)
    return results


def get_fixture_detail(env, name):
    """获取家具完整信息：位置、尺寸、类型"""
    results = {}
    for n, fxtr in env.fixtures.items():
        if name in n:
            results[n] = {
                "pos": np.array(fxtr.pos, dtype=float),
                "size": np.array(fxtr.size, dtype=float),
                "type": type(fxtr).__name__,
            }
    return results


# ============================================================
# 机器人状态
# ============================================================

def get_robot_ee_pos(env):
    """获取机器人末端执行器（夹爪）的当前位置"""
    ee_id = env.sim.model.body_name2id("robot0_right_hand")
    return env.sim.data.body_xpos[ee_id].copy()


def get_robot_base_pos(env):
    """获取机器人底座的当前位置"""
    base_id = env.sim.model.body_name2id("robot0_base")
    return env.sim.data.body_xpos[base_id].copy()


# ============================================================
# 场景信息打印
# ============================================================

def print_fixtures(env):
    """打印场景中所有家具的位置和尺寸"""
    print(f"\n{'名称':<42s} {'类型':<20s} {'位置 [x,y,z]':<30s} {'尺寸 [宽,深,高]'}")
    print("-" * 130)
    for name, fxtr in env.fixtures.items():
        pos = np.array2string(np.array(fxtr.pos, dtype=float), precision=3)
        size = np.array2string(np.array(fxtr.size, dtype=float), precision=3)
        ftype = type(fxtr).__name__
        print(f"  {name:<40s} {ftype:<20s} {pos:<30s} {size}")


def print_summary(env):
    """打印场景摘要：关键家具的位置"""
    keywords = {
        "台面": "counter",
        "炉灶": "stove",
        "水槽": "sink",
        "冰箱": "fridge",
        "微波炉": "microwave",
        "洗碗机": "dishwasher",
        "橱柜": "cab",
        "抽屉": "drawer",
        "餐台": "dining",
    }
    print("\n关键家具位置:")
    for label, kw in keywords.items():
        matches = [n for n in env.fixtures if kw in n.lower() and "backing" not in n.lower() and "base" not in n.lower()]
        for n in matches[:3]:
            pos = np.array2string(np.array(env.fixtures[n].pos, dtype=float), precision=3)
            print(f"  {label} {n}: {pos}")
        if len(matches) > 3:
            print(f"  ... 还有 {len(matches) - 3} 个")

    ee_pos = np.array2string(get_robot_ee_pos(env), precision=3)
    print(f"\n机器人末端位置: {ee_pos}")
