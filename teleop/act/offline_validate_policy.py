"""
Offline validation: feed real dataset images into the ACT policy and compare
output actions against recorded ground-truth actions.

Usage:
    conda activate lerobot
    python teleop/act/offline_validate_policy.py \
        --policy-path /home/fangqi/scrips/outputs/train/act_clear_table/checkpoints/100000/pretrained_model \
        --data-path /home/fangqi/下载/dual_arm_lerobot_data \
        --episode 0 \
        --device cuda
"""

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

import av
import numpy as np
import pandas as pd
import torch


# ---------------------------------------------------------------------------
# Policy loading (same as sim_act_inference.py)
# ---------------------------------------------------------------------------

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


def load_policy(path, device):
    ACTPolicy = import_act_policy_class()
    try:
        policy = ACTPolicy.from_pretrained(path, device=device)
    except TypeError:
        policy = ACTPolicy.from_pretrained(path)
    policy.to(device)
    policy.eval()
    if hasattr(policy, "reset"):
        policy.reset()
    return policy


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_episode(data_path: str, episode_idx: int):
    """Load state/action parquet + video frames for one episode."""
    with open(os.path.join(data_path, "meta", "info.json")) as f:
        info = json.load(f)

    # Find which parquet file contains this episode
    ep_dir = os.path.join(data_path, "meta", "episodes", "chunk-000")
    ep_files = sorted([f for f in os.listdir(ep_dir) if f.endswith(".parquet")])
    ep_df = pd.read_parquet(os.path.join(ep_dir, ep_files[episode_idx]))

    from_idx = int(ep_df["dataset_from_index"].iloc[0])
    to_idx = int(ep_df["dataset_to_index"].iloc[0])
    length = int(ep_df["length"].iloc[0])

    # Find the right data chunk
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
            subset = df[mask].copy()
            if len(subset) > 0:
                subset = subset.sort_values("index").reset_index(drop=True)
                states = np.stack(subset["observation.state"].values)
                actions = np.stack(subset["action"].values)
                assert len(states) == length, f"Expected {length} frames, got {len(states)}"
                return states, actions, ep_df
    raise RuntimeError(f"Episode {episode_idx} not found")


def decode_video_frames(video_path: str, num_frames: int, target_w=640, target_h=480):
    """Decode video file and return frames as numpy arrays (HWC uint8)."""
    container = av.open(video_path)
    stream = container.streams.video[0]
    frames = []
    for i, packet in enumerate(container.demux(stream)):
        if i >= num_frames:
            break
        for frame in packet.decode():
            arr = frame.to_ndarray(format="rgb24")
            if arr.shape[0] != target_h or arr.shape[1] != target_w:
                import cv2
                arr = cv2.resize(arr, (target_w, target_h))
            frames.append(arr)
    container.close()
    return frames


def load_episode_images(data_path: str, ep_df, num_frames: int):
    """Load video frames for all 3 cameras for one episode."""
    camera_keys = [
        "observation.images.top",
        "observation.images.left_wrist",
        "observation.images.right_wrist",
    ]
    all_images = {k: [] for k in camera_keys}

    for key in camera_keys:
        ck = int(ep_df[f"videos/{key}/chunk_index"].iloc[0])
        fi = int(ep_df[f"videos/{key}/file_index"].iloc[0])
        video_path = os.path.join(
            data_path, "videos", key, f"chunk-{ck:03d}", f"file-{fi:03d}.mp4"
        )
        if os.path.exists(video_path):
            all_images[key] = decode_video_frames(video_path, num_frames)
        else:
            print(f"WARNING: video not found: {video_path}")
            for _ in range(num_frames):
                all_images[key].append(np.zeros((480, 640, 3), dtype=np.uint8))

    return all_images


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def image_to_tensor(img: np.ndarray, device: str) -> torch.Tensor:
    arr = np.ascontiguousarray(img)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    t = torch.from_numpy(arr).permute(2, 0, 1).float().div_(255.0)
    return t.unsqueeze(0).to(device=device, non_blocking=True)


