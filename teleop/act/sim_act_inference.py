"""
ACT Policy Inference in Simulation for BlueThink Dual-Arm Robot.

Loads a LeRobot ACT policy checkpoint and runs it with the MotrixSim
simulation environment. Renders 3 camera views (base, left wrist, right wrist)
and controls 14 joint actuators based on policy output.

Usage:
    # Sim cameras (MuJoCo render)
    python teleop/act/sim_act_inference.py \
        --policy-path outputs/train/act_clear_table/checkpoints/100000/pretrained_model \
        --device cuda

    # Real dataset images (bypass sim rendering)
    python teleop/act/sim_act_inference.py \
        --policy-path outputs/train/act_clear_table/checkpoints/100000/pretrained_model \
        --data-path /path/to/lerobot_dataset \
        --episode 0 \
        --device cuda
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from serve_3dgs.backend.gs_config import GSConfig
from serve_3dgs.backend.sim_env import SimEnv
from serve_3dgs.backend.viewer_screens import viewer_widget_layout_specs

BLUETHINK_LEFT_JOINTS = [
    "Left_Shoulder_Pitch_Joint",
    "Left_Shoulder_Roll_Joint",
    "Left_Shoulder_Yaw_Joint",
    "Left_Elbow_Pitch_Joint",
    "Left_Wrist_Yaw_Joint",
    "Left_Wrist_Pitch_Joint",
    "Left_Wrist_Roll_Joint",
]

BLUETHINK_RIGHT_JOINTS = [
    "Right_Shoulder_Pitch_Joint",
    "Right_Shoulder_Roll_Joint",
    "Right_Shoulder_Yaw_Joint",
    "Right_Elbow_Pitch_Joint",
    "Right_Wrist_Yaw_Joint",
    "Right_Wrist_Pitch_Joint",
    "Right_Wrist_Roll_Joint",
]

BLUETHINK_JOINTS = BLUETHINK_LEFT_JOINTS + BLUETHINK_RIGHT_JOINTS
NUM_JOINTS = len(BLUETHINK_JOINTS)

CAMERA_NAMES = ["base_cam", "left_ee_cam", "right_ee_cam"]
POLICY_IMAGE_KEYS = [
    "observation.images.top",
    "observation.images.left_wrist",
    "observation.images.right_wrist",
]

INIT_QPOS_LEFT = [-0.5035, 0.6527, 1.1747, 1.65, -0.7471, 0.2444, 0.9295]
INIT_QPOS_RIGHT = [0.3455, -0.5776, -0.9003, 1.6831, 0.6373, -0.0942, -0.7968]
INIT_QPOS = np.array(INIT_QPOS_LEFT + INIT_QPOS_RIGHT, dtype=np.float64)


def _find_actuator(env, joint_name: str):
    for i in range(env.model.num_actuators):
        act = env.model.get_actuator(i)
        if act.target_name == joint_name:
            return act
    return None


def get_qpos(env) -> np.ndarray:
    """Read current joint positions from sim data."""
    dof_pos = np.asarray(env.data.dof_pos, dtype=np.float64).reshape(-1)
    if dof_pos.size >= NUM_JOINTS:
        return dof_pos[:NUM_JOINTS].copy()
    return dof_pos.copy()


def set_qpos(env, qpos: np.ndarray):
    """Set target joint positions via actuator controls."""
    for i, name in enumerate(BLUETHINK_JOINTS):
        act = _find_actuator(env, name)
        if act is not None:
            act.set_ctrl(env.data, float(qpos[i]))


def build_camera_name_to_id(env) -> Dict[str, int]:
    """Build name -> index map for all cameras in the model."""
    name_to_id: Dict[str, int] = {}
    for idx in range(len(env.model.cameras)):
        cam = env.model.cameras[idx]
        if cam.name:
            name_to_id[cam.name] = idx
    return name_to_id


def image_hwc_to_tensor(image: np.ndarray, device: str) -> 'torch.Tensor':
    """Convert HWC uint8 RGB image to batched CHW float tensor for LeRobot."""
    import torch
    arr = np.asarray(image, dtype=np.uint8)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    tensor = torch.from_numpy(arr.copy()).permute(2, 0, 1).float().div_(255.0)
    return tensor.unsqueeze(0).to(device=device, non_blocking=True)


def import_act_policy_class():
    candidates = (
        "lerobot.policies.act.modeling_act.ACTPolicy",
        "lerobot.common.policies.act.modeling_act.ACTPolicy",
    )
    errors = []
    for dotted in candidates:
        module_name, class_name = dotted.rsplit(".", 1)
        try:
            module = importlib.import_module(module_name)
            return getattr(module, class_name)
        except BaseException as e:
            errors.append(f"{dotted}: {e!r}")
    raise ImportError("Could not import ACTPolicy. Tried:\n  " + "\n  ".join(errors))


def load_act_policy(policy_path: str, device: str):
    global _action_mean, _action_std
    ACTPolicy = import_act_policy_class()
    try:
        policy = ACTPolicy.from_pretrained(policy_path, device=device)
    except TypeError:
        policy = ACTPolicy.from_pretrained(policy_path)
    policy.to(device)
    policy.eval()
    if hasattr(policy, "reset"):
        policy.reset()
    # Pre-load action normalization stats for manual unnormalization
    _action_mean, _action_std = _load_action_norm_stats(policy_path)
    if _action_mean is not None:
        print(f"Action norm stats loaded (manual unnormalize). mean={_action_mean[:3].tolist()}...")
    return policy


def _load_action_norm_stats(policy_path: str):
    """Load action mean/std from postprocessor safetensors for manual unnormalization.

    policy.unnormalize_outputs() is a no-op in some LeRobot versions, so we
    apply the inverse transform ourselves.
    """
    import glob, os
    from safetensors.torch import load_file as _load

    st_file = glob.glob(os.path.join(policy_path, "*unnormalizer*")) or \
              glob.glob(os.path.join(policy_path, "*.safetensors"))
    if not st_file:
        return None, None
    for f in st_file:
        d = _load(f)
        if "action.mean" in d and "action.std" in d:
            return d["action.mean"].float(), d["action.std"].float()
    return None, None


_action_mean: Optional[torch.Tensor] = None
_action_std: Optional[torch.Tensor] = None


def decode_video_frames(video_path: str, num_frames: int) -> list:
    """Decode video file into list of HWC uint8 numpy arrays."""
    import av
    frames = []
    container = av.open(video_path)
    stream = container.streams.video[0]
    for i, packet in enumerate(container.demux(stream)):
        if i >= num_frames:
            break
        for frame in packet.decode():
            arr = frame.to_ndarray(format="rgb24")
            frames.append(arr)
    container.close()
    return frames


def load_dataset_episode(data_path: str, episode_idx: int):
    """Load one episode: (state_array, action_array, images_dict, num_frames)."""
    import json, os, pandas as pd

    with open(os.path.join(data_path, "meta", "info.json")) as f:
        info = json.load(f)

    # Find episode metadata
    ep_dir = os.path.join(data_path, "meta", "episodes", "chunk-000")
    ep_files = sorted([f for f in os.listdir(ep_dir) if f.endswith(".parquet")])
    ep_df = pd.read_parquet(os.path.join(ep_dir, ep_files[episode_idx]))

    from_idx = int(ep_df["dataset_from_index"].iloc[0])
    to_idx = int(ep_df["dataset_to_index"].iloc[0])
    length = int(ep_df["length"].iloc[0])

    # Find the data chunk
    for ck in range(100):
        data_dir = os.path.join(data_path, "data", f"chunk-{ck:03d}")
        if not os.path.isdir(data_dir):
            break
        for fi in range(100):
            fp = os.path.join(data_dir, f"file-{fi:03d}.parquet")
            if not os.path.exists(fp):
                break
            df = pd.read_parquet(fp)
            mask = (df["index"] >= from_idx) & (df["index"] < to_idx)
            subset = df[mask].copy().sort_values("index").reset_index(drop=True)
            if len(subset) > 0:
                states = np.stack(subset["observation.state"].values)
                actions = np.stack(subset["action"].values)
                break
        else:
            continue
        break
    else:
        raise RuntimeError(f"Episode {episode_idx} data not found")

    # Load video frames for each camera
    camera_keys = [
        "observation.images.top",
        "observation.images.left_wrist",
        "observation.images.right_wrist",
    ]
    images: Dict[str, list] = {}
    for key in camera_keys:
        ck_idx = int(ep_df[f"videos/{key}/chunk_index"].iloc[0])
        fi_idx = int(ep_df[f"videos/{key}/file_index"].iloc[0])
        video_path = os.path.join(
            data_path, "videos", key,
            f"chunk-{ck_idx:03d}", f"file-{fi_idx:03d}.mp4"
        )
        if os.path.exists(video_path):
            images[key] = decode_video_frames(video_path, length)
            print(f"  Loaded {key}: {len(images[key])} frames")
        else:
            print(f"  WARNING: {video_path} not found, using zeros")
            images[key] = [np.zeros((480, 640, 3), dtype=np.uint8)] * length

    return states, actions, images, length


def step_policy(policy, state: np.ndarray, images: Dict[str, np.ndarray], device: str) -> np.ndarray:
    import torch
    import sys
    mod = sys.modules[__name__]

    state_t = torch.as_tensor(state, dtype=torch.float32, device=device).unsqueeze(0)

    batch = {"observation.state": state_t}
    for name, sim_name in zip(POLICY_IMAGE_KEYS, CAMERA_NAMES):
        img = images.get(sim_name)
        if img is None:
            img = images.get(name)
        if img is not None:
            batch[name] = image_hwc_to_tensor(img, device)

    if hasattr(policy, "normalize_inputs"):
        batch = policy.normalize_inputs(batch)
    elif hasattr(policy, "preprocessor"):
        batch = policy.preprocessor(batch)

    with torch.inference_mode():
        action = policy.select_action(batch)

        # Manual unnormalization: policy.unnormalize_outputs() is a no-op in
        # some LeRobot versions, so we apply x = x * std + mean ourselves.
        a_mean = getattr(mod, '_action_mean', None)
        a_std = getattr(mod, '_action_std', None)
        if a_mean is not None:
            action = action * a_std.to(device) + a_mean.to(device)
        else:
            # Fallback: try the policy method (may not work)
            out_dict = {"action": action}
            if hasattr(policy, "unnormalize_outputs"):
                out_dict = policy.unnormalize_outputs(out_dict)
            action = out_dict["action"]

    if device.startswith("cuda"):
        torch.cuda.synchronize()

    arr = action.detach().float().cpu().numpy()
    if arr.ndim == 3:
        arr = arr[0, 0]  # (1, T, dim) -> (dim,)
    elif arr.ndim == 2:
        arr = arr[0]  # (1, dim) -> (dim,)
    return arr.astype(np.float64).reshape(-1)


def main():
    parser = argparse.ArgumentParser(description="ACT inference in BlueThink simulation")
    parser.add_argument("--policy-path", required=True, help="Path to ACT policy checkpoint directory")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--scene", type=str, default="robot_only",
                        help="Scene override: robot_only (default), robot_nav, or path to MJCF/XML")
    parser.add_argument("--camera-w", type=int, default=640)
    parser.add_argument("--camera-h", type=int, default=480)
    parser.add_argument("--display-w", type=int, default=256)
    parser.add_argument("--display-h", type=int, default=192)
    parser.add_argument("--policy-fps", type=float, default=30.0)
    parser.add_argument("--physics-steps-per-step", type=int, default=10)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Max execution seconds. <=0 runs until Ctrl+C")
    parser.add_argument("--no-viewer", action="store_true")
    parser.add_argument("--no-init-move", action="store_true",
                        help="Skip moving robot to a neutral starting pose")
    parser.add_argument("--init-hold-steps", type=int, default=500,
                        help="Simulation steps to hold initial pose before inference")
    parser.add_argument("--data-path", type=str, default="",
                        help="LeRobot dataset root. If set, uses real images instead of sim cameras")
    parser.add_argument("--episode", type=int, default=0,
                        help="Episode index when using --data-path")
    args = parser.parse_args()

    print(f"Loading BlueThink scene...")
    gs_cfg = GSConfig(scene=args.scene, robot_name="BlueThink")
    env = SimEnv(gs_cfg.scene_xml, gs_cfg, enable_renderers=True)
    print(f"Model loaded: {env.model.num_links} links, {env.model.num_dof_pos} DOFs")
    print(f"Actuators: {env.model.num_actuators}")
    print(f"Cameras: {len(env.model.cameras)}")

    cam_name_to_id = build_camera_name_to_id(env)
    print(f"Camera mapping: {cam_name_to_id}")

    cam_ids = {}
    for name in CAMERA_NAMES:
        if name in cam_name_to_id:
            cam_ids[name] = cam_name_to_id[name]
            print(f"  {name} -> cam_id={cam_name_to_id[name]}")
        else:
            raise RuntimeError(f"Camera '{name}' not found in model. Available: {list(cam_name_to_id.keys())}")

    print(f"\nLoading ACT policy: {args.policy_path}")
    policy = load_act_policy(args.policy_path, device=args.device)
    print(f"Policy loaded. Device: {args.device}")

    qpos = get_qpos(env)
    print(f"\nInitial qpos ({len(qpos)} joints): {np.round(qpos, 3).tolist()}")

    if not args.no_init_move:
        set_qpos(env, INIT_QPOS)
        print(f"Moving to init pose...")
        for _ in range(args.init_hold_steps):
            env.step()
        env.forward_kinematic()
        qpos = get_qpos(env)
        print(f"Init qpos: {np.round(qpos, 3).tolist()}")

    # ---- Real dataset image mode ----
    if args.data_path:
        print(f"\n=== REAL IMAGE MODE: using dataset {args.data_path}, episode {args.episode} ===")
        gt_states, gt_actions, dataset_images, num_data_frames = load_dataset_episode(
            args.data_path, args.episode)

        try:
            from motrixsim.render import RenderApp
        except ImportError:
            RenderApp = None

        # Set sim state to match dataset's initial state
        init_state = gt_states[0]
        set_qpos(env, init_state)
        for _ in range(args.init_hold_steps):
            env.step()
        env.forward_kinematic()
        qpos = get_qpos(env)
        print(f"Dataset init state: {np.round(init_state, 3).tolist()}")
        print(f"Sim qpos after sync: {np.round(qpos, 3).tolist()}")

        inference_count = 0
        frame_idx = 0
        status_interval = 1.0
        last_status = 0.0
        _render_ref = [None]  # mutable ref for closure

        def dataset_loop():
            nonlocal last_status, inference_count, frame_idx
            dt = 1.0 / max(1e-3, float(args.policy_fps))
            next_infer = time.perf_counter()
            start_t = next_infer
            print(f"Starting dataset loop: {num_data_frames} frames at {args.policy_fps} fps")

            while frame_idx < num_data_frames:
                now = time.perf_counter()
                sleep_time = next_infer - now
                if sleep_time > 0:
                    time.sleep(min(0.005, sleep_time))
                    continue
                next_infer = max(now, next_infer) + dt

                env.forward_kinematic()
                qpos = get_qpos(env)

                # Use dataset images instead of sim rendering
                images: Dict[str, np.ndarray] = {}
                for policy_key in POLICY_IMAGE_KEYS:
                    if frame_idx < len(dataset_images.get(policy_key, [])):
                        images[policy_key] = dataset_images[policy_key][frame_idx]

                try:
                    action = step_policy(policy, qpos, images, device=args.device)
                    inference_count += 1
                except Exception as e:
                    import traceback
                    print(f"\nPolicy inference failed at frame {frame_idx}: {e}")
                    traceback.print_exc()
                    break

                action = np.asarray(action, dtype=np.float64).reshape(-1)

                # Compare with GT action
                gt_act = gt_actions[frame_idx] if frame_idx < len(gt_actions) else None

                set_qpos(env, action)
                for _ in range(args.physics_steps_per_step):
                    env.step()

                # Sync physics to renderer
                if _render_ref[0] is not None:
                    _render_ref[0].sync(env.data)

                t_now = time.perf_counter()
                if t_now - last_status >= status_interval:
                    diff = np.max(np.abs(action - qpos))
                    l_q = np.round(qpos[:7], 2)
                    r_q = np.round(qpos[7:], 2)
                    la = np.round(action[:7], 2)
                    ra = np.round(action[7:], 2)
                    extra = ""
                    if gt_act is not None:
                        gt_err = np.rad2deg(np.abs(action - gt_act))
                        extra = f" | gt_err={np.mean(gt_err):.1f} deg"
                    print(f"\n--- DATA ACT #{inference_count} frame={frame_idx}/{num_data_frames}"
                          f" | max dq={diff:.4f} rad{extra}")
                    print(f"  qpos L: {list(l_q)}")
                    print(f"  qpos R: {list(r_q)}")
                    print(f"  actn L: {list(la)}")
                    print(f"  actn R: {list(ra)}")
                    if gt_act is not None:
                        print(f"  gt_a L: {list(np.round(gt_act[:7], 2))}")
                        print(f"  gt_a R: {list(np.round(gt_act[7:], 2))}")
                    last_status = t_now

                frame_idx += 1

            print(f"\nDone. {inference_count} inferences over {frame_idx} frames.")

        if RenderApp is not None:
            with RenderApp() as render:
                render.launch(env.model)
                print("\nControls: ESC to exit")
                _render_ref[0] = render
                dataset_loop()
        else:
            print("RenderApp not available, running headless...")
            dataset_loop()

        return

    # ---- Normal sim camera mode ----
    try:
        from motrixsim.render import RenderApp
    except ImportError:
        RenderApp = None

    status_interval = 1.0
    last_status = 0.0
    inference_count = 0

    def inference_loop(env, policy, cam_ids, args, render=None, cam_images=None):
        nonlocal last_status, inference_count
        dt = 1.0 / max(1e-3, float(args.policy_fps))
        next_infer = time.perf_counter()
        start_t = next_infer

        while True:
            if render is not None and render.is_closed:
                break
            if args.duration > 0 and time.perf_counter() - start_t >= args.duration:
                print(f"\nDuration reached: {args.duration:.1f}s")
                break

            now = time.perf_counter()
            sleep_time = next_infer - now
            if sleep_time > 0:
                time.sleep(min(0.005, sleep_time))
                continue

            next_infer = max(now, next_infer) + dt

            env.forward_kinematic()
            qpos = get_qpos(env)

            images: Dict[str, np.ndarray] = {}
            for name in CAMERA_NAMES:
                frame = env.render_frame(
                    cam_id=cam_ids[name],
                    width=args.camera_w,
                    height=args.camera_h,
                )
                images[name] = np.asarray(frame)

            try:
                action = step_policy(policy, qpos, images, device=args.device)
                inference_count += 1
            except Exception as e:
                print(f"\nPolicy inference failed: {e}")
                break

            action = np.asarray(action, dtype=np.float64).reshape(-1)
            if action.size != NUM_JOINTS:
                print(f"\nWARNING: policy output dim={action.size}, expected {NUM_JOINTS}")
                if action.size > NUM_JOINTS:
                    action = action[:NUM_JOINTS]
                else:
                    break

            set_qpos(env, action)

            for _ in range(args.physics_steps_per_step):
                env.step()

            if cam_images:
                for name in CAMERA_NAMES:
                    if name in cam_images and name in images:
                        img = images[name]
                        if img.shape[0] != args.display_h or img.shape[1] != args.display_w:
                            import cv2
                            img = cv2.resize(img, (args.display_w, args.display_h))
                        cam_images[name].pixels = np.ascontiguousarray(img)

            if render is not None:
                render.sync(env.data)

            t_now = time.perf_counter()
            if t_now - last_status >= status_interval:
                diff = np.max(np.abs(action - qpos))
                l_q = np.round(qpos[:7], 2)
                r_q = np.round(qpos[7:], 2)
                la = np.round(action[:7], 2)
                ra = np.round(action[7:], 2)
                print(f"\n--- ACT #{inference_count} | max dq={diff:.4f} rad | "
                      f"latency={((t_now - now) * 1000):.0f}ms ---")
                print(f"  qpos L: {list(l_q)}")
                print(f"  qpos R: {list(r_q)}")
                print(f"  actn L: {list(la)}")
                print(f"  actn R: {list(ra)}")
                last_status = t_now

        return inference_count

    if RenderApp is None:
        print("RenderApp not available, running headless...")
        inference_loop(env, policy, cam_ids, args)
        print(f"\nDone. {inference_count} inferences.")
        return

    with RenderApp() as render:
        if not args.no_viewer:
            render.launch(env.model)
            print("\nControls: ESC to exit")

            cam_images: Dict[str, object] = {}
            specs = viewer_widget_layout_specs(CAMERA_NAMES, args.display_w, args.display_h, cols=3)
            for name, spec in zip(CAMERA_NAMES, specs):
                from motrixsim.render import Layout
                placeholder = np.zeros((args.display_h, args.display_w, 3), dtype=np.uint8)
                image = render.create_image(placeholder)
                render.widgets.create_image_widget(
                    image, layout=Layout(left=spec.left, top=spec.top, width=spec.width, height=spec.height))
                cam_images[name] = image
            print(f"Camera windows: {list(cam_images.keys())}")
        else:
            cam_images = None
            print("\nRunning inference (no viewer)")

        inference_loop(env, policy, cam_ids, args,
                       render=None if args.no_viewer else render,
                       cam_images=cam_images)

    print(f"\nDone. {inference_count} inferences.")


if __name__ == "__main__":
    main()
