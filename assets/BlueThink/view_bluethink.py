#!/usr/bin/env python3
"""BlueThink Mobile — MuJoCo viewer.

打开后:
  - 右侧面板 → Actuators → 拖动 wheel_ZQL/ZHL/YQL/YHL 控制底盘
  - 左侧面板 → Contact / Constraint 查看碰撞状态
  - 左键拖拽旋转 | 滚轮缩放 | 右键平移 | Esc 退出
"""
import os
import mujoco
import mujoco.viewer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
XML_PATH = os.path.join(SCRIPT_DIR, "scene_test.xml")

# Arm home pose
ARM_HOME = [0.5, 1.5, 0, 1.2, 0, 0, 0,    # left
            0.5, -1.5, 0, 1.2, 0, 0, 0]    # right


def main():
    model = mujoco.MjModel.from_xml_path(XML_PATH)
    data = mujoco.MjData(model)

    # Set arm home pose
    for i, v in enumerate(ARM_HOME):
        data.ctrl[i] = v

    print(f"BlueThink Mobile — {model.nbody} bodies, {model.njnt} joints, {model.nu} actuators")
    print(f"  ctrl[0..13]  手臂 (position)")
    print(f"  ctrl[14..17] 车轮 (velocity): wheel_ZQL  wheel_ZHL  wheel_YQL  wheel_YHL")

    with mujoco.viewer.launch_passive(model, data) as viewer:
        while viewer.is_running():
            mujoco.mj_step(model, data)
            viewer.sync()


if __name__ == "__main__":
    main()
