"""
map_generator.py - 从碰撞 XML 生成 3DGS 场景的 Nav2 占据地图

从 scene.xml 引用的碰撞几何文件 (V-HACD 分解的 box 集合) 读取障碍物，
应用 body 世界变换，按高度过滤 (z_min ~ z_max)，投影到 2D 栅格后
膨胀并输出 PGM + YAML。

使用:
    python assets/scene_3dgs/map_generator.py
    python assets/scene_3dgs/map_generator.py --config assets/scene_3dgs/config.yaml
"""

import os
import re
import sys
import argparse
import numpy as np
import yaml
from PIL import Image, ImageDraw
from scipy.spatial.transform import Rotation
from scipy.ndimage import binary_dilation

SCENE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCENE_DIR, "config.yaml")

FREE = 254
OCCUPIED = 0
UNKNOWN = 205


def load_config(config_path):
    with open(config_path) as f:
        return yaml.safe_load(f)


def parse_collision_xml(xml_path):
    """解析碰撞 XML，返回 [(pos_array, size_array), ...]"""
    with open(xml_path) as f:
        content = f.read()
    pattern = r'<geom\s+name="[^"]*"\s+type="(\w+)"\s+pos="([^"]+)"\s+size="([^"]+)"'
    geoms = []
    for gtype, pos_str, size_str in re.findall(pattern, content):
        pos = np.array([float(v) for v in pos_str.split()])
        size = np.array([float(v) for v in size_str.split()])
        geoms.append((pos, size))
    return geoms


def build_transform(cfg):
    """从配置构建 scipy Rotation 和平移向量。"""
    bt = cfg["body_transform"]
    euler_deg = np.array(bt["euler_deg"], dtype=float)
    body_pos = np.array(bt["pos"], dtype=float)
    R = Rotation.from_euler("ZYX", np.radians(euler_deg))
    return R, body_pos


def transform_box_to_world(pos, size, R, body_pos):
    """将 body 坐标系下的 OBB 变换到世界坐标系，返回 (center, 3 axes)。"""
    center = R.apply(pos) + body_pos
    axes = [R.apply(np.array([s if i == j else 0.0 for j in range(3)])) * s
            for i, s in enumerate(size)]
    return center, axes


