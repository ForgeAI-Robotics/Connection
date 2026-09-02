"""
map_generator.py - 从统一机器人后端生成 Nav2 占据地图

两种模式:
  1. 运行时模式（推荐）: 调用 robot_api /map_data，从当前后端读取实际障碍物
  2. 静态模式: 从 layout.yaml 解析（不够准确，仅作 fallback）

使用:
    # 运行时模式（机器人后端必须已启动）
    python map_generator.py --from-sim

    # 静态模式
    python map_generator.py --layout serve/scene/config/layout.yaml
"""

import os
import sys
import argparse
import yaml
import numpy as np
from PIL import Image, ImageDraw

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from robot_api.client import get_base_status, get_map_data

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
CONFIG_DIR = os.path.dirname(__file__)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# 默认地图参数；运行时会被 config.yaml 覆盖。
_cfg = load_config()
_map_cfg = _cfg.get("map", {})

MAP_X_MIN, MAP_X_MAX = -1.0, 8.0
MAP_Y_MIN, MAP_Y_MAX = -6.0, 1.0
RESOLUTION = _map_cfg.get("resolution", 0.05)

MAP_W = int((MAP_X_MAX - MAP_X_MIN) / RESOLUTION)
MAP_H = int((MAP_Y_MAX - MAP_Y_MIN) / RESOLUTION)

FREE = 254
OCCUPIED = 0


def set_map_bounds(x_min, x_max, y_min, y_max):
    global MAP_X_MIN, MAP_X_MAX, MAP_Y_MIN, MAP_Y_MAX, MAP_W, MAP_H
    MAP_X_MIN, MAP_X_MAX = float(x_min), float(x_max)
    MAP_Y_MIN, MAP_Y_MAX = float(y_min), float(y_max)
    MAP_W = max(1, int(np.ceil((MAP_X_MAX - MAP_X_MIN) / RESOLUTION)))
    MAP_H = max(1, int(np.ceil((MAP_Y_MAX - MAP_Y_MIN) / RESOLUTION)))


def world_to_pixel(x, y):
    px = int((x - MAP_X_MIN) / RESOLUTION)
    py = int((MAP_Y_MAX - y) / RESOLUTION)
    return max(0, min(px, MAP_W - 1)), max(0, min(py, MAP_H - 1))


def draw_rect(draw, cx, cy, size_x, size_y, fill=OCCUPIED):
    x1, y1 = world_to_pixel(cx - size_x / 2, cy + size_y / 2)
    x2, y2 = world_to_pixel(cx + size_x / 2, cy - size_y / 2)
    draw.rectangle([x1, y1, x2, y2], fill=fill)


def draw_circle(draw, cx, cy, radius, fill):
    x1, y1 = world_to_pixel(cx - radius, cy + radius)
    x2, y2 = world_to_pixel(cx + radius, cy - radius)
    draw.ellipse([x1, y1, x2, y2], fill=fill)


def draw_rotated_rect(draw, cx, cy, size_x, size_y, yaw, fill=OCCUPIED):
    hx, hy = size_x / 2, size_y / 2
    c, s = np.cos(yaw), np.sin(yaw)
    corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    pts = []
    for lx, ly in corners:
        wx = cx + lx * c - ly * s
        wy = cy + lx * s + ly * c
        pts.append(world_to_pixel(wx, wy))
    draw.polygon(pts, fill=fill)


def geom_yaw(obs):
    xmat = obs.get("xmat")
    if not xmat or len(xmat) < 4:
        return 0.0
    return float(np.arctan2(xmat[3], xmat[0]))


def shrink_extent(length, shrink_margin):
    return max(0.02, length - 2 * shrink_margin)


def draw_obstacle(draw, obs, shrink_margin=0.0):
    """Draw a MuJoCo geom footprint into the occupancy image."""
    cx, cy = obs["pos"][0], obs["pos"][1]
    size = obs["size"]
    geom_type = obs.get("type")

    # MuJoCo geom_size is half extents for boxes and radius/half-length for
    # common round primitives. Convert to a conservative 2D footprint.
    if geom_type == 6:  # mjGEOM_BOX
        sx = shrink_extent(2 * size[0], shrink_margin)
        sy = shrink_extent(2 * size[1], shrink_margin)
        draw_rotated_rect(draw, cx, cy, sx, sy, geom_yaw(obs))
    elif geom_type in (2, 5):  # sphere or cylinder
        diameter = shrink_extent(2 * size[0], shrink_margin)
        draw_rect(draw, cx, cy, diameter, diameter)
    elif geom_type == 3:  # capsule
        sx = shrink_extent(2 * size[1] + 2 * size[0], shrink_margin)
        sy = shrink_extent(2 * size[0], shrink_margin)
        draw_rotated_rect(draw, cx, cy, sx, sy, geom_yaw(obs))
    else:
        sx = shrink_extent(max(2 * size[0], 0.05), shrink_margin)
        sy = shrink_extent(max(2 * size[1] if len(size) > 1 else sx, 0.05), shrink_margin)
        draw_rotated_rect(draw, cx, cy, sx, sy, geom_yaw(obs))


