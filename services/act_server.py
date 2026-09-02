"""ACT Policy Inference Service — standalone HTTP server.

Loads ACT model at startup, exposes single-step inference via HTTP.
The simulation server calls this service during ACT-based grasp commands.

Usage:
    # Policy inference mode
    python services/act_server.py --policy-path <ckpt> --port 5003

    # Replay mode (use dataset ground-truth actions, no policy needed)
    python services/act_server.py --policy-path <ckpt> --port 5003 --data-path <dataset> --episode 0
"""
from __future__ import annotations

import argparse
import base64
import importlib
import io
import json
import time
import sys
from pathlib import Path
from typing import Dict, Optional

import numpy as np
from PIL import Image
from flask import Flask, request, jsonify

# ── LeRobot ACT ──────────────────────────────────────────────────────
_policy = None
_device = "cuda"
_action_mean = None
_action_std = None

# ── Replay mode ──────────────────────────────────────────────────────
_replay_actions: Optional[np.ndarray] = None
_replay_step = 0


def _import_act_policy_class():
    for dotted in (
        "lerobot.policies.act.modeling_act.ACTPolicy",
        "lerobot.common.policies.act.modeling_act.ACTPolicy",
    ):
        mod, cls = dotted.rsplit(".", 1)
        try:
            module = importlib.import_module(mod)
            return getattr(module, cls)
        except Exception:
            continue
    raise ImportError("Cannot import ACTPolicy from lerobot")


def _load_norm_stats(policy_path: str):
    from safetensors.torch import load_file as _load
    import glob, os
    for f in glob.glob(os.path.join(policy_path, "*unnormalizer*")) or \
              glob.glob(os.path.join(policy_path, "*.safetensors")):
        d = _load(f)
        if "action.mean" in d and "action.std" in d:
            return d["action.mean"].float(), d["action.std"].float()
    return None, None


def load_policy(policy_path: str, device: str):
    global _policy, _device, _action_mean, _action_std
    ACTPolicy = _import_act_policy_class()
    try:
        policy = ACTPolicy.from_pretrained(policy_path, device=device)
    except TypeError:
        policy = ACTPolicy.from_pretrained(policy_path)
    policy.to(device)
    policy.eval()
    if hasattr(policy, "reset"):
        policy.reset()
    _action_mean, _action_std = _load_norm_stats(policy_path)
    _policy = policy
    _device = device
    print(f"[act_server] Policy loaded from {policy_path}, device={device}")


def load_dataset_episode(data_path: str, episode_idx: int):
    """Load ground-truth actions from a LeRobot dataset episode for replay mode."""
    global _replay_actions, _replay_step
    import os
    import pandas as pd

    # Try two common LeRobot dataset layouts:
    # Layout A: data/meta/episodes/chunk-000/episode_*.parquet (original LeRobot)
    # Layout B: data/chunk-000/file-*.parquet (flat, each file = one episode)
    found = False

    # Layout A
    ep_dir = os.path.join(data_path, "meta", "episodes", "chunk-000")
    if os.path.isdir(ep_dir):
        import json
        ep_files = sorted([f for f in os.listdir(ep_dir) if f.endswith(".parquet")])
        if episode_idx < len(ep_files):
            ep_df = pd.read_parquet(os.path.join(ep_dir, ep_files[episode_idx]))
            from_idx = int(ep_df["dataset_from_index"].iloc[0])
            to_idx = int(ep_df["dataset_to_index"].iloc[0])
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
                        _replay_actions = np.stack(subset["action"].values).astype(np.float64)
                        found = True
                        break
                else:
                    continue
                break

    # Layout B: flat parquet files with episode_index column
    if not found:
        chunk_dir = os.path.join(data_path, "chunk-000")
        if os.path.isdir(chunk_dir):
            for fi in range(10000):
                fp = os.path.join(chunk_dir, f"file-{fi:03d}.parquet")
                if not os.path.exists(fp):
                    break
                df = pd.read_parquet(fp)
                ep_id = int(df["episode_index"].iloc[0])
                if ep_id == episode_idx:
                    _replay_actions = np.stack(df["action"].values).astype(np.float64)
                    found = True
                    break

    if not found:
        raise RuntimeError(
            f"Episode {episode_idx} not found in {data_path}. "
            f"Tried layouts: meta/episodes/chunk-000 and chunk-000/file-*.parquet"
        )

    _replay_step = 0
    print(f"[act_server] Replay mode: episode {episode_idx}, {len(_replay_actions)} actions loaded")


def _decode_image(base64_str: str) -> np.ndarray:
    """Base64 image string → HWC uint8 numpy array."""
    data = base64.b64decode(base64_str)
    return np.array(Image.open(io.BytesIO(data)), dtype=np.uint8)


