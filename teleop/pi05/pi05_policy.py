"""pi05_policy.py — self-contained LeRobot PI0.5 policy loader + observation/action helpers.

The PEFT/chunk-size/processor loading route is ported from the project's deploy
reference (dual_arm_pi05_deploy_ready.py). The few helpers that the deploy script
imported from sibling robot-control modules (get_policy_image_keys, parse_json_map,
ensure_legacy_pi05_processor_registry_aliases, ...) are reimplemented here so this
module has no external dependency beyond `lerobot` / `torch` / `numpy`.

Dataset / checkpoint contract (matches the dual-arm training data):
    observation.state = 14 arm joints + left gripper + right gripper  (TOTAL_DOF = 16)
    action            = 14 arm joints + left gripper + right gripper
    cameras           = observation.images.{top, left_wrist, right_wrist}, 224x224
Gripper values are normalized openings: 0=closed, 1=fully open.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Optional, Tuple

import numpy as np
import torch

ARM_DOF = 14
GRIPPER_DOF = 2
TOTAL_DOF = ARM_DOF + GRIPPER_DOF  # 16

# Known training-time image keys (dataset meta). Used as a fallback when the policy
# config does not list camera_keys explicitly.
DEFAULT_IMAGE_KEYS = [
    "observation.images.top",
    "observation.images.left_wrist",
    "observation.images.right_wrist",
]

_TEMP_DEPLOY_CHECKPOINTS: list = []


# ── metadata helpers (reimplemented; deploy imported these from a sibling module) ──

def policy_metadata_source(policy: Any) -> Any:
    """Return the underlying policy when `policy` is a PEFT wrapper."""
    get_base_model = getattr(policy, "get_base_model", None)
    return get_base_model() if callable(get_base_model) else policy


def parse_json_map(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        return json.loads(value)
    return {}


def get_policy_state_key(metadata_policy: Any) -> str:
    """State observation key. LeRobot dual-arm policy uses observation.state."""
    cfg = getattr(metadata_policy, "config", None) or metadata_policy
    input_shapes = getattr(cfg, "input_shapes", None) or {}
    for key in input_shapes:
        if key == "observation.state" or key.endswith(".state"):
            return key
    return "observation.state"


def get_policy_action_dim(metadata_policy: Any) -> Optional[int]:
    cfg = getattr(metadata_policy, "config", None) or metadata_policy
    output_shapes = getattr(cfg, "output_shapes", None) or {}
    action_shape = output_shapes.get("action")
    if action_shape:
        try:
            return int(action_shape[-1])
        except Exception:
            pass
    return None


def get_policy_image_keys(metadata_policy: Any) -> list:
    """Camera observation keys the policy expects."""
    cfg = getattr(metadata_policy, "config", None) or metadata_policy
    keys = getattr(cfg, "camera_keys", None)
    if keys:
        return list(keys)
    input_shapes = getattr(cfg, "input_shapes", None) or {}
    found = [k for k in input_shapes if k.startswith("observation.images.")]
    return found or list(DEFAULT_IMAGE_KEYS)


def ensure_legacy_pi05_processor_registry_aliases(policy_path: str = "") -> None:
    """Register processor steps missing in lerobot 0.5.1 but required by newer checkpoints.

    make_pre_post_processors injects steps from both the saved JSON AND built-in
    defaults (PI05Config). Some of those defaults (e.g. relative_actions_processor)
    don't exist in lerobot 0.5.1's registry. We hardcode the known missing ones
    and also scan the checkpoint JSONs as a safety net.

    Uses direct registry dict insertion (the decorator API silently fails in
    some Python 3.12 environments).
    """
    try:
        import lerobot.processor  # noqa: F401
    except Exception:
        pass
    try:
        from lerobot.processor.pipeline import ProcessorStepRegistry, ProcessorStep

        # Steps that lerobot 0.5.2+ registers but 0.5.1 does not, yet
        # make_pre_post_processors injects from PI05Config defaults.
        _KNOWN_MISSING = [
            "relative_actions_processor",
            "pi05_prepare_state_tokenizer_processor_step",
        ]
        known = set(ProcessorStepRegistry._registry.keys())
        for name in _KNOWN_MISSING:
            if name not in known:
                stub = type(
                    f"_Passthrough_{name}",
                    (ProcessorStep,),
                    {"__init__": lambda self, **kw: None, "__call__": lambda self, d: d, "_registry_name": name},
                )
                ProcessorStepRegistry._registry[name] = stub
                known.add(name)
                print(f"[pi05_policy] registered pass-through processor: {name}", flush=True)

        # Safety net: scan checkpoint JSONs for any other missing steps.
        if policy_path:
            for filename in ("policy_preprocessor.json", "policy_postprocessor.json"):
                fpath = os.path.join(policy_path, filename)
                if not os.path.isfile(fpath):
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    steps = data.get("steps", data) if isinstance(data, dict) else data
                    if not isinstance(steps, list):
                        continue
                    for s in steps:
                        if not isinstance(s, dict):
                            continue
                        name = s.get("registry_name")
                        if name and name not in known:
                            stub = type(
                                f"_Passthrough_{name}",
                                (ProcessorStep,),
                                {"__init__": lambda self, **kw: None, "__call__": lambda self, d: d, "_registry_name": name},
                            )
                            ProcessorStepRegistry._registry[name] = stub
                            known.add(name)
                            print(f"[pi05_policy] registered pass-through processor: {name}", flush=True)
                except Exception:
                    pass
    except Exception as e:
        print(f"[pi05_policy] warn: ensure_legacy: {e}", flush=True)


def _strip_unregistered_processor_steps(policy_path: str) -> None:
    """Remove processor steps from checkpoint JSONs whose registry_name is not
    registered in the current lerobot version.  Steps with `enabled: false` (the
    common case for these forward-compat entries) are pure no-ops and safe to drop."""
    try:
        from lerobot.processor.pipeline import ProcessorStepRegistry
    except Exception:
        return
    known = set(ProcessorStepRegistry._registry.keys())
    for filename in ("policy_preprocessor.json", "policy_postprocessor.json"):
        fpath = os.path.join(policy_path, filename)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            steps = data.get("steps", data) if isinstance(data, dict) else data
            if not isinstance(steps, list):
                continue
            original_len = len(steps)
            steps = [s for s in steps if not isinstance(s, dict)
                     or s.get("registry_name") in known
                     or "registry_name" not in s]
            if len(steps) < original_len:
                removed = original_len - len(steps)
                print(f"[pi05_policy] stripped {removed} unregistered processor step(s) from {filename}", flush=True)
                if isinstance(data, dict):
                    data["steps"] = steps
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.write("\n")
        except Exception as e:
            print(f"[pi05_policy] warn: _strip_unregistered_processor_steps({filename}): {e}", flush=True)


# ── checkpoint path / tokenizer patching (ported verbatim from deploy) ──

def _override_saved_tokenizer_path(policy_path: str, tokenizer_path: str) -> None:
    """Patch checkpoint processor JSON to use the local tokenizer path (mirrors deploy)."""
    tokenizer_path = str(tokenizer_path).strip()
    if not tokenizer_path:
        return
    tokenizer_dir = Path(tokenizer_path).expanduser().resolve()
    if not tokenizer_dir.is_dir():
        raise FileNotFoundError(f"tokenizer_path does not exist: {tokenizer_dir}")
    markers = ("tokenizer.json", "tokenizer.model", "tokenizer_config.json", "spiece.model")
    if not any((tokenizer_dir / name).is_file() for name in markers):
        raise FileNotFoundError(
            f"tokenizer_path is incomplete: {tokenizer_dir}. "
            f"Expected at least one of: {', '.join(markers)}")
    for filename in ("policy_preprocessor.json", "policy_postprocessor.json"):
        processor_path = Path(policy_path) / filename
        if not processor_path.exists():
            continue
        original_text = processor_path.read_text(encoding="utf-8")
        data = json.loads(original_text)
        steps = data.get("steps", []) if isinstance(data, dict) else data
        if not isinstance(steps, list):
            continue
        changed = False
        for step in steps:
            if not isinstance(step, dict):
                continue
            config = step.get("config")
            if isinstance(config, dict) and "tokenizer_name" in config:
                if config["tokenizer_name"] != str(tokenizer_dir):
                    config["tokenizer_name"] = str(tokenizer_dir)
                    changed = True
        if not changed:
            continue
        backup_path = processor_path.with_suffix(processor_path.suffix + ".tokenizer.bak")
        if not backup_path.exists():
            backup_path.write_text(original_text, encoding="utf-8")
        processor_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Updated processor tokenizer path: {processor_path} -> {tokenizer_dir}")


def resolve_local_pi05_policy_path(policy_path: str) -> str:
    """Resolve common local checkpoint layouts before falling back to HF-style IDs."""
    raw_path = Path(policy_path).expanduser()
    candidates = [raw_path]
    if raw_path.name == "pretrained_model":
        candidates.append(raw_path.parent)
    else:
        candidates.append(raw_path / "pretrained_model")
    tried: list[Path] = []
    for candidate in candidates:
        if candidate in tried:
            continue
        tried.append(candidate)
        if (candidate / "config.json").is_file():
            resolved = str(candidate.resolve())
            if candidate != raw_path:
                print(f"Resolved PI0.5 checkpoint path: {raw_path} -> {resolved}")
            return resolved
    if raw_path.is_absolute() or raw_path.exists() or str(policy_path).startswith("."):
        raise FileNotFoundError(f"PI0.5 checkpoint config.json not found. Tried: {tried}")
    return policy_path


def make_pi05_deploy_compatible_policy_path(policy_path: str, valid_config_keys: set) -> str:
    """Create a temporary checkpoint view with training-only config fields removed."""
    resolved_policy_path = resolve_local_pi05_policy_path(policy_path)
    source_dir = Path(resolved_policy_path)
    config_path = source_dir / "config.json"
    if not source_dir.is_dir() or not config_path.is_file():
        return resolved_policy_path
    config_data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config_data, dict) or config_data.get("type") != "pi05":
        return resolved_policy_path
    unsupported_keys = sorted(key for key in config_data if key not in valid_config_keys)
    if not unsupported_keys:
        return resolved_policy_path
    temp_dir = tempfile.TemporaryDirectory(prefix="pi05_deploy_checkpoint_")
    deploy_dir = Path(temp_dir.name)
    for source_child in source_dir.iterdir():
        target_child = deploy_dir / source_child.name
        if source_child.name == "config.json":
            continue
        if source_child.is_file() and (source_child.suffix == ".json" or source_child.name == "README.md"):
            shutil.copy2(source_child, target_child)
            continue
        try:
            target_child.symlink_to(source_child, target_is_directory=source_child.is_dir())
        except OSError:
            if source_child.is_dir():
                shutil.copytree(source_child, target_child)
            else:
                shutil.copy2(source_child, target_child)
    sanitized_config = {key: value for key, value in config_data.items() if key not in unsupported_keys}
    (deploy_dir / "config.json").write_text(
        json.dumps(sanitized_config, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    _TEMP_DEPLOY_CHECKPOINTS.append(temp_dir)
    print("PI0.5 deploy config compatibility: removed unsupported fields "
          f"{unsupported_keys}; using temporary checkpoint view {deploy_dir}")
    return str(deploy_dir)


# ── policy loading (ported from deploy) ────────────────────────────

def load_pi05_policy_and_processors(
    policy_path: str,
    device: str,
    local_files_only: bool = True,
    n_action_steps: Optional[int] = None,
    base_model_path: str = "",
    tokenizer_path: str = "",
    dtype: str = "bfloat16",
) -> Tuple[Any, Any, Any]:
    """Load PI0.5 through the offline-evaluation route (PEFT + chunk_size + processors).

    `dtype` ("bfloat16"/"float16"/"float32") is applied at load time so a 14GB fp32
    checkpoint fits on small GPUs (8GB)."""
    import torch
    from lerobot.policies.pi05 import PI05Policy
    from lerobot.policies.pi05.configuration_pi05 import PI05Config
    try:
        from lerobot.configs import PreTrainedConfig  # lerobot >=0.5
    except ImportError:
        from lerobot.configs.policies import PreTrainedConfig  # lerobot 0.4.x
    try:
        from lerobot.policies import make_pre_post_processors  # lerobot >=0.5
    except ImportError:
        from lerobot.policies.factory import make_pre_post_processors  # lerobot 0.4.x

    _dtype_map = {"bfloat16": torch.bfloat16, "float16": torch.float16, "half": torch.float16,
                  "float32": torch.float32, None: None}
    torch_dtype = _dtype_map.get(dtype, torch.bfloat16)

    pi05_config_keys = {field_info.name for field_info in dataclass_fields(PI05Config)}
    pi05_config_keys.add("type")
    policy_path = make_pi05_deploy_compatible_policy_path(policy_path, pi05_config_keys)
    config = PreTrainedConfig.from_pretrained(policy_path, local_files_only=local_files_only)
    if getattr(config, "type", None) != "pi05":
        raise ValueError(f"Expected a pi05 checkpoint, got policy type={getattr(config, 'type', None)!r}")

    # Load on CPU first; cast to dtype + move to GPU after (avoids fp32-on-GPU OOM
    # during PI05Policy.from_pretrained's internal self.model.to(config.device)).
    config.device = "cpu"
    if n_action_steps is not None:
        requested = int(n_action_steps)
        chunk_size = int(getattr(config, "chunk_size", requested))
        if requested < 1:
            raise ValueError(f"n_action_steps must be >= 1, got {requested}")
        if requested > chunk_size:
            print(f"WARNING: requested n_action_steps exceeds chunk_size; clamping {requested} -> {chunk_size}")
        config.n_action_steps = max(1, min(requested, chunk_size))
        print(f"PI0.5 rollout queue: n_action_steps={config.n_action_steps}, chunk_size={chunk_size}")
    else:
        print(f"PI0.5 rollout queue: using checkpoint n_action_steps={getattr(config, 'n_action_steps', None)}, "
              f"chunk_size={getattr(config, 'chunk_size', None)}")

    def _from_pretrained(path):
        return PI05Policy.from_pretrained(path, config=config, local_files_only=local_files_only)

    if getattr(config, "use_peft", False):
        from peft import PeftConfig, PeftModel
        peft_config = PeftConfig.from_pretrained(policy_path, local_files_only=local_files_only)
        base_path = str(base_model_path).strip() or peft_config.base_model_name_or_path
        if not base_path:
            raise RuntimeError("LoRA adapter_config.json does not contain base_model_name_or_path")
        print(f"Loading PI0.5 base model (CPU): {base_path}")
        policy = _from_pretrained(base_path)
        peft_kwargs = {"config": peft_config, "is_trainable": False, "local_files_only": local_files_only}
        try:
            policy = PeftModel.from_pretrained(policy, policy_path, **peft_kwargs)
        except TypeError:
            peft_kwargs.pop("local_files_only")
            policy = PeftModel.from_pretrained(policy, policy_path, **peft_kwargs)
    else:
        print(f"Loading full PI0.5 policy checkpoint (CPU): {policy_path}")
        policy = _from_pretrained(policy_path)

    if torch_dtype is not None:
        policy.to(torch_dtype)
    policy.to(device)
    policy.eval()
    config.device = device  # restore for processor device below
    if hasattr(policy, "reset"):
        policy.reset()

    _override_saved_tokenizer_path(policy_path, tokenizer_path)
    ensure_legacy_pi05_processor_registry_aliases(policy_path)  # register missing steps AFTER temp dir is ready
    preprocessor, postprocessor = make_pre_post_processors(
        policy_cfg=config,
        pretrained_path=policy_path,
        preprocessor_overrides={"device_processor": {"device": device}},
    )
    return policy, preprocessor, postprocessor


# ── observation / action (ported from deploy) ──────────────────────

def image_hwc_to_chw(image: np.ndarray, image_mode: str = "float01") -> torch.Tensor:
    """Convert one RGB image to CHW (no batch dim). PI0.5's saved preprocessor batches + moves to device."""
    arr = np.asarray(image)
    if arr.ndim != 3:
        raise ValueError(f"image expects 3 dims, got shape={arr.shape}")
    if arr.shape[-1] in (1, 3, 4):
        if arr.shape[-1] == 4:
            arr = arr[..., :3]
        tensor = torch.from_numpy(np.ascontiguousarray(arr)).permute(2, 0, 1).contiguous()
    elif arr.shape[0] in (1, 3):
        tensor = torch.from_numpy(np.ascontiguousarray(arr)).contiguous()
    else:
        raise ValueError(f"cannot infer image channel dimension from shape={arr.shape}")
    if image_mode == "float01":
        return tensor.to(dtype=torch.float32).div_(255.0)
    if image_mode == "float255":
        return tensor.to(dtype=torch.float32)
    if image_mode == "uint8":
        return tensor.to(dtype=torch.uint8)
    raise ValueError(f"unknown image_mode={image_mode!r}")