def inflate(img_array, radius_m):
    """膨胀障碍物"""
    from scipy.ndimage import binary_dilation
    radius_px = max(1, int(radius_m / RESOLUTION))
    mask = img_array < 128
    dilated = binary_dilation(mask, iterations=radius_px)
    result = np.full_like(img_array, FREE)
    result[dilated] = OCCUPIED
    result[mask] = OCCUPIED
    return result


# ============================================================
# 模式 1: 从机器人后端读取（推荐）
# ============================================================

def fetch_obstacles_from_sim():
    """从运行中的机器人后端获取障碍物列表"""
    result = get_map_data(timeout=5)
    if result and not (result.get("success") is False and "grid" not in result):
        return result
    if result:
        print(f"[map_generator] 无法连接机器人后端: {result.get('result')}", file=sys.stderr)
    else:
        print("[map_generator] 无法连接机器人后端", file=sys.stderr)
    return None


def _fetch_base_status():
    result = get_base_status()
    if result and not result.get("success") is False:
        return result
    return None


def fetch_base_status():
    """从运行中的后端获取机器人底座位置，用于自动扩展地图边界"""
    try:
        return _fetch_base_status()
    except Exception:
        return None


def bounds_from_layout(layout_path=None, padding=0.5):
    if layout_path is None:
        layout_path = os.path.join(
            os.path.dirname(__file__), "..", "serve", "scene", "config", "layout.yaml"
        )
    try:
        with open(layout_path, "r", encoding="utf-8") as f:
            layout = yaml.safe_load(f)
    except Exception:
        return None

    xs, ys = [], []
    for item in layout.get("room", {}).get("floor", []):
        if item.get("backing"):
            continue
        pos = item.get("pos", [0, 0, 0])
        size = item.get("size", [0, 0, 0])
        xs.extend([pos[0] - size[0], pos[0] + size[0]])
        ys.extend([pos[1] - size[1], pos[1] + size[1]])
    for wall in layout.get("room", {}).get("walls", []):
        if wall.get("backing"):
            continue
        pos = wall.get("pos", [0, 0, 0])
        xs.append(pos[0])
        ys.append(pos[1])

    if not xs or not ys:
        return None
    return min(xs) - padding, max(xs) + padding, min(ys) - padding, max(ys) + padding


def configure_bounds_from_sim(obstacles, padding=1.0, layout_path=None):
    layout_bounds = bounds_from_layout(layout_path)
    if layout_bounds is not None:
        x_min, x_max, y_min, y_max = layout_bounds
        base = fetch_base_status()
        if base and "pos" in base:
            bx, by = base["pos"][0], base["pos"][1]
            x_min = min(x_min, bx - padding)
            x_max = max(x_max, bx + padding)
            y_min = min(y_min, by - padding)
            y_max = max(y_max, by + padding)
        set_map_bounds(x_min, x_max, y_min, y_max)
        return

    xs, ys = [], []
    for obs in obstacles:
        cx, cy = obs["pos"][0], obs["pos"][1]
        size = obs["size"]
        radius = max(size[0], size[1] if len(size) > 1 else size[0]) * 2
        xs.extend([cx - radius, cx + radius])
        ys.extend([cy - radius, cy + radius])

    base = fetch_base_status()
    if base and "pos" in base:
        xs.append(base["pos"][0])
        ys.append(base["pos"][1])

    if not xs or not ys:
        return

    x_min, x_max = min(xs) - padding, max(xs) + padding
    y_min, y_max = min(ys) - padding, max(ys) + padding

    # Avoid tiny maps if the simulator reports very few static geoms.
    if x_max - x_min < 4.0:
        mid = (x_min + x_max) / 2
        x_min, x_max = mid - 2.0, mid + 2.0
    if y_max - y_min < 4.0:
        mid = (y_min + y_max) / 2
        y_min, y_max = mid - 2.0, mid + 2.0

    set_map_bounds(x_min, x_max, y_min, y_max)


