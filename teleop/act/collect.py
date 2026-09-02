"""Collect demonstration data using keyboard control with 3DGS viewer."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import torch
from serve_3dgs.backend.gs_config import GSConfig
from serve_3dgs.backend.sim_env import SimEnv
from serve_3dgs.backend.viewer_screens import (
    camera_screen_bindings, create_viewer_screen_images, update_viewer_screen_images,
)
from motrixsim.render import RenderApp
from teleop.act.data.collector import DataCollector


class CollectController:
    def __init__(self, env):
        self.env = env
        self.step = 0.05
        self.gripper_closed = False
        self.recording = False
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

    def get_qpos(self):
        return np.array([
            self.get_act("Rotation_R"),
            self.get_act("Pitch_R"),
            self.get_act("Elbow_R"),
            self.get_act("Wrist_Pitch_R"),
            self.get_act("Wrist_Roll_R"),
            self.get_act("Jaw_R"),
        ], dtype=np.float32)

    def update(self, inp):
        action = np.zeros(6, dtype=np.float32)

        if inp.is_key_pressed("Q"): action[0] = self.step
        elif inp.is_key_pressed("A"): action[0] = -self.step
        if inp.is_key_pressed("W"): action[1] = self.step
        elif inp.is_key_pressed("S"): action[1] = -self.step
        if inp.is_key_pressed("E"): action[2] = self.step
        elif inp.is_key_pressed("D"): action[2] = -self.step
        if inp.is_key_pressed("R"): action[3] = self.step
        elif inp.is_key_pressed("F"): action[3] = -self.step
        if inp.is_key_pressed("T"): action[4] = self.step
        elif inp.is_key_pressed("G"): action[4] = -self.step

        for i, name in enumerate(["Rotation_R", "Pitch_R", "Elbow_R", "Wrist_Pitch_R", "Wrist_Roll_R"]):
            self.set_act(name, self.get_act(name) + action[i])

        if inp.is_key_just_pressed("Space"):
            self.gripper_closed = not self.gripper_closed
            self.set_act("Jaw_R", 1.0 if self.gripper_closed else -0.37)
            action[5] = 1.0 if self.gripper_closed else -1.0

        if inp.is_key_just_pressed("Home"):
            self.env.data.reset(self.env.model)
            self.env.forward_kinematic()
            self.gripper_closed = False
            action = None

        return action


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Collect data for XLeRobot/BlueThink")
    parser.add_argument("--robot", type=str, default="xlerobot",
                        help="Robot name: xlerobot (default), BlueThink")
    parser.add_argument("--scene", type=str, default="xlerobot_nav")
    args = parser.parse_args()

    gs_cfg = GSConfig(scene=args.scene, robot_name=args.robot)
    env = SimEnv(gs_cfg.scene_xml, gs_cfg, enable_renderers=True)
    print(f"Model: {env.model.num_links} links")

    gs_screen_bindings = ()
    try:
        gs_screen_bindings = camera_screen_bindings(env.model, gs_cfg.default_viewer_cameras)
    except:
        pass

    controller = CollectController(env)
    collector = DataCollector()
    episode_id = 0

    print("\n=== Data Collection ===")
    print("Q/W/E/R/T: Arm joints (+)")
    print("A/S/D/F/G: Arm joints (-)")
    print("Space: Gripper")
    print("Enter: Save episode")
    print("N: New episode")
    print("Home: Reset | ESC: Quit")
    print("=======================\n")

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
            action = controller.update(render.input)

            if render.input.is_key_just_pressed("Return"):
                filename = collector.save(episode_id=episode_id)
                if filename:
                    episode_id += 1
                collector.reset()

            if render.input.is_key_just_pressed("N"):
                collector.reset()
                print("New episode")

            if action is not None and np.any(action != 0):
                qpos = controller.get_qpos()
                # Get camera image (simplified - just use current frame)
                collector.record(np.zeros((3, 224, 224)), qpos, action)

            env.step(1)
            env.forward_kinematic()

            if gs_images:
                try:
                    update_viewer_screen_images(env, gs_screen_bindings, gs_images, width=320, height=240)
                except:
                    pass

            render.sync(env.data)

    print(f"Done. Saved {episode_id} episodes.")


if __name__ == "__main__":
    main()