def run_policy_on_episode(policy, states, images_dict, device, num_frames, action_mean=None, action_std=None):
    """Run policy on each frame and collect predicted actions."""
    all_pred = []

    for t in range(num_frames):
        state_t = torch.as_tensor(states[t], dtype=torch.float32, device=device).unsqueeze(0)
        batch = {"observation.state": state_t}

        key_map = {
            "observation.images.top": images_dict["observation.images.top"],
            "observation.images.left_wrist": images_dict["observation.images.left_wrist"],
            "observation.images.right_wrist": images_dict["observation.images.right_wrist"],
        }
        for policy_key, frames in key_map.items():
            if t < len(frames):
                batch[policy_key] = image_to_tensor(frames[t], device)

        if hasattr(policy, "normalize_inputs"):
            batch = policy.normalize_inputs(batch)
        elif hasattr(policy, "preprocessor"):
            batch = policy.preprocessor(batch)

        with torch.inference_mode():
            action = policy.select_action(batch)
            # Manual unnormalization (policy.unnormalize_outputs is a no-op in some LeRobot versions)
            if action_mean is not None and action_std is not None:
                action = action * action_std.to(device) + action_mean.to(device)

        if device.startswith("cuda"):
            torch.cuda.synchronize()

        arr = action.detach().float().cpu().numpy()
        if arr.ndim == 3:
            arr = arr[0, 0]
        elif arr.ndim == 2:
            arr = arr[0]
        all_pred.append(arr.astype(np.float64).reshape(-1))

    return np.array(all_pred)


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