def apply_inflation(img, radius_m):
    if radius_m <= 0:
        return img
    try:
        img_array = inflate(np.array(img), radius_m=radius_m)
        return Image.fromarray(img_array.astype(np.uint8))
    except ImportError:
        print("[map_generator] scipy 未安装，跳过膨胀", file=sys.stderr)
        return img


def clear_robot_footprint(img, radius_m):
    if radius_m <= 0:
        return img
    base = fetch_base_status()
    if not base or "pos" not in base:
        return img
    draw = ImageDraw.Draw(img)
    draw_circle(draw, base["pos"][0], base["pos"][1], radius_m, fill=FREE)
    return img


def apply_occupied_border(img, margin_m):
    """Mark the outer map margin as occupied so free points stay inside the room."""
    if margin_m <= 0:
        return img
    margin_px = max(1, int(np.ceil(float(margin_m) / RESOLUTION)))
    margin_px = min(margin_px, img.width // 2, img.height // 2)
    if margin_px <= 0:
        return img
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, img.width - 1, margin_px - 1], fill=OCCUPIED)
    draw.rectangle([0, img.height - margin_px, img.width - 1, img.height - 1], fill=OCCUPIED)
    draw.rectangle([0, 0, margin_px - 1, img.height - 1], fill=OCCUPIED)
    draw.rectangle([img.width - margin_px, 0, img.width - 1, img.height - 1], fill=OCCUPIED)
    return img


def generate_from_sim(
    output_dir,
    inflate_radius=0.0,
    border_occupied_margin=0.0,
    clear_robot_radius=0.0,
    layout_path=None,
    shrink_footprints=0.2,
):
    """从机器人后端射线投射生成地图"""
    # 先从 layout.yaml 算正确的地图范围
    lb = bounds_from_layout(layout_path)
    if lb is not None:
        x_min, x_max, y_min, y_max = lb
        # 扩展到包含机器人位置
        base = fetch_base_status()
        if base and "pos" in base:
            bx, by = base["pos"][0], base["pos"][1]
            x_min = min(x_min, bx - 1.0)
            x_max = max(x_max, bx + 1.0)
            y_min = min(y_min, by - 1.0)
            y_max = max(y_max, by + 1.0)
    else:
        x_min, x_max, y_min, y_max = -1.0, 8.0, -6.0, 1.0

    # 请求射线投射栅格
    params = {
        "resolution": RESOLUTION,
        "x_min": x_min, "x_max": x_max,
        "y_min": y_min, "y_max": y_max,
    }
    result = get_map_data(params=params, timeout=60)
    if not result or (result.get("success") is False and "grid" not in result):
        msg = result.get("result", "未知错误") if isinstance(result, dict) else "未知错误"
        print(f"[map_generator] 无法连接机器人后端: {msg}", file=sys.stderr)
        sys.exit(1)

    w = result["width"]
    h = result["height"]
    grid = result["grid"]
    origin = result.get("origin", [x_min, y_min])

    # 转 PGM 图像
    img = Image.new("L", (w, h))
    img.putdata(grid)

    # 保险：即使仿真端过滤遗漏，也清理机器人初始 footprint。
    img = clear_robot_footprint(img, clear_robot_radius)

    # 膨胀
    img = apply_inflation(img, inflate_radius)
    img = apply_occupied_border(img, border_occupied_margin)

    # 更新全局 bounds
    res = result.get("resolution", RESOLUTION)
    set_map_bounds(origin[0], origin[0] + w * res, origin[1], origin[1] + h * res)

    _save_map(img, output_dir)
    print(f"[map_generator] 射线投射地图: {w}x{h}, 分辨率 {res}m")
    print(f"[map_generator] 地图范围 x=[{origin[0]:.2f}, {origin[0]+w*res:.2f}], y=[{origin[1]:.2f}, {origin[1]+h*res:.2f}]")


# ============================================================
# 模式 2: 静态解析 layout.yaml（fallback）
# ============================================================

