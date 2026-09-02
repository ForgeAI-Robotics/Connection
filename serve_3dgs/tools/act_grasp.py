"""act_grasp.py — ACT-based grasp, non-blocking inference via async HTTP worker.

The ACT policy runs in a SEPARATE process (services/act_server.py). This module
calls it over HTTP, but ASYNCHRONOUSLY so the sim main loop / viewer never stall
on the round-trip:

    serve_3dgs main loop (this process)  --HTTP-->  act_server (separate process)

A background worker thread owns the HTTP round-trip + base64 encoding: the main
loop renders cameras + reads qpos and submits a snapshot (non-blocking,
drop-stale); the worker POSTs to act_server and publishes the latest returned
action; the main loop applies the newest available action every iteration. env
stays single-threaded; the main loop never blocks on inference.

Matches teleop/act/sim_act_inference.py cadence: 640x480 cameras, policy_fps=30,
fresh camera each submit. Physics is NOT stepped here — the main loop owns
env.step() (avoids the old double-stepping with the main loop).

Two-phase API (like the nav command):
    act_grasp_init(env, act_url, obj_name, ...)  -> state dict
    act_grasp_step(env, state) -> mutates state in-place; state["done"] when finished
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

# Ensure torch can find ninja for JIT-compiling C++ extensions (the composite mesh
# renderer needs one). ninja may be installed in the venv but not on PATH (e.g. when
# python is invoked directly without `source activate`). Harmless if ninja is absent.
try:
    import ninja as _ninja
    _ninja_bin = getattr(_ninja, "BIN_DIR", None) or os.path.dirname(_ninja.__file__)
    if _ninja_bin and os.path.isdir(_ninja_bin) and _ninja_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ninja_bin + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

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
# HTTP client keys understood by act_server.py.
CAMERA_CLIENT_KEYS = ["top", "left_wrist", "right_wrist"]

# Init pose (arms folded up, ready for grasping)
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
    """Read 14 joint positions from sim."""
    dof_pos = np.asarray(env.data.dof_pos, dtype=np.float64).reshape(-1)
    if dof_pos.size >= NUM_JOINTS:
        return dof_pos[:NUM_JOINTS].copy()
    return dof_pos.copy()


def set_act_qpos(env, qpos: np.ndarray):
    """Set 14 joint targets via position actuators."""
    for i, name in enumerate(BLUETHINK_JOINTS):
        act = _find_actuator(env, name)
        if act is not None:
            act.set_ctrl(env.data, float(qpos[i]))


def move_to_init_pose(env, hold_steps=200):
    """Move arms to init pose and hold."""
    set_act_qpos(env, INIT_QPOS)
    for _ in range(hold_steps):
        env.step()
    env.forward_kinematic()


def render_camera(env, cam_id: int, w: int = 640, h: int = 480) -> np.ndarray:
    """Render a camera frame as HWC uint8 numpy array."""
    frame = env.render_frame(cam_id=cam_id, width=w, height=h)
    return np.asarray(frame, dtype=np.uint8)


def image_to_base64(img: np.ndarray) -> str:
    """HWC uint8 -> base64 string (PNG)."""
    pil = Image.fromarray(img)
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build_camera_name_to_id(env) -> Dict[str, int]:
    """Build camera name -> index map from model cameras."""
    name_to_id: Dict[str, int] = {}
    for idx in range(len(env.model.cameras)):
        cam = env.model.cameras[idx]
        name = getattr(cam, "name", "")
        if name:
            name_to_id[name] = idx
    return name_to_id


def get_camera_images(env, cam_ids: Dict[str, int], w: int = 640, h: int = 480) -> Dict[str, np.ndarray]:
    """Render all 3 cameras -> raw HWC uint8 arrays keyed by CAMERA_NAMES (for the snapshot)."""
    images = {}
    for name in CAMERA_NAMES:
        cid = cam_ids.get(name)
        if cid is None:
            continue
        images[name] = render_camera(env, cid, w, h)
    return images


def get_camera_images_b64(env, cam_ids: Dict[str, int], w: int = 640, h: int = 480) -> Dict[str, str]:
    """Render all 3 cameras -> dict of base64 images (kept for compatibility)."""
    return {
        CAMERA_CLIENT_KEYS[CAMERA_NAMES.index(name)]: image_to_base64(img)
        for name, img in get_camera_images(env, cam_ids, w, h).items()
    }


def get_object_z(env, obj_name: str) -> float:
    """Get object z position."""
    try:
        pos = env.get_body_xpos(obj_name)
        return float(pos[2])
    except Exception:
        return 0.0


# ── HTTP inference (runs inside the worker thread) ─────────────────

def call_act_service(act_url: str, state: np.ndarray, images_b64: Dict[str, str],
                     timeout: float = 5.0) -> Optional[np.ndarray]:
    """POST state+images to the standalone ACT service (act_server.py). Returns 14-dim
    actions, or None on error (so the worker keeps the previous action)."""
    import requests
    try:
        resp = requests.post(
            f"{act_url}/act_step",
            json={"state": np.asarray(state).tolist(), "images": images_b64},
            timeout=timeout,
        )
        resp.raise_for_status()
        return np.array(resp.json()["actions"], dtype=np.float64)
    except Exception as e:
        print(f"[act_grasp] ACT service error: {e}")
        return None


def make_http_infer_fn(act_url: str) -> Callable[[dict], Optional[np.ndarray]]:
    """Build a worker inference fn that calls a standalone act_server over HTTP.

    base64 PNG encoding is done here, inside the worker thread (off the main loop).
    """
    def fn(snap: dict) -> Optional[np.ndarray]:
        images_b64 = {
            CAMERA_CLIENT_KEYS[CAMERA_NAMES.index(name)]: image_to_base64(img)
            for name, img in snap["images"].items()
        }
        state = np.asarray(snap["state"], dtype=np.float64).reshape(-1)
        return call_act_service(act_url, state, images_b64)
    return fn


# ── Async worker thread ────────────────────────────────────────────

class _ActAsyncWorker(threading.Thread):
    """Background thread: snapshot -> infer_fn -> latest-action slot.

    Single producer (the main loop, under the env lock), single consumer (this thread).
    The worker only calls infer_fn on snapshot copies — it never touches env, so there
    is no data race with the main loop's render/physics, and the main loop never blocks
    on inference latency. Stale snapshots are dropped so we always act on the newest frame.
    """

    def __init__(self, infer_fn: Callable[[dict], Optional[np.ndarray]]):
        super().__init__(daemon=True, name="act-infer")
        self.infer_fn = infer_fn
        self._q: "queue.Queue" = queue.Queue(maxsize=1)  # keep newest snapshot only
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._latest_step = -1
        self._stop = False
        self.infer_count = 0
        self.last_error: Optional[str] = None

    def stop(self):
        self._stop = True

    def submit(self, snapshot: dict):
        """Non-blocking. Drains stale snapshots, keeps only the newest."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(snapshot)
        except queue.Full:
            pass

    def latest(self) -> Optional[Tuple[np.ndarray, int]]:
        with self._lock:
            if self._latest is None:
                return None
            return self._latest.copy(), self._latest_step

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
                    continue  # keep previous action (e.g. transient HTTP error)
                action = np.asarray(action, dtype=np.float64).reshape(-1)
                if action.size != NUM_JOINTS:
                    print(f"[act_grasp] action dim={action.size} != {NUM_JOINTS}, ignoring", flush=True)
                    continue
                with self._lock:
                    self._latest = action
                    self._latest_step = snap["step"]
                self.infer_count += 1
            except Exception as e:
                self.last_error = str(e)
                print(f"[act_grasp] worker inference error: {e}", flush=True)


