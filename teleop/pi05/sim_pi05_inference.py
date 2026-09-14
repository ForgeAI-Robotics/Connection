"""sim_pi05_inference.py — PI0.5 policy inference in MotrixSim (3DGS) for BlueThink.

Mirror of teleop/act/sim_act_inference.py, but for the LeRobot PI0.5 policy:
  - 16-dim state/action = 14 arm joints + left/right gripper (dataset contract).
  - The sim robot has 14 actuators (no grippers), so the gripper dims are
    padded in the state (tracked from the policy's last commanded gripper value)
    and the gripper part of the action is dropped on apply.
  - 224x224 images (PI0.5 image_resolution); language `task` conditioning.
  - action chunking handled internally by policy.select_action.

Two modes:

  **Local mode** (policy loaded in-process):
    python teleop/pi05/sim_pi05_inference.py \
        --policy-path /data/FQIntern/49000/pretrained_model \
        --base-model-path /data/FQIntern/pi05_base \
        --tokenizer-path /data/FQIntern/pi05_base \
        --task "pick up the object" --device cuda

  **Dataset replay mode** (remote pi05_server, no local model needed):
    python teleop/pi05/sim_pi05_inference.py \
        --data-path /data/FQIntern/dataset --episode 0 \
        --pi05-url http://127.0.0.1:5005 \
        --scene robot_only
"""
from __future__ import annotations

import argparse
import base64
import io
import sys
import time
from pathlib import Path
from typing import Dict

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from serve_3dgs.backend.gs_config import GSConfig
from serve_3dgs.backend.sim_env import SimEnv

# Ensure torch can find ninja for any JIT C++ extension (composite mesh, etc.).
try:
    import ninja as _ninja
    import os
    _nb = getattr(_ninja, "BIN_DIR", None) or os.path.dirname(_ninja.__file__)
    if _nb and os.path.isdir(_nb) and _nb not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _nb + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

from pi05_policy import (
    load_pi05_policy_and_processors,
    build_pi05_observation,
    action_to_vector,
    policy_metadata_source,
    get_policy_state_key,
    get_policy_action_dim,
    ARM_DOF,
    TOTAL_DOF,
)

BLUETHINK_LEFT_JOINTS = [
    "Left_Shoulder_Pitch_Joint", "Left_Shoulder_Roll_Joint", "Left_Shoulder_Yaw_Joint",
    "Left_Elbow_Pitch_Joint", "Left_Wrist_Yaw_Joint", "Left_Wrist_Pitch_Joint", "Left_Wrist_Roll_Joint",
]
BLUETHINK_RIGHT_JOINTS = [
    "Right_Shoulder_Pitch_Joint", "Right_Shoulder_Roll_Joint", "Right_Shoulder_Yaw_Joint",
    "Right_Elbow_Pitch_Joint", "Right_Wrist_Yaw_Joint", "Right_Wrist_Pitch_Joint", "Right_Wrist_Roll_Joint",
]
BLUETHINK_JOINTS = BLUETHINK_LEFT_JOINTS + BLUETHINK_RIGHT_JOINTS
NUM_JOINTS = len(BLUETHINK_JOINTS)  # 14

# Sim camera names -> PI0.5 training image keys.
CAMERA_NAMES = ["base_cam", "left_ee_cam", "right_ee_cam"]
IMAGE_KEY_MAP = {
    "base_cam": "observation.images.top",
    "left_ee_cam": "observation.images.left_wrist",
    "right_ee_cam": "observation.images.right_wrist",
}
# HTTP client keys (what pi05_server expects from the client).
CAMERA_CLIENT_KEYS = ["top", "left_wrist", "right_wrist"]

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
    dof_pos = np.asarray(env.data.dof_pos, dtype=np.float64).reshape(-1)
    if dof_pos.size >= NUM_JOINTS:
        return dof_pos[:NUM_JOINTS].copy()
    return dof_pos.copy()


def set_qpos(env, qpos: np.ndarray):
    for i, name in enumerate(BLUETHINK_JOINTS):
        act = _find_actuator(env, name)
        if act is not None:
            act.set_ctrl(env.data, float(qpos[i]))


def build_camera_name_to_id(env) -> Dict[str, int]:
    name_to_id: Dict[str, int] = {}
    for idx in range(len(env.model.cameras)):
        cam = env.model.cameras[idx]
        name = getattr(cam, "name", "")
        if name:
            name_to_id[name] = idx
    return name_to_id


def render_cameras(env, cam_ids: Dict[str, int], w: int, h: int) -> Dict[str, np.ndarray]:
    images = {}
    for name in CAMERA_NAMES:
        cid = cam_ids.get(name)
        if cid is None:
            continue
        frame = env.render_frame(cam_id=cid, width=w, height=h)
        images[name] = np.asarray(frame, dtype=np.uint8)
    return images