def generate_from_layout(layout_path, output_dir, inflate_radius=0.0, border_occupied_margin=0.0):
    """从 layout.yaml 静态生成地图"""
    with open(layout_path, "r", encoding="utf-8") as f:
        layout = yaml.safe_load(f)

    img = Image.new("L", (MAP_W, MAP_H), FREE)
    draw = ImageDraw.Draw(img)

    # 墙壁
    for wall in layout.get("room", {}).get("walls", []):
        if wall.get("backing"):
            continue
        pos = wall.get("pos", [0, 0, 0])
        size = wall.get("size", [0, 0, 0])
        if "side" not in wall:
            draw_rect(draw, pos[0], pos[1], size[0], 0.15)
        else:
            draw_rect(draw, pos[0], pos[1], 0.15, size[0])

    # 家具组
    for group_name in ["main_group", "island_group", "left_group", "right_group"]:
        group = layout.get(group_name, {})
        gpos = group.get("group_pos")
        grot = group.get("group_z_rot", 0)
        cos_r, sin_r = np.cos(grot), np.sin(grot)

        def tx(lx, ly):
            if gpos:
                return gpos[0] + lx * cos_r - ly * sin_r, gpos[1] + lx * sin_r + ly * cos_r
            return lx, ly

        for section in ["bottom_row", "island", "bottom_row_cabinets"]:
            for item in group.get(section, []):
                pos = item.get("pos")
                size = item.get("size", [0, 0, 0])
                if pos is None:
                    continue
                cx, cy = tx(pos[0], pos[1])
                draw_rect(draw, cx, cy, size[0], size[1])

    img = apply_inflation(img, inflate_radius)
    img = apply_occupied_border(img, border_occupied_margin)

    _save_map(img, output_dir)
    print(f"[map_generator] 从 layout.yaml 生成地图（静态模式，可能不完整）")


# ============================================================
# 通用
# ============================================================

def _save_map(img, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    pgm_path = os.path.join(output_dir, "kitchen_map.pgm")
    yaml_path = os.path.join(output_dir, "kitchen_map.yaml")

    img.save(pgm_path)

    meta = {
        "image": "kitchen_map.pgm",
        "resolution": RESOLUTION,
        "origin": [MAP_X_MIN, MAP_Y_MIN, 0.0],
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
    }
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(meta, f, default_flow_style=False)

    print(f"[map_generator] {pgm_path} ({img.width}x{img.height})")


if __name__ == "__main__":
    cfg = load_config()
    map_cfg = cfg.get("map", {})

    parser = argparse.ArgumentParser()
    parser.add_argument("--from-sim", action="store_true", help="从运行中的机器人后端读取障碍物")
    parser.add_argument("--layout", default=None, help="layout.yaml 路径")
    parser.add_argument("--output-dir", default=os.path.join(CONFIG_DIR, map_cfg.get("output_dir", "maps")))
    parser.add_argument(
        "--inflate-radius",
        type=float,
        default=map_cfg.get("inflate_radius", 0.0),
        help="生成地图时预膨胀障碍物的半径。",
    )
    parser.add_argument(
        "--border-occupied-margin",
        type=float,
        default=map_cfg.get("border_occupied_margin", 0.0),
        help="把地图四周指定宽度强制设为障碍，避免边缘被采样为可通行点。",
    )
    parser.add_argument(
        "--clear-robot-radius",
        type=float,
        default=map_cfg.get("clear_robot_radius", 0.0),
        help="从仿真生成地图时清理机器人初始位置附近的半径。",
    )
    parser.add_argument(
        "--bounds-layout",
        default=None,
        help="用于推导地图边界的 layout.yaml。",
    )
    parser.add_argument(
        "--shrink-footprints",
        type=float,
        default=map_cfg.get("shrink_footprints", 0.2),
        help="收缩家具 footprint。",
    )
    args = parser.parse_args()

    if args.from_sim:
        generate_from_sim(
            args.output_dir,
            args.inflate_radius,
            args.border_occupied_margin,
            args.clear_robot_radius,
            args.bounds_layout,
            args.shrink_footprints,
        )
    elif args.layout:
        generate_from_layout(args.layout, args.output_dir, args.inflate_radius, args.border_occupied_margin)
    else:
        # 默认尝试仿真，失败则用 layout
        if fetch_obstacles_from_sim() is not None:
            generate_from_sim(
                args.output_dir,
                args.inflate_radius,
                args.border_occupied_margin,
                args.clear_robot_radius,
                args.bounds_layout,
                args.shrink_footprints,
            )
        else:
            layout = os.path.join(os.path.dirname(__file__), "..", "serve", "scene", "config", "layout.yaml")
            generate_from_layout(layout, args.output_dir, args.inflate_radius, args.border_occupied_margin)
