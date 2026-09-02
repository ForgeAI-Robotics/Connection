"""rosmap.py - ROS map_server 地图(map.yaml + P5 PGM) → /map_data 网格 JSON 适配。

DREAM 交付的 trinary 占据图:occupied=0 / unknown=205 / free=254。
直接透传像素值即可与项目现有语义兼容:
  - nav2/free_points_generator: <128 = 障碍, >=250 = 可通行(205 unknown 两不沾 = 不可走也不算障碍)
  - serve /scan 射线: <128 = 占据
坐标约定与 nav2 生成器一致(origin 为左下角,PGM 第一行是地图顶部)。
"""

import os
import re
from pathlib import Path


def read_map_yaml(path):
    """与 nav2/free_points_generator.read_map_yaml 同款解析,容忍行内注释。"""
    text = Path(path).read_text()
    image = re.search(r"^image:\s*(.+)$", text, re.M).group(1).strip()
    resolution = float(re.search(r"^resolution:\s*([-0-9.]+)", text, re.M).group(1))
    origin_match = re.search(r"^origin:\s*\n-\s*([-0-9.eE+]+)\s*\n-\s*([-0-9.eE+]+)", text, re.M)
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


def load_map_data(map_yaml_path):
    """→ serve /map_data 同构: {grid, width, height, resolution, origin}。"""
    image, resolution, origin = read_map_yaml(map_yaml_path)
    pgm_path = Path(map_yaml_path).resolve().parent / image
    width, height, data = read_pgm(pgm_path)
    return {
        "grid": list(data),
        "width": width,
        "height": height,
        "resolution": resolution,
        "origin": [origin[0], origin[1]],
        "source": str(pgm_path),
    }


def resolve_map_path(exchange_dir, filename="map.yaml"):
    path = os.path.join(exchange_dir, filename)
    return path if os.path.isfile(path) else None
