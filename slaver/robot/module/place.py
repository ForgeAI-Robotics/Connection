"""
放置/释放控制模块。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from robot_api.client import (
    place_object as _place_object,
    get_scene,
)

_SERVE_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'serve'))
if _SERVE_PATH not in sys.path:
    sys.path.insert(0, _SERVE_PATH)
from scene.scene_memory import move_object as _move_object, coords_to_waypoint as _coords_to_waypoint


def _place_position_for_fixture(target_name, fixture):
    fpos = fixture["pos"]
    fsize = fixture["size"]
    fixture_type = str(fixture.get("type", "")).lower()
    target_lower = target_name.lower()

    if "sink" in target_lower or "sink" in fixture_type:
        return [fpos[0] + 0.20, fpos[1], fpos[2] - 0.04]

    surface_z = fpos[2] + fsize[2] / 2
    return [fpos[0], fpos[1], surface_z + 0.05]


def register_tools(mcp):

    @mcp.tool()
    async def place_on_top(obj_name: str, target_name: str) -> str:
        """将物体放置在目标物体上面。

        Args:
            obj_name: 要放置的物体名称（如 "mug"）
            target_name: 目标物体名称（如 "table"、"plate"）

        Returns:
            放置结果，成功或失败信息。
        """
        print(f"[place] 将 '{obj_name}' 放在 '{target_name}' 上面...", file=sys.stderr)

        # 优先用场景坐标解析(MuJoCo)，找不到则透传名称(ALFWorld等符号后端)
        place_pos = None
        scene = get_scene()
        if scene and "error" not in scene:
            objects = scene.get("objects", {})
            if target_name in objects and objects[target_name].get("pos") is not None:
                target_pos = objects[target_name]["pos"]
                place_pos = [target_pos[0], target_pos[1], target_pos[2] + 0.05]
            else:
                fixtures = scene.get("fixtures", {})
                matched = next((f for f in fixtures if target_name in f or f in target_name), None)
                if matched and fixtures[matched].get("pos") is not None:
                    place_pos = _place_position_for_fixture(target_name, fixtures[matched])

        print(f"[place] 诊断: target_name={target_name!r} → 解析落点 place_pos={place_pos} "
              f"(None 表示没匹配到 fixture,会透传名称)", file=sys.stderr)
        result = _place_object(obj_name, place_pos if place_pos is not None else target_name)

        if result.get("success"):
            response = result.get("result", f"成功将 {obj_name} 放在 {target_name} 上面")
            print(f"[place] ✓ {response}", file=sys.stderr)
            try:
                wp_name = _coords_to_waypoint(place_pos)
                _move_object(obj_name, wp_name)
            except Exception as e:
                print(f"[place] 记忆更新失败: {e}", file=sys.stderr)
            return json.dumps([response, {"_status": "success"}])
        else:
            msg = result.get("result", f"放置失败，请重试。")
            print(f"[place] ✗ {msg}", file=sys.stderr)
            return json.dumps([msg, {"_status": "failure"}])

    @mcp.tool()
    async def place_object(obj_name: str, x: float, y: float, z: float) -> str:
        """将物体放置到指定坐标位置。

        Args:
            obj_name: 要放置的物体名称
            x: 目标位置 x 坐标
            y: 目标位置 y 坐标
            z: 目标位置 z 坐标

        Returns:
            放置结果，成功或失败信息。
        """
        target_pos = [x, y, z]
        print(f"[place] 将 '{obj_name}' 放到 {target_pos}...", file=sys.stderr)

        result = _place_object(obj_name, target_pos)

        if result.get("success"):
            response = result.get("result", f"成功将 {obj_name} 放到目标位置")
            print(f"[place] ✓ {response}", file=sys.stderr)
            try:
                wp_name = _coords_to_waypoint(target_pos)
                _move_object(obj_name, wp_name)
            except Exception as e:
                print(f"[place] 记忆更新失败: {e}", file=sys.stderr)
            return json.dumps([response, {"_status": "success"}])
        else:
            msg = result.get("result", f"放置失败，请重试。")
            print(f"[place] ✗ {msg}", file=sys.stderr)
            return json.dumps([msg, {"_status": "failure"}])

    print("[place.py] 放置模块已注册 (robot_api)", file=sys.stderr)
