#!/usr/bin/env python3
"""
Bridge LLM grasp tool calls to the real arm grasp server.

The bridge is disabled by default. Enable it in serve_real/config.yaml:
    real_arm:
      enabled: 1
"""

import os
import socket
import threading
import time
from pathlib import Path


_SERVE_REAL_DIR = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _SERVE_REAL_DIR / "config.yaml"
_GRASP_COMMAND = 0xAA

_RESPONSE_CODES = {
    "00": "执行成功",
    "01": "指令格式错误",
    "02": "颜色参数错误",
    "03": "抓取脚本或相机不可用",
    "04": "抓取失败",
    "05": "未知错误",
}


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
        print(f"[real_arm] 读取配置失败 {_CONFIG_PATH}: {exc}", flush=True)
        return {}


def _env_or_config(env_name: str, config: dict, key: str, default):
    value = os.getenv(env_name)
    if value is not None:
        return value
    return config.get(key, default)


def _parse_command_byte(value) -> int:
    if value is None:
        return _GRASP_COMMAND
    if isinstance(value, int):
        command = value
    else:
        text = str(value).strip()
        command = int(text, 16) if text.lower().startswith("0x") else int(text)
    if command < 0 or command > 255:
        raise ValueError(f"command_byte 超出范围: {command}")
    return command


def _decode_failure(response: str) -> str:
    parts = response.split(":", 2)
    if len(parts) < 2:
        return response or "真实抓取失败"
    code = parts[1].strip().zfill(2)
    message = _RESPONSE_CODES.get(code, f"错误码: {parts[1]}")
    if len(parts) >= 3 and parts[2].strip():
        return f"{message}: {parts[2].strip()}"
    return message


class RealArmBridge:
    def __init__(self):
        config = (_load_config().get("real_arm") or {})
        self.enabled = _as_bool(
            _env_or_config("REAL_ARM_ENABLED", config, "enabled", 0),
            default=False,
        )
        self.host = str(_env_or_config("REAL_ARM_HOST", config, "host", "127.0.0.1"))
        self.port = int(_env_or_config("REAL_ARM_PORT", config, "port", 9999))
        self.timeout = float(_env_or_config("REAL_ARM_TIMEOUT", config, "timeout", 60))
        self.command_byte = _parse_command_byte(
            _env_or_config("REAL_ARM_COMMAND_BYTE", config, "command_byte", _GRASP_COMMAND)
        )
        self.fail_on_error = _as_bool(
            _env_or_config("REAL_ARM_FAIL_ON_ERROR", config, "fail_on_error", 1),
            default=True,
        )
        self._lock = threading.RLock()

    def is_enabled(self) -> bool:
        return self.enabled

    def should_fail_on_error(self) -> bool:
        return self.fail_on_error

    def trigger_grasp(self, obj_name: str | None = None) -> dict:
        if not self.enabled:
            return {
                "enabled": False,
                "sent": False,
                "success": None,
                "result": "real_arm disabled",
            }

        with self._lock:
            started_at = time.time()
            command = bytes([self.command_byte])
            target = obj_name or ""
            print(
                f"[real_arm] 发送抓取信号 target={target!r} host={self.host}:{self.port} "
                f"command=0x{self.command_byte:02X}",
                flush=True,
            )
            try:
                with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                    sock.settimeout(self.timeout)
                    sock.sendall(command)
                    response_data = sock.recv(1024)
            except socket.timeout:
                error = f"Socket连接超时（{self.timeout:g}秒）"
                print(f"[real_arm] {error}", flush=True)
                return self._error_result(error, started_at, sent=False)
            except ConnectionRefusedError:
                error = f"无法连接真实抓取服务器 {self.host}:{self.port}"
                print(f"[real_arm] {error}", flush=True)
                return self._error_result(error, started_at, sent=False)
            except Exception as exc:
                error = f"真实抓取通信错误: {exc}"
                print(f"[real_arm] {error}", flush=True)
                return self._error_result(error, started_at, sent=False)

            response = response_data.decode("utf-8", errors="replace").strip()
            elapsed = round(time.time() - started_at, 3)
            print(f"[real_arm] 收到响应: {response}", flush=True)

            if response.startswith("SUCCESS"):
                return {
                    "enabled": True,
                    "sent": True,
                    "success": True,
                    "response": response,
                    "host": self.host,
                    "port": self.port,
                    "elapsed_s": elapsed,
                }
            if response.startswith("FAILED") or not response:
                error = _decode_failure(response)
                return {
                    "enabled": True,
                    "sent": True,
                    "success": False,
                    "response": response,
                    "error": error,
                    "host": self.host,
                    "port": self.port,
                    "elapsed_s": elapsed,
                }

            return {
                "enabled": True,
                "sent": True,
                "success": True,
                "response": response,
                "warning": "unknown response treated as success",
                "host": self.host,
                "port": self.port,
                "elapsed_s": elapsed,
            }

    def _error_result(self, error: str, started_at: float, sent: bool) -> dict:
        return {
            "enabled": True,
            "sent": sent,
            "success": False,
            "error": error,
            "host": self.host,
            "port": self.port,
            "elapsed_s": round(time.time() - started_at, 3),
        }


_BRIDGE = RealArmBridge()


def real_arm_enabled() -> bool:
    return _BRIDGE.is_enabled()


def real_arm_fail_on_error() -> bool:
    return _BRIDGE.should_fail_on_error()


def trigger_real_grasp(obj_name: str | None = None) -> dict:
    return _BRIDGE.trigger_grasp(obj_name)
