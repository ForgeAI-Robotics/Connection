#!/usr/bin/env python3
"""
Bridge unified robot base commands to the real differential base.

The bridge is disabled by default. Enable it in serve_real/config.yaml:
    real_base:
      enabled: 1
"""

import os
import sys
import threading
import time
from pathlib import Path


_SERVE_REAL_DIR = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _SERVE_REAL_DIR / "config.yaml"
_CANDIDATE_REALBASE_DIRS = [
    _SERVE_REAL_DIR / "backend" / "base" / "RealBase",
]
for _path in _CANDIDATE_REALBASE_DIRS:
    if (_path / "motor_controller.py").exists():
        sys.path.insert(0, str(_path))
        break

try:
    from motor_controller import OmniWheelController
except Exception as exc:  # pragma: no cover - import failure is reported at runtime
    OmniWheelController = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_config() -> dict:
    try:
        from robot_api.config import _read_yaml

        return _read_yaml(_CONFIG_PATH)
    except Exception as exc:
        print(f"[real_base] 读取配置失败 {_CONFIG_PATH}: {exc}", flush=True)
        return {}


def _env_or_config(env_name: str, config: dict, key: str, default):
    value = os.getenv(env_name)
    if value is not None:
        return value
    return config.get(key, default)


class RealBaseBridge:
    def __init__(self):
        config = (_load_config().get("real_base") or {})
        self.enabled = _as_bool(
            _env_or_config("REAL_BASE_ENABLED", config, "enabled", 0),
            default=False,
        )
        self.port = str(_env_or_config("REAL_BASE_PORT", config, "port", "/dev/ttyACM0"))
        self.speed_scale = float(_env_or_config("REAL_BASE_SPEED_SCALE", config, "speed_scale", 0.1))
        self.max_speed = float(_env_or_config("REAL_BASE_MAX_SPEED", config, "max_speed", 0.2))
        self._controller = None
        self._lock = threading.RLock()

    def is_enabled(self) -> bool:
        return self.enabled

    def _ensure_connected(self) -> bool:
        if not self.enabled:
            return False
        if OmniWheelController is None:
            print(f"[real_base] motor_controller 导入失败: {_IMPORT_ERROR}", flush=True)
            return False
        if self._controller is not None and self._controller.base_bus is not None:
            return True

        controller = OmniWheelController(port=self.port)
        if not controller.connect():
            print("[real_base] 真实底盘连接失败，跳过本次真实运动", flush=True)
            self._controller = None
            return False
        self._controller = controller
        return True

    def _sim_to_real_speed(self, vx: float) -> float:
        speed = float(vx) * self.speed_scale
        if speed > self.max_speed:
            return self.max_speed
        if speed < -self.max_speed:
            return -self.max_speed
        return speed

    def set_forward_velocity(self, vx: float) -> bool:
        with self._lock:
            if not self._ensure_connected():
                return False
            real_vx = self._sim_to_real_speed(vx)
            return bool(self._controller.set_velocity_raw(vx=real_vx, vy=0.0, omega=0.0))

    def stop(self) -> None:
        with self._lock:
            if self._controller is not None and self._controller.base_bus is not None:
                try:
                    self._controller.stop()
                except Exception as exc:
                    print(f"[real_base] 停止失败: {exc}", flush=True)

    def disconnect(self) -> None:
        with self._lock:
            if self._controller is not None:
                try:
                    self._controller.disconnect()
                except Exception as exc:
                    print(f"[real_base] 断开失败: {exc}", flush=True)
                finally:
                    self._controller = None

    def move_for_duration(self, vx: float, duration: float, vw: float = 0.0) -> None:
        if not self.enabled:
            return
        if abs(float(vw)) > 1e-6:
            print("[real_base] 当前真实底盘只接前后移动，跳过含转向的 move_duration", flush=True)
            return
        if abs(float(vx)) < 1e-6:
            return
        with self._lock:
            if not self._ensure_connected():
                return
            real_vx = self._sim_to_real_speed(vx)
            print(
                f"[real_base] move_duration: sim_vx={vx:.3f}, real_vx={real_vx:.3f}m/s, duration={duration:.2f}s",
                flush=True,
            )
            try:
                self._controller.set_velocity_raw(vx=real_vx, vy=0.0, omega=0.0)
                time.sleep(max(0.0, float(duration)))
            except Exception as exc:
                print(f"[real_base] move_duration 失败: {exc}", flush=True)
            finally:
                self.disconnect()


_BRIDGE = RealBaseBridge()


def real_base_enabled() -> bool:
    return _BRIDGE.is_enabled()


def start_move_duration(vx: float, duration: float, vw: float = 0.0) -> threading.Thread | None:
    if not _BRIDGE.is_enabled():
        return None
    thread = threading.Thread(
        target=_BRIDGE.move_for_duration,
        args=(float(vx), float(duration), float(vw)),
        daemon=True,
    )
    thread.start()
    return thread


def stop_real_base() -> None:
    _BRIDGE.stop()


def disconnect_real_base() -> None:
    _BRIDGE.disconnect()
