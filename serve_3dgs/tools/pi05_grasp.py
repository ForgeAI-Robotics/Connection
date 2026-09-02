"""pi05_grasp.py — PI0.5 grasp via remote inference service (non-blocking).

Mirror of tools/act_grasp.py, but for PI0.5 served by services/pi05_server.py:
  - state/action are 16-dim = 14 arm joints + left/right gripper. The sim robot
    has 14 actuators (no grippers), so gripper dims are padded in the state
    (tracked from the policy's last commanded value) and dropped on apply.
  - a language `task` string is sent each step (PI0.5 is language-conditioned).
  - 224x224 images (PI0.5 image_resolution).

Architecture: PI0.5 runs in a SEPARATE process/machine (pi05_server.py); this
module calls it over HTTP asynchronously (background worker thread), so the sim
main loop / viewer never stall on the round-trip. env stays single-threaded.

Two-phase API (like act_grasp / nav):
    pi05_grasp_init(env, pi05_url, obj_name, task=..., ...)  -> state dict
    pi05_grasp_step(env, state) -> mutates state in-place; state["done"] when finished
"""
from __future__ import annotations

import base64
import io
import os
import queue
import threading
from typing import Callable, Dict, Optional, Tuple

import numpy as np
from PIL import Image

# Ensure torch can find ninja for JIT C++ extensions (composite mesh, etc.).
try:
    import ninja as _ninja
    _nb = getattr(_ninja, "BIN_DIR", None) or os.path.dirname(_ninja.__file__)
    if _nb and os.path.isdir(_nb) and _nb not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _nb + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

BLUETHINK_LEFT_JOINTS = [
    "Left_Shoulder_Pitch_Joint", "Left_Shoulder_Roll_Joint", "Left_Shoulder_Yaw_Joint",
    "Left_Elbow_Pitch_Joint", "Left_Wrist_Yaw_Joint", "Left_Wrist_Pitch_Joint", "Left_Wrist_Roll_Joint",
]
BLUETHINK_RIGHT_JOINTS = [
    "Right_Shoulder_Pitch_Joint", "Right_Shoulder_Roll_Joint", "Right_Shoulder_Yaw_Joint",
    "Right_Elbow_Pitch_Joint", "Right_Wrist_Yaw_Joint", "Right_Wrist_Pitch_Joint", "Right_Wrist_Roll_Joint",
]
BLUETHINK_JOINTS = BLUETHINK_LEFT_JOINTS + BLUETHINK_RIGHT_JOINTS
NUM_JOINTS = len(BLUETHINK_JOINTS)  # 14 arm joints
GRIPPER_DOF = 2
TOTAL_DOF = NUM_JOINTS + GRIPPER_DOF  # 16 (PI0.5 state/action)

CAMERA_NAMES = ["base_cam", "left_ee_cam", "right_ee_cam"]
# HTTP client keys understood by pi05_server.py.
CAMERA_CLIENT_KEYS = ["top", "left_wrist", "right_wrist"]

INIT_QPOS_LEFT = [-0.5035, 0.6527, 1.1747, 1.65, -0.7471, 0.2444, 0.9295]
INIT_QPOS_RIGHT = [0.3455, -0.5776, -0.9003, 1.6831, 0.6373, -0.0942, -0.7968]
INIT_QPOS = np.array(INIT_QPOS_LEFT + INIT_QPOS_RIGHT, dtype=np.float64)


# ── Sim helpers ────────────────────────────────────────────────────

def _find_actuator(env, joint_name: str):
    for i in range(env.model.num_actuators):
        act = env.model.get_actuator(i)
        if act.target_name == joint_name:
            return act
    return None


def get_act_state(env) -> np.ndarray:
    """Read 14 arm joint positions from sim."""
    dof_pos = np.asarray(env.data.dof_pos, dtype=np.float64).reshape(-1)
    if dof_pos.size >= NUM_JOINTS:
        return dof_pos[:NUM_JOINTS].copy()
    return dof_pos.copy()


def set_act_qpos(env, qpos: np.ndarray):
    for i, name in enumerate(BLUETHINK_JOINTS):
        act = _find_actuator(env, name)
        if act is not None:
            act.set_ctrl(env.data, float(qpos[i]))