# ── Dataset loading helpers (mirrors teleop/act/sim_act_inference.py) ──

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
    """Load one episode: (state_array, action_array, images_dict, task_text, num_frames).
    Images are keyed by dataset feature names (observation.images.top, etc.)."""
    import json, os, pandas as pd

    with open(os.path.join(data_path, "meta", "info.json")) as f:
        info = json.load(f)

    ep_dir = os.path.join(data_path, "meta", "episodes", "chunk-000")
    ep_files = sorted([f for f in os.listdir(ep_dir) if f.endswith(".parquet")])
    ep_df = pd.read_parquet(os.path.join(ep_dir, ep_files[episode_idx]))

    from_idx = int(ep_df["dataset_from_index"].iloc[0])
    to_idx = int(ep_df["dataset_to_index"].iloc[0])
    length = int(ep_df["length"].iloc[0])

    # task text
    task_idx = None
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
                task_idx = int(subset["task_index"].iloc[0])
                break
        else:
            continue
        break
    else:
        raise RuntimeError(f"Episode {episode_idx} data not found")

    task_text = "pick up the object"
    tasks_path = os.path.join(data_path, "meta", "tasks.parquet")
    if os.path.exists(tasks_path) and task_idx is not None:
        tasks_df = pd.read_parquet(tasks_path)
        if task_idx < len(tasks_df):
            task_text = str(tasks_df.index[task_idx])

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

    return states, actions, images, task_text, length


