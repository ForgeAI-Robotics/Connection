"""Keyboard control for XLeRobot right arm in FQPlanner"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from serve_3dgs.backend.gs_config import GSConfig
from serve_3dgs.backend.sim_env import SimEnv
from serve_3dgs.backend.viewer_screens import (
    camera_screen_bindings,
    create_viewer_screen_images,
    update_viewer_screen_images,
)
from motrixsim.render import RenderApp


# Key layout (ergonomic, grouped by function):
#
#   Q   W   E   R       <- positive direction
#   |   |   |   |
#   A   S   D   F       <- negative direction
#
#   Q/A: Rotation (shoulder)
#   W/S: Pitch (shoulder)
#   E/D: Elbow
#   R/F: Wrist Pitch
#   T/G: Wrist Roll
#   Space: Gripper toggle
#
#   Home: Reset | ESC: Quit

class KeyboardController:
    def __init__(self, env):
        self.env = env
        self.step = 0.05
        self.gripper_closed = False
        self.actuators = {}
        for i in range(env.model.num_actuators):
            act = env.model.get_actuator(i)
            self.actuators[act.name] = act

    def set_act(self, name, value):
        if name in self.actuators:
            act = self.actuators[name]
            lo, hi = act.ctrl_range
            act.set_ctrl(self.env.data, float(np.clip(value, lo, hi)))

    def get_act(self, name):
        if name in self.actuators:
            return float(self.actuators[name].get_ctrl(self.env.data)[0])
        return 0.0

    def update(self, inp):
        # Right arm: Q/W/E/R/T for positive, A/S/D/F/G for negative
        if inp.is_key_pressed("Q"): self.set_act("Rotation_R", self.get_act("Rotation_R") + self.step)
        elif inp.is_key_pressed("A"): self.set_act("Rotation_R", self.get_act("Rotation_R") - self.step)

        if inp.is_key_pressed("W"): self.set_act("Pitch_R", self.get_act("Pitch_R") + self.step)
        elif inp.is_key_pressed("S"): self.set_act("Pitch_R", self.get_act("Pitch_R") - self.step)

        if inp.is_key_pressed("E"): self.set_act("Elbow_R", self.get_act("Elbow_R") + self.step)
        elif inp.is_key_pressed("D"): self.set_act("Elbow_R", self.get_act("Elbow_R") - self.step)

        if inp.is_key_pressed("R"): self.set_act("Wrist_Pitch_R", self.get_act("Wrist_Pitch_R") + self.step)
        elif inp.is_key_pressed("F"): self.set_act("Wrist_Pitch_R", self.get_act("Wrist_Pitch_R") - self.step)

        if inp.is_key_pressed("T"): self.set_act("Wrist_Roll_R", self.get_act("Wrist_Roll_R") + self.step)
        elif inp.is_key_pressed("G"): self.set_act("Wrist_Roll_R", self.get_act("Wrist_Roll_R") - self.step)

        # Gripper
        if inp.is_key_just_pressed("Space"):
            self.gripper_closed = not self.gripper_closed
            self.set_act("Jaw_R", 1.0 if self.gripper_closed else -0.37)

        # Reset
        if inp.is_key_just_pressed("Home"):
            self.env.data.reset(self.env.model)
            self.env.forward_kinematic()
            self.gripper_closed = False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Keyboard control for XLeRobot/BlueThink")
    parser.add_argument("--robot", type=str, default="xlerobot",
                        help="Robot name: xlerobot (default), BlueThink")
    parser.add_argument("--scene", type=str, default="")
    args = parser.parse_args()

    gs_cfg = GSConfig(scene=args.scene, robot_name=args.robot)
    env = SimEnv(gs_cfg.scene_xml, gs_cfg, enable_renderers=True)
    print(f"Model: {env.model.num_links} links")

    gs_screen_bindings = ()
    try:
        gs_screen_bindings = camera_screen_bindings(env.model, gs_cfg.default_viewer_cameras)
    except:
        pass

    controller = KeyboardController(env)

    print("\n=== Right Arm Control ===")
    print("  Q(+)/A(-): Rotation")
    print("  W(+)/S(-): Pitch")
    print("  E(+)/D(-): Elbow")
    print("  R(+)/F(-): Wrist Pitch")
    print("  T(+)/G(-): Wrist Roll")
    print("  Space: Gripper")
    print("  Home: Reset | ESC: Quit")
    print("=========================\n")

    with RenderApp() as render:
        render.launch(env.model)
        render.sync(env.data)

        gs_images = {}
        if gs_screen_bindings:
            gs_images = create_viewer_screen_images(render, gs_screen_bindings, width=320, height=240)
            if gs_images:
                update_viewer_screen_images(env, gs_screen_bindings, gs_images, width=320, height=240)
                render.sync(env.data)

        while not render.is_closed:
            controller.update(render.input)
            env.step(1)
            env.forward_kinematic()
            if gs_images:
                try:
                    update_viewer_screen_images(env, gs_screen_bindings, gs_images, width=320, height=240)
                except:
                    pass
            render.sync(env.data)

    print("Done.")


if __name__ == "__main__":
    main()
