"""
waypoint_manager.py - 工作点管理器
根据目标物体名或坐标，找到最近的导航工作点
"""

import os
import sys
import math
import json
import numpy as np
import yaml

from robot_api.client import get_fixtures, get_objects

try:
    from serve.scene.scene_memory import get_object_coords, get_object_location
except Exception:
    def get_object_coords(obj_name):
        return None

    def get_object_location(obj_name):
        return None

_WAYPOINTS_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'serve', 'scene', 'config', 'waypoints.yaml'
)
_NAV2_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'nav2', 'config.yaml'
)
_FREE_POINTS_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'nav2', 'maps', 'free_points.json'
)

_waypoints_cache = None
_free_points_cache = None


def load_waypoints():
    global _waypoints_cache
    if _waypoints_cache is not None:
        return _waypoints_cache
    with open(_WAYPOINTS_PATH, 'r') as f:
        data = yaml.safe_load(f)
    _waypoints_cache = data['waypoints']
    print(f"[waypoint] 加载了 {len(_waypoints_cache)} 个工作点", file=sys.stderr)
    return _waypoints_cache


def load_free_points():
    global _free_points_cache
    if _free_points_cache is not None:
        return _free_points_cache
    with open(_FREE_POINTS_PATH, 'r') as f:
        data = json.load(f)
    _free_points_cache = [
        {
            "name": p['name'],
            "pos": [p['x'], p['y']],
            "serves": [],
        }
        for p in data.get('points', [])
    ]
    print(f"[waypoint] 加载了 {len(_free_points_cache)} 个可通行点", file=sys.stderr)
    return _free_points_cache


def load_reach_limits():
    try:
        with open(_NAV2_CONFIG_PATH, 'r') as f:
            cfg = yaml.safe_load(f)
        wp_cfg = cfg.get('waypoints', {})
        return wp_cfg.get('min_dist', 0.1), wp_cfg.get('max_reach', 1.0)
    except Exception as e:
        print(f"[waypoint] 读取工作点距离配置失败，使用默认值: {e}", file=sys.stderr)
        return 0.1, 1.0


def _load_perception_config():
    """读取感知模式配置"""
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get('perception', {}).get('use_realtime_coords', True)


def get_object_pos(obj_name):
    """查询物体坐标：记忆模式用场景状态，实时模式调API"""
    use_realtime = _load_perception_config()

    if not use_realtime:
        # 记忆模式：直接跳到记忆查询，不调任何 API
        try:
            waypoints = load_waypoints()
            FIXTURE_KEYWORDS = ['counter', 'island', 'sink', 'stove', 'floor', 'fridge', 'microwave', 'oven']
            simple_name = None
            for kw in FIXTURE_KEYWORDS:
                if kw in obj_name:
                    simple_name = kw
                    break
            if simple_name:
                for wp in waypoints:
                    serves = wp.get('serves') or []
                    if any(simple_name in s or s in simple_name for s in serves):
                        coords = wp['pos'][:2]
                        print(f"[waypoint] 记忆模式: {obj_name} 是家具({simple_name}) → {wp['name']} @ {coords}", file=sys.stderr)
                        return [coords[0], coords[1], 0.9]
            coords = get_object_coords(obj_name)
            if coords:
                location = get_object_location(obj_name)
                print(f"[waypoint] 记忆模式: {obj_name} 在工作点 {location} @ {coords[:2]}", file=sys.stderr)
                return coords
        except Exception as e:
            print(f"[waypoint] 记忆模式查询失败: {e}", file=sys.stderr)
        return None

    # 实时模式：调 API
    try:
        objects = get_objects()
        if isinstance(objects, dict) and objects.get("success") is False:
            objects = {}
        if isinstance(objects, dict) and "objects" in objects:
            objects = objects["objects"]
        if isinstance(objects, dict):
            if obj_name in objects:
                return objects[obj_name]['pos']
            candidates = [k for k in objects if obj_name in k or k in obj_name]
            if candidates:
                return objects[candidates[0]]['pos']

        fixtures = get_fixtures()
        if isinstance(fixtures, dict) and fixtures.get("success") is False:
            fixtures = {}
        if isinstance(fixtures, dict) and "fixtures" in fixtures:
            fixtures = fixtures["fixtures"]
        if isinstance(fixtures, dict):
            if obj_name in fixtures:
                return fixtures[obj_name]['pos']
            candidates = [k for k in fixtures if obj_name in k and 'main' in k]
            if not candidates:
                candidates = [k for k in fixtures if obj_name in k]
            if candidates:
                return fixtures[candidates[0]]['pos']
    except Exception as e:
        print(f"[waypoint] 查询物体位置失败: {e}", file=sys.stderr)
    return None


def find_waypoint(target):
    """
    根据目标名称或坐标字符串，找最近的工作点并用预存的朝向

    Args:
        target: 物体名称（如 "apple"）或坐标字符串（如 "(1.5, -0.3)"）

    Returns:
        dict: {"name": ..., "x": ..., "y": ..., "yaw_deg": ...}
    """
    target_pos = None
    target_name = None

    # 判断是坐标还是名称
    try:
        cleaned = str(target).strip().strip("()")
        parts = [float(x.strip()) for x in cleaned.split(",")]
        if len(parts) >= 2:
            target_pos = parts[:3]
    except ValueError:
        target_name = str(target).strip()

    # 用名称查实时坐标
    if target_name and target_pos is None:
        pos = get_object_pos(target_name)
        if pos:
            target_pos = pos

    waypoints = load_waypoints()

    if target_pos is None:
        raise ValueError(f"无法确定目标 '{target}' 的位置，请确认 /objects 或 /fixtures 中存在该名称")

    tp = np.array(target_pos[:2])

    use_realtime = _load_perception_config()

    min_dist, max_reach = load_reach_limits()

    def within_reach(wp):
        dist = np.linalg.norm(np.array(wp['pos'][:2]) - tp)
        return min_dist <= dist <= max_reach

    if use_realtime and target_name:
        # 实时模式：先按 serves 缩小候选范围
        serving = [wp for wp in waypoints
                   if any(target_name in s or s in target_name
                          for s in wp.get('serves', []))]
        free_points = load_free_points()
        serving_reachable = [wp for wp in serving if within_reach(wp)]
        free_reachable = [wp for wp in free_points if within_reach(wp)]
        all_reachable = [wp for wp in waypoints if within_reach(wp)]
        candidates = serving_reachable or free_reachable or all_reachable or serving or waypoints
    else:
        # 记忆模式：物体位置动态变化，serves 不可靠，直接用全部按距离选
        candidates = waypoints

    if not candidates:
        candidates = waypoints

    # 按距离目标排序，选最近的
    candidates_sorted = sorted(
        candidates,
        key=lambda wp: np.linalg.norm(np.array(wp['pos'][:2]) - tp)
    )
    best = candidates_sorted[0]
    best_xy = best['pos'][:2]

    # 工作点生成器已经根据可达区域和目标家具方向写入 yaw_deg。
    # 这里必须使用预存角度，否则会出现 nav_011 应为 90° 却被重算成 0° 的问题。
    yaw_deg = float(best.get('yaw_deg', 0.0))

    print(
        f"[waypoint] 目标: '{target}' @ {tp.tolist()}, "
        f"工作点: {best['name']} @ {best_xy}, "
        f"朝向: {yaw_deg:.1f}°",
        file=sys.stderr
    )

    return {
        "name": best['name'],
        "x": best_xy[0],
        "y": best_xy[1],
        "yaw_deg": yaw_deg,
    }