def build_pi05_observation(
    *,
    policy: Any,
    state: list,
    images: Optional[dict],
    task: str,
    state_key: str,
    image_key_map: dict,
    image_mode: str = "float01",
) -> dict:
    if len(state) != TOTAL_DOF:
        raise ValueError(f"PI0.5 state must have {TOTAL_DOF} values, got {len(state)}")
    observation: dict = {
        state_key: torch.as_tensor(state, dtype=torch.float32),
        "task": task,
    }
    policy_image_keys = get_policy_image_keys(policy_metadata_source(policy))
    provided: set = set()
    for source_key, image in dict(images or {}).items():
        policy_key = image_key_map.get(source_key, source_key)
        if policy_image_keys and policy_key not in policy_image_keys:
            continue
        observation[policy_key] = image_hwc_to_chw(image, image_mode)
        provided.add(policy_key)
    missing = [key for key in policy_image_keys if key not in provided]
    if missing:
        raise RuntimeError(
            f"missing policy image keys: {missing}. Use image_key_map if camera names differ from training keys.")
    return observation


def action_to_vector(action: Any, chunk_step_index: int = 0) -> np.ndarray:
    if isinstance(action, dict):
        action = action.get("action")
    if action is None:
        raise RuntimeError("PI0.5 output does not contain action")
    if isinstance(action, torch.Tensor):
        arr = action.detach().float().cpu().numpy()
    else:
        arr = np.asarray(action, dtype=np.float32)
    if arr.ndim == 3:
        step = max(0, min(int(chunk_step_index), arr.shape[1] - 1))
        arr = arr[0, step]
    elif arr.ndim == 2:
        arr = arr.reshape(-1, arr.shape[-1])[0]
    return np.asarray(arr, dtype=np.float64).reshape(-1)
