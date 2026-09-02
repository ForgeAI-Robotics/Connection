"""pi05_server.py — PI0.5 policy inference as a standalone HTTP service.

Uses the self-contained pi05_policy.py loader (no dependency on the deploy
script's robot-control modules). Place this file AND pi05_policy.py in the
same directory, then point --policy-path / --base-model-path / --tokenizer-path
to the model files.

Usage (on the GPU server):
    python services/pi05_server.py \
        --policy-path /root/gpufree-data/49000/pretrained_model \
        --base-model-path /root/gpufree-data/pi05_base \
        --tokenizer-path /root/gpufree-data/paligemma_tokenizer \
        --port 5005 --device cuda
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys

import numpy as np
from PIL import Image
from flask import Flask, request, jsonify

# Ensure pi05_policy.py is importable (same directory as this script).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pi05_policy import (
    load_pi05_policy_and_processors,
    build_pi05_observation,
    action_to_vector,
    policy_metadata_source,
    get_policy_state_key,
    image_hwc_to_chw,
    TOTAL_DOF,
)

# Client keys (what serve_3dgs sends) -> PI0.5 policy observation image keys.
IMAGE_KEY_MAP = {
    "top": "observation.images.top",
    "left_wrist": "observation.images.left_wrist",
    "right_wrist": "observation.images.right_wrist",
}

_policy = None
_preprocessor = None
_postprocessor = None
_state_key = "observation.state"
_device = "cuda"
_default_task = "pick up the object"
_infer_count = 0
_last_error = None

app = Flask(__name__)


def _decode_image(b64_str: str) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(base64.b64decode(b64_str))), dtype=np.uint8)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "policy_loaded": _policy is not None})


@app.route("/info", methods=["GET"])
def info():
    return jsonify({
        "policy_loaded": _policy is not None,
        "state_key": _state_key,
        "image_keys": list(IMAGE_KEY_MAP.keys()),
        "state_dim": TOTAL_DOF,
        "action_dim": TOTAL_DOF,
        "infer_count": _infer_count,
    })


@app.route("/reset", methods=["POST"])
def reset():
    """Reset the policy's internal action queue (call at the start of each episode)."""
    global _infer_count
    if _policy is not None and hasattr(_policy, "reset"):
        _policy.reset()
    _infer_count = 0
    return jsonify({"success": True})


@app.route("/pi05_step", methods=["POST"])
def pi05_step():
    """Single PI0.5 inference step.

    Request body:
        state:   list[float] length 16 (14 arm joints + left/right gripper)
        images:  dict[str, str] base64 HWC images, keys in {top, left_wrist, right_wrist}
        task:    optional str (language task); falls back to server default
    Response:
        actions: list[float] length 16
        infer_count: int
    """
    global _infer_count, _last_error
    if _policy is None:
        return jsonify({"error": "policy not loaded"}), 503
    import torch

    data = request.json or {}
    state = data.get("state") or []
    if len(state) != TOTAL_DOF:
        return jsonify({"error": f"state must have {TOTAL_DOF} values, got {len(state)}"}), 400
    task = data.get("task") or _default_task
    images_b64 = data.get("images") or {}

    images = {}
    for client_key, b64 in images_b64.items():
        if client_key not in IMAGE_KEY_MAP:
            continue
        try:
            images[client_key] = _decode_image(b64)
        except Exception as e:
            return jsonify({"error": f"image decode failed for {client_key}: {e}"}), 400

    try:
        observation = build_pi05_observation(
            policy=_policy, state=state, images=images, task=task,
            state_key=_state_key, image_key_map=IMAGE_KEY_MAP, image_mode="float01")
        batch = _preprocessor(observation)
        with torch.inference_mode():
            action = _policy.select_action(batch)
            action = _postprocessor(action)
        if _device.startswith("cuda"):
            torch.cuda.synchronize()
        vec = action_to_vector(action, chunk_step_index=0)
        _infer_count += 1
        return jsonify({"actions": vec.tolist(), "infer_count": _infer_count})
    except Exception as e:
        _last_error = str(e)
        return jsonify({"error": str(e)}), 500


def main():
    global _policy, _preprocessor, _postprocessor, _state_key, _device, _default_task
    parser = argparse.ArgumentParser(description="PI0.5 policy inference HTTP service")
    parser.add_argument("--policy-path", required=True, help="PI0.5 LoRA checkpoint dir (pretrained_model)")
    parser.add_argument("--base-model-path", required=True, help="PI0.5 base model dir (for PEFT/LoRA)")
    parser.add_argument("--tokenizer-path", default="", help="tokenizer dir (defaults to --base-model-path)")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--n-action-steps", type=int, default=None)
    parser.add_argument("--dtype", default="float32",
                        choices=["bfloat16", "float16", "float32"],
                        help="Model dtype. float32 (default) avoids dtype mismatches on GPUs with enough VRAM.")
    parser.add_argument("--default-task", default="pick up the object and place it")
    args = parser.parse_args()

    if not args.tokenizer_path:
        args.tokenizer_path = args.base_model_path

    _device = args.device
    _default_task = args.default_task

    print(f"[pi05_server] Loading PI0.5: policy={args.policy_path} base={args.base_model_path} "
          f"tokenizer={args.tokenizer_path} dtype={args.dtype}", flush=True)
    _policy, _preprocessor, _postprocessor = load_pi05_policy_and_processors(
        args.policy_path, args.device,
        n_action_steps=args.n_action_steps,
        base_model_path=args.base_model_path,
        tokenizer_path=args.tokenizer_path,
        dtype=args.dtype,
    )
    meta = policy_metadata_source(_policy)
    _state_key = get_policy_state_key(meta)
    print(f"[pi05_server] Ready. state_key={_state_key} device={_device}", flush=True)

    print(f"[pi05_server] Listening on http://0.0.0.0:{args.port}", flush=True)
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