def render_camera(env, cam_id: int, w: int = 224, h: int = 224) -> np.ndarray:
    frame = env.render_frame(cam_id=cam_id, width=w, height=h)
    return np.asarray(frame, dtype=np.uint8)


def image_to_base64(img: np.ndarray) -> str:
    pil = Image.fromarray(img)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=90)  # JPEG: much smaller/faster than PNG over HTTP
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build_camera_name_to_id(env) -> Dict[str, int]:
    name_to_id: Dict[str, int] = {}
    for idx in range(len(env.model.cameras)):
        cam = env.model.cameras[idx]
        name = getattr(cam, "name", "")
        if name:
            name_to_id[name] = idx
    return name_to_id


def get_camera_images(env, cam_ids: Dict[str, int], w: int = 224, h: int = 224) -> Dict[str, np.ndarray]:
    images = {}
    for name in CAMERA_NAMES:
        cid = cam_ids.get(name)
        if cid is None:
            continue
        images[name] = render_camera(env, cid, w, h)
    return images


def get_object_z(env, obj_name: str) -> float:
    try:
        return float(env.get_body_xpos(obj_name)[2])
    except Exception:
        return 0.0


# ── HTTP inference (runs inside the worker thread) ─────────────────

def call_pi05_service(pi05_url: str, state: list, images_b64: Dict[str, str],
                      task: str, timeout: float = 10.0) -> Optional[np.ndarray]:
    """POST state+images+task to pi05_server, return 16-dim actions or None."""
    import requests
    try:
        resp = requests.post(
            f"{pi05_url}/pi05_step",
            json={"state": list(state), "images": images_b64, "task": task},
            timeout=timeout,
        )
        resp.raise_for_status()
        return np.array(resp.json()["actions"], dtype=np.float64)
    except Exception as e:
        print(f"[pi05_grasp] PI0.5 service error: {e}")
        return None


def make_http_infer_fn(pi05_url: str, task: str) -> Callable[[dict], Optional[np.ndarray]]:
    """Worker inference fn: JPEG-encode images (off main loop) + POST to pi05_server."""
    def fn(snap: dict) -> Optional[np.ndarray]:
        images_b64 = {
            CAMERA_CLIENT_KEYS[CAMERA_NAMES.index(name)]: image_to_base64(img)
            for name, img in snap["images"].items()
        }
        return call_pi05_service(pi05_url, snap["state"], images_b64, task)
    return fn


# ── Async worker thread (same shape as act_grasp) ──────────────────