def _run_dataset_replay(args):
    """Run dataset replay mode: use dataset images + remote pi05_server for inference."""
    import json
    import requests

    print(f"\n=== DATASET REPLAY MODE ===")
    print(f"  dataset: {args.data_path}")
    print(f"  episode: {args.episode}")
    print(f"  pi05_server: {args.pi05_url}")

    # load dataset
    print("Loading dataset episode...")
    gt_states, gt_actions, dataset_images, task_text, num_frames = \
        load_dataset_episode(args.data_path, args.episode)
    print(f"  Task: {task_text}")
    print(f"  Frames: {num_frames}, state dim: {gt_states.shape[1]}, action dim: {gt_actions.shape[1]}")

    # build image key mapping: dataset feature names -> sim camera names
    dataset_to_sim = {
        "observation.images.top": "base_cam",
        "observation.images.left_wrist": "left_ee_cam",
        "observation.images.right_wrist": "right_ee_cam",
    }

    # load sim scene
    print("Loading BlueThink scene...")
    gs_cfg = GSConfig(scene=args.scene, robot_name="BlueThink")
    env = SimEnv(gs_cfg.scene_xml, gs_cfg, enable_renderers=True)
    print(f"Model loaded: {env.model.num_links} links, {env.model.num_dof_pos} DOFs, {env.model.num_actuators} actuators")

    # set initial state (only first 14 joints, grippers are virtual)
    init_state = gt_states[0]
    set_qpos(env, init_state[:NUM_JOINTS])
    print(f"Moving to dataset init pose...")
    for _ in range(args.init_hold_steps):
        env.step()
    env.forward_kinematic()
    print(f"Init qpos: {np.round(get_qpos(env), 3).tolist()}")

    def inference_loop(render=None):
        dt = 1.0 / max(1e-3, float(args.policy_fps))
        next_infer = time.perf_counter()
        count = 0
        gripper_state = np.array([1.0, 1.0], dtype=np.float64)

        for frame_idx in range(num_frames):
            if render is not None and render.is_closed:
                break
            now = time.perf_counter()
            sleep_t = next_infer - now
            if sleep_t > 0:
                time.sleep(min(0.005, sleep_t))
            next_infer = max(now, next_infer) + dt

            # sync sim state with dataset GT before inference
            if frame_idx < len(gt_states):
                set_qpos(env, gt_states[frame_idx][:NUM_JOINTS])
                for _ in range(args.physics_steps_per_step):
                    env.step()
            env.forward_kinematic()
            joints = get_qpos(env)

            # build images from dataset (not sim cameras)
            images_for_server = {}
            for dataset_key, sim_key in dataset_to_sim.items():
                frames = dataset_images.get(dataset_key, [])
                if frame_idx < len(frames):
                    images_for_server[sim_key] = frames[frame_idx]

            state16 = np.concatenate([joints, gripper_state]).tolist()

            # call remote pi05_server
            try:
                images_b64 = {}
                for sim_key, img in images_for_server.items():
                    # map sim key -> HTTP client key (top/left_wrist/right_wrist)
                    client_key = CAMERA_CLIENT_KEYS[CAMERA_NAMES.index(sim_key)]
                    pil_img = Image.fromarray(img)
                    buf = io.BytesIO()
                    pil_img.save(buf, format="JPEG", quality=90)
                    images_b64[client_key] = base64.b64encode(buf.getvalue()).decode("ascii")

                resp = requests.post(
                    f"{args.pi05_url}/pi05_step",
                    json={"state": state16, "images": images_b64, "task": task_text},
                    timeout=10.0,
                )
                resp.raise_for_status()
                vec = np.array(resp.json()["actions"], dtype=np.float64)
            except Exception as e:
                print(f"\n[{frame_idx}] pi05_server error: {e}")
                vec = np.zeros(TOTAL_DOF)

            if vec.size != TOTAL_DOF:
                print(f"WARNING: action dim={vec.size} != {TOTAL_DOF}; skipping")
                continue
            if not np.all(np.isfinite(vec)):
                print("WARNING: NaN/Inf action; holding")
                continue

            # compare with GT
            gt_act = gt_actions[frame_idx] if frame_idx < len(gt_actions) else None
            gt_err = np.rad2deg(np.abs(vec[:NUM_JOINTS] - gt_act[:NUM_JOINTS])) if gt_act is not None else None

            # apply action (only joints, not grippers)
            set_qpos(env, vec[:ARM_DOF])
            gripper_state = np.clip(vec[ARM_DOF:TOTAL_DOF], 0.0, 1.0)
            for _ in range(args.physics_steps_per_step):
                env.step()
            if render is not None:
                render.sync(env.data)

            count += 1
            if count % 10 == 0:
                extra = f" gt_err={np.mean(gt_err):.1f}°" if gt_err is not None else ""
                print(f"--- PI0.5 #{count}/{num_frames} | qpos[:3]={np.round(joints[:3],2).tolist()} "
                      f"act[:3]={np.round(vec[:3],2).tolist()} grip={np.round(gripper_state,2).tolist()}{extra}",
                      flush=True)

        print(f"\nDone. {count} frames processed.")
        return count

    try:
        from motrixsim.render import RenderApp
    except ImportError:
        RenderApp = None

    if RenderApp is None or args.no_viewer:
        print("Running headless (Ctrl+C to stop)..." if RenderApp is None else "Running (no viewer)...")
        inference_loop()
        return

    with RenderApp() as render:
        render.launch(env.model)
        print("Controls: ESC to exit")
        inference_loop(render=render)
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="PI0.5 inference in BlueThink MotrixSim")
    # --- mode selection ---
    parser.add_argument("--data-path", default="",
                        help="LeRobot dataset root. When set, uses dataset images + remote "
                             "pi05_server instead of loading policy locally.")
    parser.add_argument("--episode", type=int, default=0,
                        help="Episode index for --data-path mode")
    parser.add_argument("--pi05-url", default="http://127.0.0.1:5005",
                        help="PI0.5 server URL for --data-path mode (default: localhost via SSH tunnel)")
    # --- local mode args (ignored when --data-path is set) ---
    parser.add_argument("--policy-path", default="",
                        help="PI0.5 LoRA checkpoint dir (pretrained_model)")
    parser.add_argument("--base-model-path", default="",
                        help="PI0.5 base model dir (for PEFT/LoRA)")
    parser.add_argument("--tokenizer-path", default="", help="tokenizer dir (defaults to --base-model-path)")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--task", default="pick up the object and place it",
                        help="language task string (PI0.5 is language-conditioned)")
    parser.add_argument("--scene", default="robot_only",
                        help="robot_only (default), robot_nav, or MJCF/XML path")
    parser.add_argument("--policy-fps", type=float, default=30.0)
    parser.add_argument("--physics-steps-per-step", type=int, default=10)
    parser.add_argument("--camera-w", type=int, default=224, help="PI0.5 expects 224x224")
    parser.add_argument("--camera-h", type=int, default=224)
    parser.add_argument("--n-action-steps", type=int, default=None,
                        help="override checkpoint n_action_steps (<= chunk_size)")
    parser.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"],
                        help="load dtype (8GB GPUs must use bfloat16/float16)")
    parser.add_argument("--duration", type=float, default=0.0, help="<=0 runs until viewer closed / Ctrl+C")
    parser.add_argument("--no-viewer", action="store_true")
    parser.add_argument("--no-init-move", action="store_true")
    parser.add_argument("--init-hold-steps", type=int, default=300)
    args = parser.parse_args()

    if not args.tokenizer_path:
        args.tokenizer_path = args.base_model_path  # tokenizer often ships inside the base repo

    # ---- Dataset replay mode (remote pi05_server, no local model) ----
    if args.data_path:
        _run_dataset_replay(args)
        return

    # ---- Local inference mode (load policy in-process) ----
    print("Loading BlueThink scene...")
    gs_cfg = GSConfig(scene=args.scene, robot_name="BlueThink")
    env = SimEnv(gs_cfg.scene_xml, gs_cfg, enable_renderers=True)
    print(f"Model loaded: {env.model.num_links} links, {env.model.num_dof_pos} DOFs, {env.model.num_actuators} actuators")

    cam_ids = build_camera_name_to_id(env)
    for name in CAMERA_NAMES:
        if name not in cam_ids:
            raise RuntimeError(f"Camera '{name}' not found. Available: {list(cam_ids.keys())}")
        print(f"  {name} -> cam_id={cam_ids[name]}")

    print(f"\nLoading PI0.5 policy: {args.policy_path}")
    print(f"  base={args.base_model_path} tokenizer={args.tokenizer_path}")
    policy, preprocessor, postprocessor = load_pi05_policy_and_processors(
        args.policy_path, args.device,
        n_action_steps=args.n_action_steps,
        base_model_path=args.base_model_path,
        tokenizer_path=args.tokenizer_path,
        dtype=args.dtype,
    )
    meta = policy_metadata_source(policy)
    state_key = get_policy_state_key(meta)
    action_dim = get_policy_action_dim(meta)
    print(f"Policy loaded. state_key={state_key} action_dim={action_dim} device={args.device}")

    if not args.no_init_move:
        set_qpos(env, INIT_QPOS)
        print("Moving to init pose...")
        for _ in range(args.init_hold_steps):
            env.step()
        env.forward_kinematic()
        print(f"Init qpos: {np.round(get_qpos(env), 3).tolist()}")

    # Gripper state tracker: sim has no grippers, so we feed the policy its own last
    # commanded gripper value (open at start). This keeps state feedback self-consistent.
    gripper_state = np.array([1.0, 1.0], dtype=np.float64)

    def inference_loop(render=None):
        dt = 1.0 / max(1e-3, float(args.policy_fps))
        next_infer = time.perf_counter()
        start_t = next_infer
        count = 0
        import torch
        while True:
            if render is not None and render.is_closed:
                break
            if args.duration > 0 and time.perf_counter() - start_t >= args.duration:
                print(f"Duration reached: {args.duration:.1f}s")
                break
            now = time.perf_counter()
            sleep_t = next_infer - now
            if sleep_t > 0:
                time.sleep(min(0.005, sleep_t))
                continue
            next_infer = max(now, next_infer) + dt

            env.forward_kinematic()
            joints = get_qpos(env)
            state16 = np.concatenate([joints, gripper_state]).tolist()
            images = render_cameras(env, cam_ids, args.camera_w, args.camera_h)

            observation = build_pi05_observation(
                policy=policy, state=state16, images=images, task=args.task,
                state_key=state_key, image_key_map=IMAGE_KEY_MAP, image_mode="float01",
            )
            batch = preprocessor(observation)
            with torch.inference_mode():
                action = policy.select_action(batch)
                action = postprocessor(action)
            if args.device.startswith("cuda"):
                torch.cuda.synchronize()
            vec = action_to_vector(action, chunk_step_index=0)
            if vec.size != TOTAL_DOF:
                print(f"WARNING: action dim={vec.size} != {TOTAL_DOF}; skipping")
                continue
            if not np.all(np.isfinite(vec)):
                print("WARNING: NaN/Inf action; holding")
                continue

            set_qpos(env, vec[:ARM_DOF])
            gripper_state = np.clip(vec[ARM_DOF:TOTAL_DOF], 0.0, 1.0)
            for _ in range(args.physics_steps_per_step):
                env.step()
            if render is not None:
                render.sync(env.data)

            count += 1
            if count % 10 == 0:
                print(f"--- PI0.5 #{count} | qpos[:3]={np.round(joints[:3],2).tolist()} "
                      f"act[:3]={np.round(vec[:3],2).tolist()} grip={np.round(gripper_state,2).tolist()}", flush=True)
        return count

    try:
        from motrixsim.render import RenderApp
    except ImportError:
        RenderApp = None

    if RenderApp is None or args.no_viewer:
        print("Running headless (Ctrl+C to stop)..." if RenderApp is None else "Running inference (no viewer)...")
        inference_loop()
        return

    with RenderApp() as render:
        render.launch(env.model)
        print("Controls: ESC to exit")
        inference_loop(render=render)
    print("Done.")


if __name__ == "__main__":
    main()