def save_comparison_images(images_dict, states, gt_actions, pred_actions,
                           episode_idx, output_dir, num_frames, joint_names):
    """Save side-by-side comparison images for key timesteps."""
    os.makedirs(output_dir, exist_ok=True)

    # Save first frame + last frame + every 30 frames
    key_frames = sorted(set([0, num_frames - 1] + list(range(0, num_frames, 30))))
    key_frames = [f for f in key_frames if f < num_frames]

    camera_display = {
        "observation.images.top": "top_cam",
        "observation.images.left_wrist": "left_ee_cam",
        "observation.images.right_wrist": "right_ee_cam",
    }

    import cv2

    for t in key_frames:
        for key, label in camera_display.items():
            if t < len(images_dict[key]):
                img = images_dict[key][t].copy()
                # Add timestamp overlay
                cv2.putText(img, f"frame {t}/{num_frames}", (10, 30),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.putText(img, f"ep {episode_idx}", (10, 60),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
                out_path = os.path.join(output_dir, f"ep{episode_idx}_frame{t:04d}_{label}.png")
                cv2.imwrite(out_path, img)

    # Save action error plot over time
    errors_rad = np.abs(pred_actions - gt_actions)
    errors_deg = np.rad2deg(errors_rad)

    fig_path = os.path.join(output_dir, f"ep{episode_idx}_action_errors.png")
    _plot_action_comparison(errors_deg, joint_names, num_frames, fig_path)


def _plot_action_comparison(errors_deg, joint_names, num_frames, save_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n_joints = errors_deg.shape[1]
    fig, axes = plt.subplots(2, 1, figsize=(16, 10), sharex=True)

    # Per-joint error over time
    for j in range(n_joints):
        short = joint_names[j].replace("Left_", "L_").replace("Right_", "R_").replace("_Joint", "")
        color = f"C{j // 7}" if j < 14 else f"C{j}"
        ls = "-" if j < 7 else "--"
        axes[0].plot(errors_deg[:, j], label=short, color=color, ls=ls, alpha=0.8)

    axes[0].set_ylabel("Error (deg)")
    axes[0].set_title("Per-Joint Action Error: |pred - gt|")
    axes[0].legend(ncol=7, fontsize=7, loc="upper right")
    axes[0].grid(True, alpha=0.3)

    # Mean error per side
    mean_left = np.mean(errors_deg[:, :7], axis=1)
    mean_right = np.mean(errors_deg[:, 7:], axis=1)
    mean_all = np.mean(errors_deg, axis=1)
    axes[1].plot(mean_left, label="Left arm mean", color="C0", alpha=0.8)
    axes[1].plot(mean_right, label="Right arm mean", color="C1", alpha=0.8)
    axes[1].plot(mean_all, label="Overall mean", color="C2", lw=2)
    axes[1].set_ylabel("Mean Error (deg)")
    axes[1].set_xlabel("Frame")
    axes[1].set_title("Mean Joint Error Over Time")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    plt.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Offline ACT policy validation on real dataset")
    parser.add_argument("--policy-path", required=True)
    parser.add_argument("--data-path", required=True, help="LeRobot dataset root")
    parser.add_argument("--episode", type=int, default=0, help="Episode index to validate")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--output-dir", default="/tmp/act_validate_output",
                       help="Directory to save comparison images and plots")
    parser.add_argument("--save-every", type=int, default=1,
                       help="Save detailed per-frame output every N frames (1=all)")
    args = parser.parse_args()

    joint_names = [
        "Left_Shoulder_Pitch", "Left_Shoulder_Roll", "Left_Shoulder_Yaw",
        "Left_Elbow_Pitch", "Left_Wrist_Yaw", "Left_Wrist_Pitch", "Left_Wrist_Roll",
        "Right_Shoulder_Pitch", "Right_Shoulder_Roll", "Right_Shoulder_Yaw",
        "Right_Elbow_Pitch", "Right_Wrist_Yaw", "Right_Wrist_Pitch", "Right_Wrist_Roll",
    ]

    # Load policy
    print(f"Loading policy: {args.policy_path}")
    policy = load_policy(args.policy_path, args.device)
    print(f"Policy loaded on {args.device}")

    # Load action normalization stats for manual unnormalization
    from safetensors.torch import load_file as _load_sf
    import glob, os as _os
    st_files = glob.glob(_os.path.join(args.policy_path, "*unnormalizer*"))
    action_mean = action_std = None
    for sf in st_files:
        d = _load_sf(sf)
        if "action.mean" in d and "action.std" in d:
            action_mean = d["action.mean"].float()
            action_std = d["action.std"].float()
            break
    if action_mean is not None:
        print(f"Action norm stats loaded for manual unnormalize")
    else:
        print(f"WARNING: no action norm stats found, output will be in normalized space!")

    # Load episode data
    print(f"\nLoading episode {args.episode} from {args.data_path}")
    states, gt_actions, ep_df = load_episode(args.data_path, args.episode)
    num_frames = len(states)
    print(f"  Frames: {num_frames}")
    print(f"  State shape: {states.shape}")
    print(f"  Action shape: {gt_actions.shape}")

    # Load images
    print("Loading video frames...")
    images_dict = load_episode_images(args.data_path, ep_df, num_frames)
    for key, frames in images_dict.items():
        print(f"  {key}: {len(frames)} frames, shape={frames[0].shape if frames else 'N/A'}")

    # Run policy
    print(f"\nRunning policy inference on {num_frames} frames...")
    pred_actions = run_policy_on_episode(policy, states, images_dict, args.device, num_frames,
                                         action_mean=action_mean, action_std=action_std)
    print(f"Prediction shape: {pred_actions.shape}")

    # Compute errors
    errors_rad = pred_actions - gt_actions
    errors_deg = np.rad2deg(errors_rad)
    abs_errors_deg = np.abs(errors_deg)

    # Print summary
    print("\n" + "=" * 80)
    print(f"OFFLINE VALIDATION RESULTS - Episode {args.episode}")
    print("=" * 80)

    print(f"\n{'Joint':<30s} {'MeanErr(deg)':>12s} {'MaxErr(deg)':>12s} {'RMSE(rad)':>12s}")
    print("-" * 70)
    for j in range(14):
        mean_e = np.mean(abs_errors_deg[:, j])
        max_e = np.max(abs_errors_deg[:, j])
        rmse = np.sqrt(np.mean(errors_rad[:, j] ** 2))
        print(f"  {joint_names[j]:<28s} {mean_e:>12.3f} {max_e:>12.3f} {rmse:>12.5f}")

    mean_all = np.mean(abs_errors_deg)
    max_all = np.max(abs_errors_deg)
    rmse_all = np.sqrt(np.mean(errors_rad ** 2))
    print("-" * 70)
    print(f"  {'OVERALL':<28s} {mean_all:>12.3f} {max_all:>12.3f} {rmse_all:>12.5f}")

    left_mean = np.mean(abs_errors_deg[:, :7])
    right_mean = np.mean(abs_errors_deg[:, 7:])
    print(f"\n  Left arm mean error:  {left_mean:.3f} deg")
    print(f"  Right arm mean error: {right_mean:.3f} deg")

    # Per-frame summary at key points
    print(f"\nPer-frame summary (sampled):")
    step = max(1, num_frames // 20)
    print(f"  {'Frame':>6s} {'MeanErr':>8s} {'MaxErr':>8s} {'LeftMean':>9s} {'RightMean':>10s}")
    for t in range(0, num_frames, step):
        me = np.mean(abs_errors_deg[t])
        mx = np.max(abs_errors_deg[t])
        lm = np.mean(abs_errors_deg[t, :7])
        rm = np.mean(abs_errors_deg[t, 7:])
        print(f"  {t:>6d} {me:>8.3f} {mx:>8.3f} {lm:>9.3f} {rm:>10.3f}")

    # Judgment
    print("\n" + "=" * 80)
    if mean_all < 5.0:
        print("VERDICT: GOOD - policy reproduces training data with <5 deg mean error")
    elif mean_all < 15.0:
        print("VERDICT: ACCEPTABLE - policy has moderate error, may work in sim with tuning")
    elif mean_all < 30.0:
        print("VERDICT: POOR - large errors suggest domain gap or normalization issue")
    else:
        print("VERDICT: BAD - policy output barely matches training data, check model/data mismatch")
    print("=" * 80)

    # Save visualizations
    print(f"\nSaving comparison images to {args.output_dir}")
    save_comparison_images(images_dict, states, gt_actions, pred_actions,
                          args.episode, args.output_dir, num_frames, joint_names)

    # Save detailed CSV
    csv_path = os.path.join(args.output_dir, f"ep{args.episode}_detailed.csv")
    header = ",".join(["frame"] + joint_names + ["gt_" + n for n in joint_names] + ["abs_err_deg"])
    with open(csv_path, "w") as f:
        f.write(header + "\n")
        for t in range(num_frames):
            pred = pred_actions[t]
            gt = gt_actions[t]
            err = abs_errors_deg[t]
            row = [f"{t}"] + [f"{v:.6f}" for v in pred] + [f"{v:.6f}" for v in gt] + [f"{np.mean(err):.4f}"]
            f.write(",".join(row) + "\n")
    print(f"Detailed CSV saved: {csv_path}")

    # Save first frame images for visual inspection
    import cv2
    print("\nSaving first-frame camera images for visual inspection:")
    camera_labels = {
        "observation.images.top": "top_cam_frame0.png",
        "observation.images.left_wrist": "left_ee_cam_frame0.png",
        "observation.images.right_wrist": "right_ee_cam_frame0.png",
    }
    for key, fname in camera_labels.items():
        if images_dict[key]:
            path = os.path.join(args.output_dir, fname)
            cv2.imwrite(path, images_dict[key][0])
            print(f"  {fname}")


if __name__ == "__main__":
    main()
