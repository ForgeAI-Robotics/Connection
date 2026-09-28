"""Read-only integration with the colleague's calibrated O6 grasp task."""
import hashlib
import json
import math
from pathlib import Path

from .store import VERSION


def overlay(config):
    return Path(config["source"]) / config["o6_overlay"]


def fingerprint(config):
    root = Path(config["source"])
    paths = [root / "scripts/validate_g1_o6_cycle.py"]
    paths += sorted(overlay(config).rglob("*.py"))
    paths += sorted((root / "data/robots/g1_o6/curobo/o6_mp").glob("*"))
    if not (overlay(config) / "simple/robots/g1_o6_mp.py").is_file():
        raise ValueError("o6_overlay_missing")
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


def scene(config):
    revision = {"source": fingerprint(config), "seed": int(config.get("seed", 101)),
                "task": "grasp", "world_tracking": True, "torso_feedback": True,
                "tracking_iterations": 10, "plan_attempts": 1}
    return {"contract_version": VERSION, "backend": "simple_o7", "robot_variant": "o6",
            "scene_id": "g1_o6_can_grasp_mp", "asset_version": "graspnet1b:2",
            "scene_revision": hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest(),
            "observation_source": "native_task_template", "scope": "new_episode_template_not_live_camera",
            "capabilities": ["pick_hold_can"], "episode_policy": "one_compound_action_per_task",
            "objects": {"can": {"asset_id": "graspnet1b:2", "name": "校准汤罐", "graspable": True}},
            "limitations": ["Only the calibrated soup can, not the O7 coke model",
                            "No navigation, placement or live camera endpoint",
                            "New task creates a new episode; same task never resets"]}


def collect(output):
    """Recompute terminal hold from sampled physics, never translate O7 lowering evidence."""
    import numpy as np
    output = Path(output)
    report = json.loads((output / "report.json").read_text())
    if report.get("task") != "simple/G1O6CanGraspMP-v0":
        raise ValueError("o6_task_mismatch")
    if not (output / "final_physics_state.npz").is_file():
        raise ValueError("physical_terminal_evidence_missing")
    with np.load(output / "final_physics_state.npz", allow_pickle=False) as terminal:
        if not all(np.isfinite(terminal[k]).all() for k in ("qpos", "qvel", "ctrl")):
            raise ValueError("nonfinite_terminal_state")
    with np.load(output / "diagnostic_trajectory.npz", allow_pickle=False) as data:
        times, poses, contact = data["time"], data["object_pose"], data["contact_state"]
        names = list(data["contact_state_names"])
        if len(times) < 2 or poses.shape != (len(times), 7) or contact.shape != (len(times), 3):
            raise ValueError("physical_samples_missing")
        if not np.isfinite(times).all() or not np.isfinite(poses).all() or not (np.diff(times) > 0).all():
            raise ValueError("invalid_physical_samples")
        support = contact[:, names.index("target_table_contact")]
        grasp = contact[:, names.index("target_grasp_contact")]
        speed = np.r_[np.inf, np.linalg.norm(np.diff(poses[:, :3], axis=0), axis=1) / np.diff(times)]
        initial_z = float(report["initial_target"][2])
        held = (poses[:, 2] - initial_z >= .08) & grasp & ~support & (speed <= .02)
        start = len(times) - 1
        while start > 0 and held[start - 1]:
            start -= 1
        duration = float(times[-1] - times[start]) if held[-1] else 0.
        sample = {"held": bool(held[-1]), "supported": bool(support[-1]),
                  "can_position": poses[-1, :3].tolist(), "object_speed_m_s": float(speed[-1]),
                  "lift_m": float(poses[-1, 2] - initial_z)}
    penetration_keys = ("self_penetration_m", "grasp_penetration_m", "object_environment_penetration_m",
                        "substep_max_self_penetration_m", "substep_max_target_penetration_m",
                        "substep_max_robot_environment_penetration_m")
    penetrations = [float(report.get(k, float("inf"))) for k in penetration_keys]
    penetration = max(penetrations) if all(math.isfinite(v) and v >= 0 for v in penetrations) else float("inf")
    tilt = float(report.get("base_tilt_degrees", float("inf")))
    passed = (report.get("task_passed") is True and report.get("terminated") is True
              and report.get("truncated") is False and report.get("stable_grasp_achieved") is True
              and not report.get("error") and sample["held"] and not sample["supported"]
              and duration >= 1.0 - 1e-6 and math.isfinite(penetration) and 0 <= penetration <= .003
              and math.isfinite(tilt) and 0 <= tilt <= 20)
    result = {"robot_variant": "o6", "evidence_profile": "o6_native_grasp_v1", "pick_hold_passed": bool(passed),
              "lifted": bool(sample["lift_m"] >= .08), "terminal_stable_hold_s": duration,
              "maximum_guarded_penetration_m": penetration if math.isfinite(penetration) else None,
              "base_tilt_degrees": tilt if math.isfinite(tilt) else None,
              "stop_reason": "completed" if passed else report.get("error") or "physical_hold_not_verified"}
    diagnostics = {key: report.get(key) for key in ("seed", "frames", "task", "task_passed", "error",
                    "peak_lift_m", "phase_events", "source_sha256", "asset_sha256", "production_ready")}
    return result, sample, diagnostics
