"""
main.py - RoboCasa 固定场景仿真主程序(robosuite PandaOmron 真机器人)
加载 scene/config 场景,创建 robosuite MyKitchen(真臂 OSC + 真控制器),启动 Flask API。

使用方式:
    python main.py [--no-viewer]
    API 地址: http://localhost:5001
"""

import argparse
import os
import sys
import time

import numpy as np
from termcolor import colored

SERVE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVE_DIR)

from utils.utils import create_scene
from service.server import (
    start_server,
    process_commands,
    try_record_frame,
    get_lock,
)


# 方案A:显式把 demo 物体钉到"已知可达"的台面坐标,绕开 robocasa layout 7 随机 placement
# 把物体甩到南岛台死区的问题。三块可达台面(top_z≈0.92):
#   东  counter_1_right (y≈-1.55, 从 nav_044 面南可达,mug 验证过) —— 组装区
#   中北 counter_main    (y≈-0.60, 从 nav_012/nav_014 面北可达)
#   西北 counter_1_main  (y≈-0.60, 从 nav_001/nav_004 面北可达)
# 三样食材(bread/cheese/ketchup)分散到不同台面,凑齐需跨厨房导航;plate 组装区在东侧。
# 全放"可靠又可达"的北侧台面(counter_1_main + counter_main, y≈-0.6, top_z≈0.92),
# 从过道工作点 nav_001/004/012/014/044 面北可达。东侧 counter_1_right 碰撞面有洞(会掉穿/弹飞),弃用。
# 3 样食材(ketchup 西 / bread 中 / cheese 中)+ plate 组装目标,分散需跨厨房导航。
_DEMO_LAYOUT = {
    "sponge":  [0.8, -0.60, 0.95],
    "ketchup": [1.3, -0.60, 0.95],   # 食材(西,最远)
    "plate":   [2.0, -0.42, 0.95],   # 组装目标(往台面里挪,远离前沿 y=-0.66,免得食材放上去滚下台)
    "bowl":    [2.6, -0.60, 0.95],
    "bread":   [3.2, -0.60, 0.95],   # 食材(中)
    "cheese":  [3.8, -0.60, 0.95],   # 食材(中)
    "cup":     [4.4, -0.60, 0.95],
    "pot":     [5.0, -0.60, 0.95],
    "apple":   [5.5, -0.60, 0.95],
    "mug":     [6.1, -1.55, 0.95],   # 东台(唯一验证可停的点)
}


def _place_demo_objects(env):
    """把 demo 物体显式摆到 _DEMO_LAYOUT 的可达坐标(reset 后、建 belief 前调用)。

    从台面上方 12cm 落下再 settle,避免直接 set 在台面高度时初始穿模被物理弹飞。
    settle 后 belief 按稳定位置建,反映真实落点。"""
    for name, pos in _DEMO_LAYOUT.items():
        if name in env.obj_body_id:
            try:
                env.set_object_pos(name, [pos[0], pos[1], pos[2] + 0.12])
            except Exception as e:
                print(f"[demo] 摆放 {name} 失败: {e}")
    # settle:零动作步进让物体落到台面稳定(idle=手臂模式零 delta,机器人不动)
    idle = np.zeros(env.action_dim)
    idle[-1] = -1.0
    for _ in range(120):
        env.step(idle)
    n = len([n for n in _DEMO_LAYOUT if n in env.obj_body_id])
    print(f"[demo] 已显式摆放 {n} 个物体到可达台面并 settle")


def _init_belief(env):
    """初始化 belief(机器人的场景记忆/scene graph)。

    按物体真实位置生成 belief(工作点→fixture→物体)= 上帝视角。
    这是有意为之:坐标组负责"进新房间跑一趟,拿到所有家具/物体坐标与 on/inside 关系,东西被
    移动后再看一眼更新关系",所以感知/建图那层可直接假设已知真值,机器人开局就有完整 scene graph。
    """
    from scene import scene_memory
    try:
        state = scene_memory.build_initial_state(env)
        import yaml
        with open(scene_memory.INITIAL_PATH, "w", encoding="utf-8") as f:
            yaml.dump(state, f, allow_unicode=True, default_flow_style=False)
        scene_memory.reset_to_initial()   # belief = 真值(上帝视角)
    except Exception as e:
        print(f"[main] belief 初始化失败(非致命): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-viewer", action="store_true",
                        help="headless(默认就是无 viewer;此 flag 保留兼容旧启动命令)")
    parser.add_argument("--viewer", action="store_true",
                        help="打开 MuJoCo 交互 viewer 观看执行。macOS 必须用 mjpython 启动:"
                             "mjpython main.py --viewer(普通 python 会报错)")
    args = parser.parse_args()

    print(colored("正在从 scene/ 加载 robosuite PandaOmron 厨房场景...", "yellow"))
    env = create_scene(scene_dir=os.path.join(SERVE_DIR, "scene"), seed=42)
    env.reset()
    _place_demo_objects(env)   # 方案A:显式摆到可达台面(必须在建 belief 前,belief 按物体位置分配工作点)
    _init_belief(env)

    # 调试信息
    print(f"[debug] action_dim = {env.action_dim}")
    print(f"[debug] 底座位置 = {env.get_body_pos('mobilebase0_base').round(3).tolist()}")
    print(f"[debug] 末端位置 = {env.get_body_pos('robot0_right_hand').round(3).tolist()}")
    print(f"[debug] objects = {list(env.obj_body_id.keys())}")

    start_server(env, port=5001)

    # 可选:打开交互 viewer 观看机器人执行(--viewer)。
    # macOS 上 mujoco.viewer.launch_passive 必须由 mjpython 启动,否则抛错 → 这里兜住并给出提示。
    # 相机离屏渲染(截图/感知)与 viewer 用各自独立 GL context,一般可共存;若你的机器上冲突(崩/花屏),
    # 就是"不能同时开"的情况,把感知那侧的截图停掉只留 viewer 即可(告诉我我加个 viewer-only 开关)。
    viewer = None
    if getattr(args, "viewer", False):
        try:
            import mujoco.viewer
            viewer = mujoco.viewer.launch_passive(env.raw_model, env.raw_data)
            print(colored("MuJoCo viewer 已打开(关掉窗口或 Ctrl+C 退出)。", "green"))
        except Exception as e:
            print(colored(f"[viewer] 打开失败: {e}", "red"))
            print(colored("  macOS 请用:  mjpython serve/main.py --viewer", "yellow"))
            viewer = None

    print(colored("\n仿真器已就绪(真臂 OSC + PD 导航)。", "green"))
    print("API 地址: http://localhost:5001")
    print("按 Ctrl+C 退出\n")

    # 主循环:process_commands 同步执行命令(nav/grasp/place 内部自己 env.step),
    # 空闲时用零动作步进保持物理活跃(OSC 控制器会 hold 住臂当前位姿)。
    idle_action = np.zeros(env.action_dim)
    idle_action[-1] = -1.0  # 手臂模式,零 delta = 保持当前臂位姿
    try:
        while True:
            with get_lock():
                process_commands(env)
                try_record_frame()
                env.step(idle_action)
                if viewer is not None:
                    viewer.sync()   # 刷新 viewer(同 lock、同主线程,与离屏渲染不并发)
            if viewer is not None and not viewer.is_running():
                break  # 用户关掉 viewer 窗口 → 退出
            time.sleep(0.002)
    except KeyboardInterrupt:
        pass

    if viewer is not None:
        viewer.close()
    env.close()
    print(colored("场景已关闭。", "yellow"))