# ── Two-phase API (init + step per main-loop iteration) ────────────

def act_grasp_init(env, act_url: str, obj_name: str, max_steps: int = 300,
                   physics_steps: int = 10, camera_w: int = 640, camera_h: int = 480,
                   lift_threshold: float = 0.05, policy_fps: float = 30.0,
                   init_hold_steps: int = 100, render_interval: int = 1) -> dict:
    """Initialize ACT grasp state and start a non-blocking HTTP inference worker.

    Matches sim_act_inference cadence: 640x480 cameras, policy_fps=30, fresh camera
    each submit (render_interval is kept for API compat; the async path always renders
    fresh). Call act_grasp_step() once per main-loop iteration.
    """
    if not act_url:
        raise RuntimeError("act_grasp_init requires act_url (ACT service URL)")

    cam_ids = build_camera_name_to_id(env)
    obj_z_before = get_object_z(env, obj_name)
    set_act_qpos(env, INIT_QPOS)
    print(f"[act_grasp] Init pose target set. Object '{obj_name}' z_before={obj_z_before:.4f}", flush=True)

    worker = _ActAsyncWorker(make_http_infer_fn(act_url))
    worker.start()
    print(f"[act_grasp] Async HTTP worker started -> {act_url} (non-blocking)", flush=True)

    return {
        "done": False,
        "success": False,
        "phase": "init",          # "init" -> hold init pose, "run" -> ACT inference
        "init_remaining": init_hold_steps,
        "step": 0,
        "steps_run": 0,
        "max_steps": max_steps,
        "physics_steps": physics_steps,   # informational; main loop owns env.step()
        "camera_w": camera_w,
        "camera_h": camera_h,
        "lift_threshold": lift_threshold,
        "obj_name": obj_name,
        "obj_z_before": obj_z_before,
        "act_url": act_url,
        "cam_ids": cam_ids,
        "dt": 1.0 / max(policy_fps, 1e-3),     # min seconds between submits
        "last_submit_time": 0.0,
        "render_interval": render_interval,     # kept for API compat
        "worker": worker,
        "last_applied": None,                    # last action applied to the arm
    }


