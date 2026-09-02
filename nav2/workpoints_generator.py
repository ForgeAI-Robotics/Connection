"""
workpoints_generator.py
从 free_points.json 里自动选出覆盖所有物体/家具的最少工作点
运行：python nav2/workpoints_generator.py

输出:
  - serve/scene/config/waypoints.yaml  (给 waypoint_manager 使用)
  - maps/workpoints.json               (JSON 格式备份)
  - maps/workpoints_vis.png            (可视化图)
"""

import json
import math
import os
import re
import sys

import numpy as np
import yaml
from pathlib import Path
from PIL import Image, ImageDraw

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from robot_api.client import get_fixtures, get_objects

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
CONFIG_DIR = os.path.dirname(__file__)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_map_yaml(path):
    text = Path(path).read_text()
    image = re.search(r"^image:\s*(.+)$", text, re.M).group(1).strip()
    resolution = float(re.search(r"^resolution:\s*([-0-9.]+)$", text, re.M).group(1))
    origin_match = re.search(r"^origin:\s*\n-\s*([-0-9.]+)\n-\s*([-0-9.]+)", text, re.M)
    origin = (float(origin_match.group(1)), float(origin_match.group(2)))
    return image, resolution, origin


def read_pgm(path):
    with Path(path).open("rb") as f:
        if f.readline().strip() != b"P5":
            raise ValueError("Only binary PGM (P5) is supported")
        line = f.readline()
        while line.startswith(b"#"):
            line = f.readline()
        width, height = map(int, line.split())
        max_value = int(f.readline())
        data = f.read()
    if max_value != 255:
        raise ValueError(f"Unsupported PGM max value: {max_value}")
    return width, height, data


def get_scene_objects(fixtures_map):
    """拉取所有可操作物体和主要家具坐标"""
    targets = {}

    objects = get_objects()
    if isinstance(objects, dict) and objects.get("success") is False:
        objects = {}
    if isinstance(objects, dict) and "objects" in objects:
        objects = objects["objects"]
    if isinstance(objects, dict):
        for name, data in objects.items():
            if isinstance(data, dict) and "pos" in data:
                targets[name] = data['pos'][:2]

    fixture_data = get_fixtures()
    if isinstance(fixture_data, dict) and fixture_data.get("success") is False:
        fixture_data = {}
    if isinstance(fixture_data, dict) and "fixtures" in fixture_data:
        fixture_data = fixture_data["fixtures"]
    if isinstance(fixture_data, dict):
        for simple_name, fixture_key in fixtures_map.items():
            if fixture_key in fixture_data:
                targets[simple_name] = fixture_data[fixture_key]['pos'][:2]

    return targets


def load_free_points(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data['points']


def _target_groups(targets, merge_target_distance):
    """把相互接近的目标合并成组，使用传递闭包。"""
    names = list(targets.keys())
    parent = {name: name for name in names}

    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    if merge_target_distance > 0:
        for i, a in enumerate(names):
            apos = np.array(targets[a])
            for b in names[i + 1:]:
                bpos = np.array(targets[b])
                if np.linalg.norm(apos - bpos) <= merge_target_distance:
                    union(a, b)

    groups = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)
    return [sorted(group) for group in groups.values()]


def _select_best_point_for_group(free_points, targets, group, max_reach, min_dist):
    candidates = []
    for p in free_points:
        pp = np.array([p['x'], p['y']])
        distances = [float(np.linalg.norm(pp - np.array(targets[name]))) for name in group]
        if all(min_dist <= dist <= max_reach for dist in distances):
            candidates.append((float(np.mean(distances)), max(distances), p))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]['name']))
    return candidates[0][2]


def find_covering_waypoints(free_points, targets, max_reach, min_dist, merge_target_distance=0.0):
    """为每个目标选最近工作点；相近目标合并后选平均距离最小工作点。"""
    selected = []
    uncovered = []

    for group in _target_groups(targets, merge_target_distance):
        p = _select_best_point_for_group(free_points, targets, group, max_reach, min_dist)
        if p is None:
            uncovered.extend(group)
            continue

        target_xy = np.mean([np.array(targets[name]) for name in group], axis=0).tolist()
        selected.append({
            'name': p['name'],
            'point': p,
            'serves': group,
            'target_xy': target_xy,
        })
        print(f"选择工作点 {p['name']} @ [{p['x']}, {p['y']}], 覆盖: {group}")

    if uncovered:
        print(f"[警告] 以下目标无法被任何工作点覆盖: {set(uncovered)}")

    return selected