def generate_map(cfg, config_path):
    map_cfg = cfg["map"]
    scene_cfg = cfg["scene"]

    collision_path = os.path.join(SCENE_DIR, scene_cfg["collision_xml"])
    if not os.path.isfile(collision_path):
        print(f"[map_generator] 碰撞文件不存在: {collision_path}", file=sys.stderr)
        sys.exit(1)

    resolution = map_cfg.get("resolution", 0.05)
    z_min = map_cfg.get("z_min", 0.0)
    z_max = map_cfg.get("z_max", 2.0)
    vahacd_dilation = map_cfg.get("vahacd_dilation", 0.15)
    inflate_radius = map_cfg.get("inflate_radius", 0.05)
    border_margin = map_cfg.get("border_margin", 0.20)
    bounds_padding = map_cfg.get("bounds_padding", 1.0)
    output_dir = os.path.join(SCENE_DIR, map_cfg.get("output_dir", "maps"))

    R, body_pos = build_transform(cfg)
    geoms = parse_collision_xml(collision_path)
    print(f"[map_generator] 读取 {len(geoms)} 个碰撞几何体")

    # ── 计算世界坐标包围盒 (仅保留 z 在 [z_min, z_max] 范围内的 box) ──
    all_xy = []
    kept = 0
    for pos, size in geoms:
        center_w, axes_w = transform_box_to_world(pos, size, R, body_pos)
        corners_w = []
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    c = center_w + sx * axes_w[0] + sy * axes_w[1] + sz * axes_w[2]
                    corners_w.append(c)
        corners_w = np.array(corners_w)

        # 高度过滤：box 的 Z 范围是否与 [z_min, z_max] 相交
        cz_min = corners_w[:, 2].min()
        cz_max = corners_w[:, 2].max()
        if cz_max < z_min or cz_min > z_max:
            continue
        kept += 1
        all_xy.append(corners_w[:, :2])

    all_xy = np.concatenate(all_xy, axis=0)
    print(f"[map_generator] 高度过滤后保留 {kept} 个碰撞体 (z ∈ [{z_min}, {z_max}])")

    x_min = float(all_xy[:, 0].min()) - bounds_padding
    x_max = float(all_xy[:, 0].max()) + bounds_padding
    y_min = float(all_xy[:, 1].min()) - bounds_padding
    y_max = float(all_xy[:, 1].max()) + bounds_padding

    w = int(np.ceil((x_max - x_min) / resolution))
    h = int(np.ceil((y_max - y_min) / resolution))
    print(f"[map_generator] 地图范围 x=[{x_min:.2f}, {x_max:.2f}] y=[{y_min:.2f}, {y_max:.2f}]  栅格 {w}x{h}")

    # ── 光栅化碰撞体 ──
    def w2p(wx, wy):
        px = int(round((wx - x_min) / resolution))
        py = int(round((y_max - wy) / resolution))
        return max(0, min(px, w - 1)), max(0, min(py, h - 1))

    grid = np.full((h, w), UNKNOWN, dtype=np.uint8)
    img = Image.fromarray(grid)
    draw = ImageDraw.Draw(img)

    for pos, size in geoms:
        center_w, axes_w = transform_box_to_world(pos, size, R, body_pos)
        corners_w = []
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    c = center_w + sx * axes_w[0] + sy * axes_w[1] + sz * axes_w[2]
                    corners_w.append(c)
        corners_w = np.array(corners_w)
        cz_min = corners_w[:, 2].min()
        cz_max = corners_w[:, 2].max()
        if cz_max < z_min or cz_min > z_max:
            continue
        px_pts = [w2p(c[0], c[1]) for c in corners_w]
        draw.polygon(px_pts, fill=OCCUPIED)

    grid = np.array(img)

    # ── V-HACD 缝隙闭合 ──
    dilation_px = max(1, int(vahacd_dilation / resolution))
    obs = grid == OCCUPIED
    grid[binary_dilation(obs, iterations=dilation_px)] = OCCUPIED

    # 剩余 UNKNOWN 视为 free
    grid[grid == UNKNOWN] = FREE

    # ── 边框 ──
    border_px = max(1, int(border_margin / resolution))
    grid[:border_px, :] = OCCUPIED
    grid[-border_px:, :] = OCCUPIED
    grid[:, :border_px] = OCCUPIED
    grid[:, -border_px:] = OCCUPIED

    # ── 最终膨胀 ──
    inflate_px = max(1, int(inflate_radius / resolution))
    obs = grid == OCCUPIED
    grid[binary_dilation(obs, iterations=inflate_px)] = OCCUPIED

    # ── 保存 ──
    os.makedirs(output_dir, exist_ok=True)
    pgm_path = os.path.join(output_dir, "scene_map.pgm")
    yaml_path = os.path.join(output_dir, "scene_map.yaml")

    Image.fromarray(grid).save(pgm_path)

    meta = {
        "image": "scene_map.pgm",
        "resolution": resolution,
        "origin": [round(x_min, 2), round(y_min, 2), 0.0],
        "negate": 0,
        "occupied_thresh": 0.65,
        "free_thresh": 0.196,
    }
    with open(yaml_path, "w") as f:
        yaml.dump(meta, f, default_flow_style=False)

    free_n = int((grid == FREE).sum())
    occ_n = int((grid == OCCUPIED).sum())
    print(f"[map_generator] {pgm_path} ({w}x{h})  free={free_n}  occupied={occ_n}")
    return pgm_path, yaml_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从碰撞 XML 生成 3DGS 场景占据地图")
    parser.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    args = parser.parse_args()

    cfg = load_config(args.config)
    generate_map(cfg, args.config)
