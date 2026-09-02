"""demo.py - Keyboard-controlled XLeRobot locomotion with 3DGS rendering."""

import os
import sys
import argparse
import time

import numpy as np

os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["ALSOFT_DRIVERS"] = "null"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.gs_config import GSConfig
from backend.sim_env import SimEnv
from backend.viewer_screens import (
    camera_screen_bindings,
    create_viewer_screen_images,
    update_viewer_screen_images,
)


def find_camera_id(model, camera_name: str):
    for idx, camera in enumerate(model.cameras.cameras if hasattr(model.cameras, 'cameras') else model.cameras):
        name = getattr(camera, "name", "")
        if name == camera_name or name.endswith(camera_name):
            return idx
    return None


def main():
    parser = argparse.ArgumentParser(description="Keyboard control for XLeRobot")
    parser.add_argument("--scene", type=str, default="")
    parser.add_argument("--scene_config", type=str, default="")
    parser.add_argument("--gs_assets", type=str, default="")
    parser.add_argument("--robot_gs_dir", type=str, default="")
    parser.add_argument("--no-viewer", action="store_true")
    parser.add_argument("--gs_w", type=int, default=320)
    parser.add_argument("--gs_h", type=int, default=240)
    parser.add_argument("--viewer_gs_fps", type=float, default=5.0)
    parser.add_argument("--viewer_cameras", type=str,
                        default="follower,head_cam,right_arm_cam,left_arm_cam")
    parser.add_argument("--robot", type=str, default="",
                        help="Robot name override (default: read from assets/config.yaml)")
    args = parser.parse_args()

    gs_cfg = GSConfig(
        assets_dir=args.gs_assets,
        scene=args.scene,
        robot_gs_dir=args.robot_gs_dir or None,
        scene_config=args.scene_config or None,
        robot_name=args.robot,
    )
    print(f"Loading scene: {gs_cfg.scene_xml}")
    env = SimEnv(gs_cfg.scene_xml, gs_cfg)
    print(f"Model loaded: {env.model.num_links} links, {env.model.num_dof_pos} DOFs")

    forward_act = None
    turn_act = None
    for i in range(env.model.num_actuators):
        act = env.model.get_actuator(i)
        if act.name == "forward":
            forward_act = act
        elif act.name == "turn":
            turn_act = act

    if forward_act is None or turn_act is None:
        print("ERROR: forward/turn actuators not found in model")
        sys.exit(1)

    chassis_idx = list(env.model.link_names).index("chassis")
    command = np.zeros(3, dtype=np.float32)

    def apply_command():
        forward = float(np.clip(command[0] * 1.0, -1.0, 1.0))
        turn = float(np.clip(-command[2] * 0.5, -1.0, 1.0))
        forward_act.set_ctrl(env.data, forward)
        turn_act.set_ctrl(env.data, turn)

    def print_state():
        poses = env.model.get_link_poses(env.data)
        pose = poses[0, chassis_idx] if poses.ndim == 3 else poses[chassis_idx]
        x, y, z = pose[:3]
        qw, qx, qy, qz = pose[6], pose[3], pose[4], pose[5]
        yaw = np.rad2deg(np.arctan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz)))
        bl_yaw = (yaw + 180) % 360 - 180
        fwd = command[0]
        trn = command[2]
        print(f"\rpos=[{x:+.2f}, {y:+.2f}, {z:.2f}]  heading={bl_yaw:+.1f}°  "
              f"fwd={fwd:+.1f} turn={trn:+.1f}  ", end="", flush=True)

    from motrixsim.render import RenderApp, Layout

    # Setup viewer camera screens
    camera_names = tuple(name.strip() for name in args.viewer_cameras.split(",") if name.strip())
    gs_screen_bindings = ()
    gs_screen_images = {}
    try:
        gs_screen_bindings = camera_screen_bindings(env.model, camera_names)
        print(f"3DGS viewer screens: "
              + ", ".join(f"{b.camera_name}->cam{b.camera_id}" for b in gs_screen_bindings),
              flush=True)
    except ValueError as exc:
        print(f"3DGS viewer screens disabled: {exc}", flush=True)

    gs_update_interval = 1.0 / max(float(args.viewer_gs_fps), 0.1)

    print("Controls:")
    print("  W / Up Arrow    : Forward")
    print("  S / Down Arrow  : Backward")
    print("  A               : Turn Left")
    print("  D               : Turn Right")
    print("  ESC             : Exit")
    print()

    if args.no_viewer:
        print("Running headless (Ctrl+C to stop)...")
        try:
            while True:
                command[:] = 0.0
                apply_command()
                for _ in range(10):
                    env.step()
                env.forward_kinematic()
                print_state()
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
    else:
        with RenderApp() as render:
            render.launch(env.model)
            render.sync(env.data)

            # Create 3DGS camera widgets
            if gs_screen_bindings:
                gs_screen_images = create_viewer_screen_images(
                    render, gs_screen_bindings,
                    width=args.gs_w, height=args.gs_h,
                )
                try:
                    update_viewer_screen_images(
                        env, gs_screen_bindings, gs_screen_images,
                        width=args.gs_w, height=args.gs_h,
                    )
                    render.sync(env.data)
                except Exception as exc:
                    print(f"Initial 3DGS render failed: {exc}", flush=True)
                next_gs_update_at = time.perf_counter() + gs_update_interval

            print("Render launched. Click viewport, then use W/A/S/D or arrow keys.", flush=True)
            render_tick = [0]

            def phys_step():
                from motrixsim import step as mstep
                apply_command()
                mstep(env.model, env.data)

            def render_step():
                render_tick[0] += 1
                inp = render.input

                if inp.is_key_pressed("up") or inp.is_key_pressed("w"):
                    command[0] = 1.0
                elif inp.is_key_pressed("down") or inp.is_key_pressed("s"):
                    command[0] = -1.0
                else:
                    command[0] = 0.0

                if inp.is_key_pressed("a"):
                    command[2] = 2.0
                elif inp.is_key_pressed("d"):
                    command[2] = -2.0
                else:
                    command[2] = 0.0

                if inp.is_key_just_pressed("esc") or inp.is_key_just_pressed("escape"):
                    return False

                env.forward_kinematic()

                now = time.perf_counter()
                if gs_screen_images and now >= next_gs_update_at[0]:
                    try:
                        update_viewer_screen_images(
                            env, gs_screen_bindings, gs_screen_images,
                            width=args.gs_w, height=args.gs_h,
                        )
                    except Exception:
                        pass
                    next_gs_update_at[0] = now + gs_update_interval

                if render_tick[0] % 10 == 0:
                    print_state()

                render.sync(env.data)
                return True

            next_gs_update_at = [time.perf_counter()]

            try:
                from motrixsim import run
                run.render_loop(env.model.options.timestep, 60.0, phys_step, render_step)
            except ImportError:
                print("render_loop not available, using fallback loop...")
                gs_update_counter = [0]
                while True:
                    inp = render.input
                    if inp.is_key_pressed("up") or inp.is_key_pressed("w"):
                        command[0] = 1.0
                    elif inp.is_key_pressed("down") or inp.is_key_pressed("s"):
                        command[0] = -1.0
                    else:
                        command[0] = 0.0
                    if inp.is_key_pressed("a"):
                        command[2] = 2.0
                    elif inp.is_key_pressed("d"):
                        command[2] = -2.0
                    else:
                        command[2] = 0.0
                    if inp.is_key_just_pressed("esc"):
                        break
                    apply_command()
                    for _ in range(10):
                        env.step()
                    env.forward_kinematic()
                    gs_update_counter[0] += 1
                    if gs_screen_images and gs_update_counter[0] % 12 == 0:
                        try:
                            update_viewer_screen_images(
                                env, gs_screen_bindings, gs_screen_images,
                                width=args.gs_w, height=args.gs_h,
                            )
                        except Exception:
                            pass
                    render.sync(env.data)
                    time.sleep(0.01)

    print("\nShutdown.")


if __name__ == "__main__":
    main()
