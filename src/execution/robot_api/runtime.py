"""Internal routing from semantic robot actions to configured backends."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import BackendConfig, load_robot_api_config


NO_RENDER_BACKENDS = {"desk"}
STATE_ENDPOINTS = {
    "scene": ("GET", "/scene"),
    "scene_state": ("GET", "/scene_state"),  # slaver 的 belief/搜索需要;robocasa 后端有,motrixsim 后端暂返 503
    "objects": ("GET", "/objects"),
    "fixtures": ("GET", "/fixtures"),
    "robot_state": ("GET", "/scene"),
    "base_status": ("GET", "/base_status"),
    "arm_status": ("GET", "/status"),
    "map_data": ("GET", "/map_data"),
    "image": ("POST", "/screenshot"),
}

ACTION_ENDPOINTS = {
    "grasp_object": "/grasp",
    "place_object": "/place",
    "navigate_to": "/nav",
    "inspect_target": "/inspection",
    "move_forward": "/move_duration",
    "rotate": "/move_duration",
}

SPEEDS = {
    "slow": 0.25,
    "normal": 0.5,
    "fast": 0.8,
}


class RobotRuntime:
    def __init__(self):
        self.config = load_robot_api_config()

    def set_backend_url(self, url: str) -> None:
        updated = []
        active_backend = self.config.active_backend
        for backend in self.config.backends:
            if (
                backend.name == active_backend
                or (active_backend is None and backend.name in ("mujoco", "mujoco_3dgs"))
            ):
                backend = BackendConfig(**{**backend.__dict__, "url": str(url).rstrip("/")})
            updated.append(backend)
        self.config = type(self.config)(
            backends=updated,
            active_backend=active_backend,
            navigation=self.config.navigation,
            observation_backend=self.config.observation_backend,
        )

    def get_state(self, name: str, params: dict[str, Any] | None = None):
        if name not in STATE_ENDPOINTS:
            return {"success": False, "result": f"未知状态接口: {name}"}
        params = params or {}
        if name == "image":
            return self._capture_image(params)
        method, endpoint = STATE_ENDPOINTS[name]
        failures = []
        for backend in self.config.state_backends():
            payload_endpoint = endpoint
            data = None
            if name == "map_data" and params:
                payload_endpoint = f"{endpoint}?{urllib.parse.urlencode(params)}"
            elif method == "POST":
                data = self._state_payload(name, params)
            result = self._http(backend, method, payload_endpoint, data=data)
            if result.get("success") is not False:
                return result
            failures.append((backend, result))
            if backend.required:
                return result
        return self._last_failure_or_disabled("state", failures)

    def _vision_backends(self) -> list[BackendConfig]:
        if self.config.observation_backend is not None:
            return [b for b in self.config.backends
                    if b.name == self.config.observation_backend and b.enabled and b.url]
        enabled = [
            backend
            for backend in self.config.backends
            if backend.enabled
            and backend.url
            and backend.name not in NO_RENDER_BACKENDS
        ]
        rank = {"mujoco_3dgs": 0, "mujoco": 1}
        enabled.sort(key=lambda backend: rank.get(backend.name, 10))
        return enabled

    def _capture_image(self, params: dict[str, Any]):
        failures = []
        camera_name = str(params.get("camera_name") or "").strip()
        payload = self._state_payload("image", params)
        payload.setdefault("width", 640)
        payload.setdefault("height", 480)
        for backend in self._vision_backends():
            result = self._http(backend, "POST", "/screenshot", payload)
            if result.get("success") is not False and result.get("image"):
                result["_backend"] = backend.name
                return result
            failures.append(result)
            latest = self._latest_frame(backend, camera_name)
            if latest.get("success") is not False and latest.get("image"):
                latest["_backend"] = backend.name
                return latest
            failures.append(latest)
        if failures:
            return failures[-1]
        return {"success": False, "result": "没有可用的渲染后端，无法截图"}

    def _latest_frame(self, backend: BackendConfig, camera_name: str) -> dict[str, Any]:
        endpoint = "/camera/latest"
        if camera_name:
            endpoint += f"?camera={urllib.parse.quote(camera_name)}"
        url = f"{backend.url}{endpoint}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=min(20.0, backend.timeout)) as resp:
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                body = resp.read()
            if not body:
                return {"success": False, "result": f"{backend.name} 无画面"}
            if "json" in content_type:
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict) and payload.get("image"):
                    payload["success"] = True
                    return payload
                return {"success": False, "result": f"{backend.name} latest 无图像"}
            if content_type.startswith("image/") or body[:2] == b"\xff\xd8":
                import base64

                return {
                    "success": True,
                    "image": base64.b64encode(body).decode("ascii"),
                }
            return {"success": False, "result": f"{backend.name} 不支持的画面格式"}
        except Exception as exc:
            return {"success": False, "result": f"{backend.name} 取帧失败: {exc}"}

    def execute(self, action: str, args: dict[str, Any]):
        if action not in ACTION_ENDPOINTS:
            return {"success": False, "result": f"未知动作接口: {action}"}
        if action == "navigate_to":
            nav_backend = self.config.navigation_backend()
            if nav_backend is not None:
                endpoint, payload = self._nav_call(nav_backend, args)
                result = self._http(nav_backend, "POST", endpoint, payload)
                result["_backend"] = nav_backend.name
                result["_required"] = nav_backend.required
                return self._merge([result])

        if not self.config.action_backends():
            return {"success": False, "result": "当前执行模块没有可用后端"}
        results = []
        for backend in self.config.action_backends():
            if backend.name == "real":
                result = self._real(action, args)
            elif action == "navigate_to":
                # 导航按后端重路由(同 nav-backend 分支):robocasa→/nav,3dgs→/move_to
                endpoint, payload = self._nav_call(backend, args)
                result = self._http(backend, "POST", endpoint, payload)
            else:
                result = self._http(backend, "POST", ACTION_ENDPOINTS[action], self._action_payload(action, args))
            result["_backend"] = backend.name
            result["_required"] = backend.required
            results.append(result)
        if not results:
            return {"success": False, "result": "没有启用动作后端"}
        return self._merge(results)

    def _http(self, backend: BackendConfig, method: str, endpoint: str, data=None):
        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{backend.url}{endpoint}"
        try:
            if method == "POST":
                req = urllib.request.Request(
                    url,
                    data=json.dumps(data or {}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            else:
                req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=backend.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            return {"success": False, "result": f"{backend.name} 连接失败: {exc}"}
        except Exception as exc:
            return {"success": False, "result": f"{backend.name} 请求错误: {exc}"}

    def _state_payload(self, name: str, params: dict[str, Any]):
        if name == "image":
            payload = {"width": 640, "height": 480}
            if params.get("camera_name"):
                payload["camera_name"] = params["camera_name"]
            return payload
        return params

    def _action_payload(self, action: str, args: dict[str, Any]):
        if action == "grasp_object":
            payload = {
                "obj_name": args["object_name"],
                "snap_threshold": 0.15,
            }
            if args.get("mode"):
                payload["mode"] = args["mode"]
            return payload
        if action == "place_object":
            return {
                "obj_name": args["object_name"],
                "target": args["target"],
                "snap_threshold": 0.15,
            }
        if action == "navigate_to":
            return self._navigation_payload(args["target"], args.get("yaw"))
        if action == "inspect_target":
            return dict(args)
        if action == "move_forward":
            return {
                "vx": self._speed(args.get("speed", 0.5)),
                "vw": 0.0,
                "duration": float(args.get("duration", 1.0)),
            }
        if action == "rotate":
            direction = str(args.get("direction") or "left").lower()
            sign = -1.0 if direction in {"right", "clockwise", "cw", "右", "右转"} else 1.0
            return {
                "vx": 0.0,
                "vw": sign * self._speed(args.get("speed", 0.5)),
                "duration": float(args.get("duration", 1.0)),
            }
        return args

    def _navigation_payload(self, target, yaw):
        if isinstance(target, dict):
            payload = {"x": target["x"], "y": target["y"]}
            yaw_value = target.get("yaw", yaw)
            # DREAM Agent HTTP transaction metadata.  Ordinary backends ignore these
            # because callers only provide them for the dream backend.
            for key in (
                "command_id", "task_id", "target_id", "leg_index", "frame_id",
                "motion_mode", "require_final_orientation", "yaw_unit", "route_phase",
            ):
                if key in target and target[key] is not None:
                    payload[key] = target[key]
        elif isinstance(target, (list, tuple)):
            payload = {"x": target[0], "y": target[1]}
            yaw_value = target[2] if len(target) > 2 else yaw
        else:
            payload = {"target": str(target)}
            yaw_value = yaw
        if yaw_value is not None:
            payload["target_yaw"] = yaw_value
        return payload

    def _nav_call(self, backend, args):
        """底盘导航端点按后端选：3DGS/MotrixSim 使用 /move_to（target=[x,y]），
        RoboCasa 等用 /nav。坐标目标才重路由；名称目标（ALFWorld
        等符号后端)仍走 /nav 透传。/move_to 只接位置、不控 yaw(如需朝向后续用 /move_duration 补)。"""
        std = self._navigation_payload(args["target"], args.get("yaw"))
        name = (backend.name or "").lower()
        is_move_to = "3dgs" in name or "motrix" in name
        if is_move_to and "x" in std:
            return "/move_to", {"target": [std["x"], std["y"]]}
        return ACTION_ENDPOINTS["navigate_to"], std

    def _real(self, action: str, args: dict[str, Any]):
        if action == "grasp_object":
            return self._real_grasp(args.get("object_name"))
        if action == "move_forward":
            payload = self._action_payload(action, args)
            return self._real_base(payload)
        if action == "rotate":
            payload = self._action_payload(action, args)
            return self._real_base(payload)
        return {"success": True, "skipped": True, "result": f"real 暂不处理 {action}"}

    def _real_grasp(self, object_name: str | None):
        try:
            from extensions.serve_real.bridge.arm import (
                real_arm_fail_on_error,
                trigger_real_grasp,
            )
        except Exception as exc:
            return {"success": False, "result": f"真实机械臂桥接导入失败: {exc}"}

        result = trigger_real_grasp(object_name)
        if result.get("success") is True:
            return {"success": True, "result": f"真实抓取成功: {object_name}", "detail": result}
        if result.get("success") is None:
            return {"success": True, "skipped": True, "result": "真实机械臂未启用", "detail": result}

        message = result.get("error") or result.get("result") or result.get("response") or "真实抓取失败"
        if real_arm_fail_on_error():
            return {"success": False, "result": f"真实抓取失败: {message}", "detail": result}
        return {"success": True, "warning": f"真实抓取失败但未阻断: {message}", "detail": result}

    @staticmethod
    def _real_base(payload: dict[str, Any]):
        try:
            from extensions.serve_real.bridge.base import start_move_duration

            start_move_duration(
                float(payload.get("vx", 0.0)),
                float(payload.get("duration", 0.0)),
                float(payload.get("vw", 0.0)),
            )
            return {"success": True, "result": "真实底盘指令已发送"}
        except Exception as exc:
            return {"success": False, "result": f"真实底盘通信失败: {exc}"}

    @staticmethod
    def _speed(value) -> float:
        if isinstance(value, str):
            return SPEEDS.get(value.lower(), 0.5)
        return max(-1.0, min(1.0, float(value)))

    @staticmethod
    def _last_failure_or_disabled(kind: str, failures):
        if failures:
            return failures[-1][1]
        return {"success": False, "result": f"没有启用 {kind} 后端"}

    @staticmethod
    def _merge(results):
        handled = [r for r in results if not r.get("skipped")]
        summary = {
            r["_backend"]: {
                "success": r.get("success"),
                "result": r.get("result"),
                "skipped": r.get("skipped", False),
            }
            for r in results
        }
        if not handled:
            return {"success": True, "result": "动作未被任何后端处理", "backends": summary}
        required_failed = [
            r for r in handled if r.get("_required") and r.get("success") is False
        ]
        first_success = next((r for r in handled if r.get("success") is not False), handled[0])
        if required_failed:
            return {
                "success": False,
                "result": "；".join(r.get("result", "动作失败") for r in required_failed),
                "backends": summary,
            }
        response = {
            key: value
            for key, value in first_success.items()
            if key not in {"_backend", "_required"}
        }
        response["success"] = True
        response.setdefault("result", "动作完成")
        response["backends"] = summary
        return response