class _Pi05AsyncWorker(threading.Thread):
    def __init__(self, infer_fn: Callable[[dict], Optional[np.ndarray]]):
        super().__init__(daemon=True, name="pi05-infer")
        self.infer_fn = infer_fn
        self._q: "queue.Queue" = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._stop = False
        self.infer_count = 0
        self.last_error: Optional[str] = None

    def stop(self):
        self._stop = True

    def submit(self, snapshot: dict):
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(snapshot)
        except queue.Full:
            pass

    def latest(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def run(self):
        while not self._stop:
            try:
                snap = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._stop:
                break
            try:
                action = self.infer_fn(snap)
                if action is None:
                    continue
                action = np.asarray(action, dtype=np.float64).reshape(-1)
                if action.size != TOTAL_DOF:
                    print(f"[pi05_grasp] action dim={action.size} != {TOTAL_DOF}, ignoring", flush=True)
                    continue
                with self._lock:
                    self._latest = action
                self.infer_count += 1
            except Exception as e:
                self.last_error = str(e)
                print(f"[pi05_grasp] worker error: {e}", flush=True)


# ── Two-phase API ──────────────────────────────────────────────────

def pi05_grasp_init(env, pi05_url: str, obj_name: str, *, task: str,
                    max_steps: int = 1000, camera_w: int = 224, camera_h: int = 224,
                    lift_threshold: float = 0.05, policy_fps: float = 30.0,
                    init_hold_steps: int = 100) -> dict:
    """Initialize PI0.5 grasp state and start a non-blocking HTTP inference worker."""
    if not pi05_url:
        raise RuntimeError("pi05_grasp_init requires pi05_url (PI0.5 service URL)")
    cam_ids = build_camera_name_to_id(env)
    obj_z_before = get_object_z(env, obj_name)
    set_act_qpos(env, INIT_QPOS)
    print(f"[pi05_grasp] Init pose target set. Object '{obj_name}' z_before={obj_z_before:.4f} "
          f"task={task!r}", flush=True)

    worker = _Pi05AsyncWorker(make_http_infer_fn(pi05_url, task))
    worker.start()
    print(f"[pi05_grasp] Async HTTP worker started -> {pi05_url} (non-blocking)", flush=True)

    return {
        "done": False,
        "success": False,
        "phase": "init",
        "init_remaining": init_hold_steps,
        "step": 0,
        "steps_run": 0,
        "max_steps": max_steps,
        "camera_w": camera_w,
        "camera_h": camera_h,
        "lift_threshold": lift_threshold,
        "obj_name": obj_name,
        "obj_z_before": obj_z_before,
        "pi05_url": pi05_url,
        "task": task,
        "cam_ids": cam_ids,
        "worker": worker,
        "dt": 1.0 / max(policy_fps, 1e-3),
        "last_submit_time": 0.0,
        "gripper_state": np.array([1.0, 1.0], dtype=np.float64),  # sim has no grippers; track policy cmds
        "last_applied": None,
    }


def _stop_worker(state: dict):
    w = state.get("worker")
    if w is not None:
        w.stop()
        state["worker_inferences"] = w.infer_count


def pi05_grasp_step(env, state: dict) -> dict:
    """Advance PI0.5 grasp by one main-loop iteration. Main loop owns env.step()."""
    if state["done"]:
        return state
    import time

    if state["phase"] == "init":
        set_act_qpos(env, INIT_QPOS)
        state["init_remaining"] -= 1
        if state["init_remaining"] <= 0:
            env.forward_kinematic()
            state["phase"] = "run"
            state["last_submit_time"] = 0.0
            # Reset the remote policy's action queue at episode start.
            try:
                import requests
                requests.post(f"{state['pi05_url']}/reset", timeout=2.0)
            except Exception:
                pass
            print("[pi05_grasp] Init complete, starting PI0.5 inference", flush=True)
        return state

    now = time.time()
    worker = state["worker"]

    if now - state["last_submit_time"] >= state["dt"]:
        state["last_submit_time"] = now
        joints = get_act_state(env)
        state16 = np.concatenate([joints, state["gripper_state"]]).tolist()
        images = get_camera_images(env, state["cam_ids"], state["camera_w"], state["camera_h"])
        worker.submit({"state": state16, "images": images})

    latest = worker.latest()
    if latest is not None:
        state["last_applied"] = latest
        set_act_qpos(env, latest[:NUM_JOINTS])
        state["gripper_state"] = np.clip(latest[NUM_JOINTS:TOTAL_DOF], 0.0, 1.0)
    elif state["last_applied"] is None:
        set_act_qpos(env, INIT_QPOS)

    state["step"] += 1
    state["steps_run"] += 1

    if state["step"] % 10 == 0:
        ap = state["last_applied"]
        ap3 = ap[:3].tolist() if ap is not None else None
        print(f"[pi05_grasp] step={state['step']}/{state['max_steps']} "
              f"inferences={worker.infer_count} action[:3]={ap3} "
              f"grip={np.round(state['gripper_state'],2).tolist()}", flush=True)

    if state["step"] % 30 == 0:
        obj_z = get_object_z(env, state["obj_name"])
        lifted = obj_z - state["obj_z_before"]
        if lifted > state["lift_threshold"]:
            print(f"[pi05_grasp] ✓ Object lifted! dz={lifted:.3f}m after {state['steps_run']} steps "
                  f"({worker.infer_count} inferences)")
            state["success"] = True
            state["done"] = True
            _stop_worker(state)
            return state

    if state["step"] >= state["max_steps"]:
        obj_z_after = get_object_z(env, state["obj_name"])
        lifted = obj_z_after - state["obj_z_before"]
        state["success"] = lifted > state["lift_threshold"]
        print(f"[pi05_grasp] Finished. dz={lifted:.3f}m, steps={state['steps_run']}, "
              f"inferences={worker.infer_count}, success={state['success']}")
        state["done"] = True
        _stop_worker(state)

    return state