def _stop_worker(state: dict):
    w = state.get("worker")
    if w is not None:
        w.stop()
        state["worker_inferences"] = w.infer_count


def act_grasp_step(env, state: dict) -> dict:
    """Advance ACT grasp by one main-loop iteration. Mutates state in-place.

    The main loop drives physics (env.step()); here we only update joint targets:
    submit a fresh snapshot at policy_fps and apply the newest completed action.
    Sets state["done"] = True when finished.
    """
    if state["done"]:
        return state

    import time

    # Phase 1: hold init pose (the main loop's physics settles the arm toward INIT_QPOS).
    if state["phase"] == "init":
        set_act_qpos(env, INIT_QPOS)
        state["init_remaining"] -= 1
        if state["init_remaining"] <= 0:
            env.forward_kinematic()
            state["phase"] = "run"
            state["last_submit_time"] = 0.0  # trigger first submit immediately
            print("[act_grasp] Init complete, starting ACT inference", flush=True)
        return state

    # Phase 2: ACT inference (non-blocking)
    now = time.time()
    worker = state["worker"]

    # 1) Submit a fresh snapshot at policy_fps (render 3 cams + read state on the main thread).
    if now - state["last_submit_time"] >= state["dt"]:
        state["last_submit_time"] = now
        qpos = get_act_state(env)
        images = get_camera_images(env, state["cam_ids"], state["camera_w"], state["camera_h"])
        worker.submit({"step": state["step"], "state": qpos, "images": images})

    # 2) Apply the latest completed action; hold init pose / last action until the first returns.
    latest = worker.latest()
    if latest is not None:
        action, _step = latest
        state["last_applied"] = action
        set_act_qpos(env, action)
    elif state["last_applied"] is None:
        set_act_qpos(env, INIT_QPOS)

    state["step"] += 1
    state["steps_run"] += 1

    # Debug log every 10 steps
    if state["step"] % 10 == 0:
        ap = state["last_applied"]
        ap3 = ap[:3].tolist() if ap is not None else None
        print(f"[act_grasp] step={state['step']}/{state['max_steps']} "
              f"inferences={worker.infer_count} action[:3]={ap3}", flush=True)

    # Check if object lifted (every 30 steps)
    if state["step"] % 30 == 0:
        obj_z = get_object_z(env, state["obj_name"])
        lifted = obj_z - state["obj_z_before"]
        if lifted > state["lift_threshold"]:
            print(f"[act_grasp] ✓ Object lifted! dz={lifted:.3f}m after {state['steps_run']} steps "
                  f"({worker.infer_count} inferences)")
            state["success"] = True
            state["done"] = True
            _stop_worker(state)
            return state

    # Check max steps
    if state["step"] >= state["max_steps"]:
        obj_z_after = get_object_z(env, state["obj_name"])
        lifted = obj_z_after - state["obj_z_before"]
        state["success"] = lifted > state["lift_threshold"]
        print(f"[act_grasp] Finished. dz={lifted:.3f}m, steps={state['steps_run']}, "
              f"inferences={worker.infer_count}, success={state['success']}")
        state["done"] = True
        _stop_worker(state)

    return state


# ── Blocking API (for standalone use, e.g. without the server main loop) ──

def act_grasp(env, act_url: str, obj_name: str, max_steps: int = 300,
              physics_steps: int = 10, camera_w: int = 640, camera_h: int = 480,
              lift_threshold: float = 0.05, policy_fps: float = 30.0) -> dict:
    """Blocking version: runs the full ACT grasp loop, stepping physics itself."""
    import time
    state = act_grasp_init(
        env, act_url, obj_name, max_steps=max_steps, physics_steps=physics_steps,
        camera_w=camera_w, camera_h=camera_h, lift_threshold=lift_threshold, policy_fps=policy_fps,
    )
    dt = 1.0 / max(policy_fps, 1e-3)
    next_t = time.time()
    while not state["done"]:
        act_grasp_step(env, state)
        for _ in range(physics_steps):
            env.step()
        now = time.time()
        sleep_t = next_t - now
        if sleep_t > 0:
            time.sleep(min(0.005, sleep_t))
        next_t = max(now, next_t) + dt

    from tools.move import get_base_info
    info = get_base_info(env)
    return {"success": state["success"], "steps_run": state["steps_run"], **info}
