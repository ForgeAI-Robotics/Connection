#!/usr/bin/env python3
"""Linghou mobile base — MuJoCo viewer with keyboard drive.

Controls
--------
  W / Arrow-Up    Forward
  S / Arrow-Down  Backward
  A / Arrow-Left  Turn left
  D / Arrow-Right Turn right
  Space           Stop all motion
"""
import os
import mujoco
import mujoco.viewer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
XML_PATH = os.path.join(SCRIPT_DIR, "base.xml")

# ── drive state (mutable, shared with callback) ─────────────────
class _Drive:
    vx = 0.0   # +1 forward, -1 backward
    vyaw = 0.0  # +1 turn left, -1 turn right

drv = _Drive()

# GLFW key codes
_KW, _KA, _KS, _KD = 87, 65, 83, 68          # WASD
_KUP, _KDN, _KLT, _KRT = 265, 264, 263, 262   # arrows
_KSPC = 32
_KESC = 256


def _key_callback(keycode: int):
    """Handle both press (positive) and release (negative, MuJoCo >= 2.3.2)."""
    if keycode < 0:
        k = -keycode
        if k in (_KW, _KUP) and drv.vx > 0:
            drv.vx = 0.0
        elif k in (_KS, _KDN) and drv.vx < 0:
            drv.vx = 0.0
        elif k in (_KA, _KLT) and drv.vyaw > 0:
            drv.vyaw = 0.0
        elif k in (_KD, _KRT) and drv.vyaw < 0:
            drv.vyaw = 0.0
    else:
        if keycode in (_KW, _KUP):
            drv.vx = 1.0
        elif keycode in (_KS, _KDN):
            drv.vx = -1.0
        elif keycode in (_KA, _KLT):
            drv.vyaw = 1.0
        elif keycode in (_KD, _KRT):
            drv.vyaw = -1.0
        elif keycode in (_KSPC, _KESC):
            drv.vx = 0.0
            drv.vyaw = 0.0


def main():
    model = mujoco.MjModel.from_xml_path(XML_PATH)
    data = mujoco.MjData(model)

    # ── info ────────────────────────────────────────────────────
    print(f"Model loaded: {model.nbody} bodies, "
          f"{model.njnt} joints, {model.nu} actuators")
    print()
    print("Actuator order:  0=ZQL(FR)  1=ZHL(RR)  2=YQL(FL)  3=YHL(RL)")
    print()
    print("Keyboard controls:")
    print("  W / ↑      Forward")
    print("  S / ↓      Backward")
    print("  A / ←      Turn left")
    print("  D / →      Turn right")
    print("  Space      Stop")
    print()

    speed = 8.0  # wheel angular-velocity target (rad/s)

    with mujoco.viewer.launch_passive(model, data,
                                       key_callback=_key_callback) as viewer:
        # ── 关闭昂贵渲染特效，减少 GPU 负担 ──
        viewer.opt.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = 0
        viewer.opt.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = 0
        viewer.opt.flags[mujoco.mjtRndFlag.mjRND_SKYBOX] = 0
        viewer.opt.flags[mujoco.mjtRndFlag.mjRND_FOG] = 0
        viewer.opt.flags[mujoco.mjtRndFlag.mjRND_HAZE] = 0

        while viewer.is_running():
            vx   = drv.vx   * speed
            vyaw = drv.vyaw  * speed

            # Differential-drive split
            data.ctrl[0] = vx + vyaw   # ZQL  front-right
            data.ctrl[1] = vx + vyaw   # ZHL  rear-right
            data.ctrl[2] = vx - vyaw   # YQL  front-left
            data.ctrl[3] = vx - vyaw   # YHL  rear-left

            mujoco.mj_step(model, data)
            viewer.sync()


if __name__ == "__main__":
    main()