FIXTURE_PRIORITY = ["counter", "island", "stove", "sink"]


def snap_to_90(yaw_deg):
    """把角度吸附到最近的 90 度方向"""
    while yaw_deg > 180:
        yaw_deg -= 360
    while yaw_deg <= -180:
        yaw_deg += 360
    angles = [-180, -90, 0, 90, 180]
    return float(min(angles, key=lambda a: abs(a - yaw_deg)))


def pick_primary_target(serves, targets):
    """从 serves 列表中选最主要的目标（家具优先）"""
    for fixture in FIXTURE_PRIORITY:
        if fixture in serves and fixture in targets:
            return fixture
    for name in serves:
        if name in targets:
            return name
    return None


def compute_yaw(from_xy, to_xy):
    dx = to_xy[0] - from_xy[0]
    dy = to_xy[1] - from_xy[1]
    # 工作点朝向使用公开的 base_link/map 坐标系，直接让 base_link 正面朝向目标。
    return math.degrees(math.atan2(dy, dx))


def add_fixture_waypoints(selected, free_points, targets, fixture_names, max_reach, min_dist):
    """为指定家具强制添加最近的工作点"""
    existing_names = {item['name'] for item in selected}

    for fname in fixture_names:
        if fname not in targets:
            continue
        fpos = np.array(targets[fname])

        candidates = []
        for p in free_points:
            pp = np.array([p['x'], p['y']])
            dist = np.linalg.norm(pp - fpos)
            if min_dist <= dist <= max_reach:
                candidates.append((dist, p))

        if not candidates:
            print(f"[警告] 找不到 {fname} 附近的工作点")
            continue

        candidates.sort(key=lambda x: x[0])
        best = candidates[0][1]

        if best['name'] in existing_names:
            print(f"{fname} 已被现有工作点 {best['name']} 覆盖，跳过")
            continue

        selected.append({
            'name': best['name'],
            'point': best,
            'serves': [fname],
        })
        existing_names.add(best['name'])
        print(f"为 {fname} 补充工作点 {best['name']} @ [{best['x']}, {best['y']}]")

    return selected


def build_waypoints(selected, targets):
    """构建工作点列表"""
    waypoints = []
    for item in selected:
        p = item['point']
        serves = item['serves']

        # 取目标组中心朝向，snap 到 90°
        primary = pick_primary_target(serves, targets)
        target_xy = item.get('target_xy')
        if target_xy is not None:
            raw_yaw = compute_yaw([p['x'], p['y']], target_xy)
            yaw_deg = snap_to_90(raw_yaw)
        elif primary:
            raw_yaw = compute_yaw([p['x'], p['y']], targets[primary])
            yaw_deg = snap_to_90(raw_yaw)
        else:
            yaw_deg = 0.0

        waypoints.append({
            'name': item['name'],
            'pos': [p['x'], p['y']],
            'yaw_deg': round(yaw_deg, 1),
            'serves': serves,
        })
    return waypoints


def save_yaml(waypoints, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding="utf-8") as f:
        yaml.dump({'waypoints': waypoints}, f, allow_unicode=True, default_flow_style=False)
    print(f"[workpoints] YAML -> {output_path}")


def save_json(waypoints, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding="utf-8") as f:
        json.dump({'waypoints': waypoints}, f, ensure_ascii=False, indent=2)
    print(f"[workpoints] JSON -> {output_path}")