def _image_to_tensor(image: np.ndarray, device: str):
    import torch
    arr = np.ascontiguousarray(image, dtype=np.uint8)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)  # grayscale → RGB
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    t = torch.from_numpy(arr.copy()).permute(2, 0, 1).float().div_(255.0)
    return t.unsqueeze(0).to(device=device, non_blocking=True)


# ── Flask App ──────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "policy_loaded": _policy is not None})


@app.route("/load", methods=["POST"])
def api_load():
    data = request.json or {}
    policy_path = data.get("policy_path")
    device = data.get("device", "cuda")
    if not policy_path:
        return jsonify({"error": "missing policy_path"}), 400
    try:
        load_policy(policy_path, device)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/act_step", methods=["POST"])
def api_act_step():
    """Single ACT inference step (or replay step in dataset mode).

    Request body:
        state:   list[float] (length 14) — joint positions
        images: dict[str, str] — base64-encoded HWC images
            keys: "top", "left_wrist", "right_wrist"
    Response:
        actions: list[float] (length 14) — joint targets
    """
    global _replay_step

    # Replay mode: return dataset ground-truth action
    if _replay_actions is not None:
        if _replay_step >= len(_replay_actions):
            return jsonify({"error": "replay finished", "step": _replay_step}), 410
        action = _replay_actions[_replay_step].tolist()
        _replay_step += 1
        return jsonify({"actions": action, "replay_step": _replay_step})

    if _policy is None:
        return jsonify({"error": "policy not loaded"}), 503

    data = request.json or {}
    state = data.get("state", [])
    images_b64 = data.get("images", {})

    import torch

    # Build batch
    state_t = torch.as_tensor(state, dtype=torch.float32, device=_device).unsqueeze(0)
    batch = {"observation.state": state_t}

    image_key_map = {
        "top": "observation.images.top",
        "left_wrist": "observation.images.left_wrist",
        "right_wrist": "observation.images.right_wrist",
    }
    for client_key, policy_key in image_key_map.items():
        b64 = images_b64.get(client_key)
        if b64:
            img = _decode_image(b64)
            batch[policy_key] = _image_to_tensor(img, _device)

    # Normalize
    try:
        if hasattr(_policy, "normalize_inputs"):
            batch = _policy.normalize_inputs(batch)
        elif hasattr(_policy, "preprocessor"):
            batch = _policy.preprocessor(batch)
    except Exception:
        pass  # some policies don't support it

    # Inference
    with torch.inference_mode():
        action = _policy.select_action(batch)
        if _action_mean is not None:
            action = action * _action_std.to(_device) + _action_mean.to(_device)
        else:
            out_dict = {"action": action}
            if hasattr(_policy, "unnormalize_outputs"):
                out_dict = _policy.unnormalize_outputs(out_dict)
            action = out_dict["action"]

    if _device.startswith("cuda"):
        torch.cuda.synchronize()

    arr = action.detach().float().cpu().numpy()
    if arr.ndim == 3:
        arr = arr[0, 0]  # (1, T, dim) → (dim,)
    elif arr.ndim == 2:
        arr = arr[0]  # (1, dim) → (dim,)

    return jsonify({"actions": arr.tolist()})


@app.route("/info", methods=["GET"])
def api_info():
    return jsonify({
        "policy_loaded": _policy is not None,
        "device": _device,
        "state_dim": 14,
        "action_dim": 14,
        "image_keys": ["top", "left_wrist", "right_wrist"],
        "replay_mode": _replay_actions is not None,
        "replay_total": len(_replay_actions) if _replay_actions is not None else 0,
        "replay_step": _replay_step if _replay_actions is not None else 0,
    })


@app.route("/reset", methods=["POST"])
def api_reset():
    """Reset replay step counter to 0."""
    global _replay_step
    _replay_step = 0
    return jsonify({"success": True, "replay_step": 0})


def main():
    global _device
    parser = argparse.ArgumentParser(description="ACT Policy Inference Service")
    parser.add_argument("--policy-path", required=True, help="Path to ACT checkpoint dir")
    parser.add_argument("--device", default="cuda", help="cuda or cpu")
    parser.add_argument("--port", type=int, default=5003, help="HTTP listen port")
    parser.add_argument("--data-path", type=str, default="",
                        help="LeRobot dataset root for replay mode")
    parser.add_argument("--episode", type=int, default=0,
                        help="Episode index for replay mode (requires --data-path)")
    args = parser.parse_args()

    _device = args.device
    print(f"[act_server] Loading policy from {args.policy_path} on {args.device}...")
    load_policy(args.policy_path, args.device)

    if args.data_path:
        print(f"[act_server] Loading dataset replay: {args.data_path}, episode {args.episode}")
        load_dataset_episode(args.data_path, args.episode)

    print(f"[act_server] Listening on http://0.0.0.0:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