def generate_visualization(free_points_path, waypoints, targets, output_path):
    """可视化：灰度地图 + 蓝色free_points + 红色目标物体 + 绿色工作点"""
    with open(free_points_path, "r", encoding="utf-8") as f:
        fp_data = json.load(f)
    map_file = fp_data.get("map", "maps/kitchen_map.yaml")
    if not os.path.isabs(map_file):
        # free_points.json 里的 map 路径可能相对 nav2/、项目根或运行目录,取第一个存在的
        for _base in (CONFIG_DIR, PROJECT_ROOT, os.getcwd()):
            _cand = os.path.join(_base, map_file)
            if os.path.isfile(_cand):
                map_file = _cand
                break

    image_name, resolution, origin = read_map_yaml(map_file)
    map_dir = Path(map_file).resolve().parent
    width, height, data = read_pgm(map_dir / image_name)

    arr = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
    vis = np.stack([arr] * 3, axis=-1).copy()

    # 蓝色：free_points
    for pt in fp_data['points']:
        px = int((pt['x'] - origin[0]) / resolution)
        py = int((origin[1] + height * resolution - pt['y']) / resolution)
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                ny, nx = py + dy, px + dx
                if 0 <= nx < width and 0 <= ny < height:
                    vis[ny, nx] = [60, 100, 255]

    # 红色：目标物体/家具
    for name, pos in targets.items():
        px = int((pos[0] - origin[0]) / resolution)
        py = int((origin[1] + height * resolution - pos[1]) / resolution)
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                ny, nx = py + dy, px + dx
                if 0 <= nx < width and 0 <= ny < height:
                    vis[ny, nx] = [255, 50, 50]

    # 绿色：工作点
    for wp in waypoints:
        px = int((wp['pos'][0] - origin[0]) / resolution)
        py = int((origin[1] + height * resolution - wp['pos'][1]) / resolution)
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                ny, nx = py + dy, px + dx
                if 0 <= nx < width and 0 <= ny < height:
                    vis[ny, nx] = [0, 220, 0]

    # 放大 4 倍 + 标注文字
    scale = 4
    vis_big = np.repeat(np.repeat(vis, scale, axis=0), scale, axis=1)
    vis_img = Image.fromarray(vis_big)
    draw = ImageDraw.Draw(vis_img)

    for wp in waypoints:
        px = int((wp['pos'][0] - origin[0]) / resolution) * scale
        py = int((origin[1] + height * resolution - wp['pos'][1]) / resolution) * scale
        label = f"{wp['name']}[{','.join(wp['serves'][:3])}]"
        draw.text((px + 8, py - 8), label, fill=(0, 255, 0))

    for name, pos in targets.items():
        px = int((pos[0] - origin[0]) / resolution) * scale
        py = int((origin[1] + height * resolution - pos[1]) / resolution) * scale
        draw.text((px + 8, py + 8), name, fill=(255, 80, 80))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    vis_img.save(output_path)
    print(f"[vis] 可视化图 -> {output_path} ({vis_img.size[0]}x{vis_img.size[1]})")
    print(f"[vis] 图例: 白=可通行 黑=障碍 蓝=可通行采样点 绿=工作点 红=目标物体")


if __name__ == "__main__":
    cfg = load_config()
    wp_cfg = cfg.get("waypoints", {})

    free_points_path = os.path.join(CONFIG_DIR, wp_cfg.get("free_points_path", "maps/free_points.json"))
    output_yaml = os.path.join(CONFIG_DIR, wp_cfg.get("output_path", "../serve/scene/config/waypoints.yaml"))
    max_reach = wp_cfg.get("max_reach", 1.0)
    min_dist = wp_cfg.get("min_dist", 0.3)
    merge_target_distance = wp_cfg.get("merge_target_distance", 0.0)
    must_cover = wp_cfg.get("must_cover", ["counter", "island", "stove", "sink"])
    fixtures_map = wp_cfg.get("fixtures", {})

    maps_dir = os.path.join(CONFIG_DIR, "maps")
    output_json = os.path.join(maps_dir, "workpoints.json")
    output_vis = os.path.join(maps_dir, "workpoints_vis.png")

    print("拉取场景物体...")
    targets = get_scene_objects(fixtures_map)
    print(f"找到 {len(targets)} 个目标: {list(targets.keys())}")

    print("\n加载 free_points...")
    free_points = load_free_points(free_points_path)
    print(f"共 {len(free_points)} 个可通行点")

    print("\n计算最优工作点覆盖...")
    selected = find_covering_waypoints(
        free_points,
        targets,
        max_reach,
        min_dist,
        merge_target_distance=merge_target_distance,
    )
    selected = add_fixture_waypoints(selected, free_points, targets, must_cover, max_reach, min_dist)

    print("\n保存工作点...")
    waypoints = build_waypoints(selected, targets)
    save_yaml(waypoints, output_yaml)
    save_json(waypoints, output_json)
    print(f"\n共选出 {len(waypoints)} 个工作点")

    print("\n生成可视化图...")
    generate_visualization(free_points_path, waypoints, targets, output_vis)
