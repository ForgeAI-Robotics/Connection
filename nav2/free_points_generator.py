#!/usr/bin/env python3
"""
从占据栅格地图提取可通行点，并生成可视化图。

输出:
  - free_points.json: 可通行点数据
  - free_points_vis.png: 可视化图（白=可通行 黑=障碍 红点=采样点）
"""

import argparse
import json
import math
import os
import re
from pathlib import Path

import numpy as np
import yaml
from PIL import Image


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


def world_from_pixel(px, py, origin, resolution, height):
    x = origin[0] + (px + 0.5) * resolution
    y = origin[1] + height * resolution - (py + 0.5) * resolution
    return x, y


def distance_to_obstacle(px, py, occupied, resolution, max_cells):
    best = max_cells + 1
    for ox, oy in occupied:
        dx = abs(px - ox)
        dy = abs(py - oy)
        if dx > best or dy > best:
            continue
        dist = math.hypot(dx, dy)
        if dist < best:
            best = dist
    return best * resolution


def generate_visualization(pgm_path, width, height, data, resolution, origin, points, output_path):
    arr = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
    vis = np.stack([arr] * 3, axis=-1).copy()

    for pt in points:
        px = int((pt["x"] - origin[0]) / resolution)
        py = int((origin[1] + height * resolution - pt["y"]) / resolution)
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                ny, nx = py + dy, px + dx
                if 0 <= nx < width and 0 <= ny < height:
                    vis[ny, nx] = [255, 50, 50]

    scale = 4
    vis_big = np.repeat(np.repeat(vis, scale, axis=0), scale, axis=1)
    vis_img = Image.fromarray(vis_big)

    vis_output = Path(output_path).with_name("free_points_vis.png")
    vis_img.save(vis_output)
    print(f"[vis] 可视化图已保存: {vis_output} ({vis_img.size[0]}x{vis_img.size[1]})")
    print(f"[vis] 图例: 白=可通行 黑=障碍 红点=采样可通行点({len(points)}个)")


def main():
    cfg = load_config()
    fp_cfg = cfg.get("free_points", {})

    parser = argparse.ArgumentParser(description="从占据栅格地图提取可通行点并生成可视化")
    parser.add_argument("--map", default=os.path.join(CONFIG_DIR, fp_cfg.get("map_file", "maps/kitchen_map.yaml")))
    parser.add_argument("--output", default=os.path.join(CONFIG_DIR, fp_cfg.get("output", "maps/free_points.json")))
    parser.add_argument("--spacing", type=float, default=fp_cfg.get("spacing", 0.5))
    parser.add_argument("--clearance", type=float, default=fp_cfg.get("clearance", 0.35))
    args = parser.parse_args()

    image, resolution, origin = read_map_yaml(args.map)
    map_dir = Path(args.map).resolve().parent
    pgm_path = map_dir / image
    width, height, data = read_pgm(pgm_path)

    occupied = [(i % width, i // width) for i, value in enumerate(data) if value < 128]
    step = max(1, round(args.spacing / resolution))
    max_cells = max(1, math.ceil(args.clearance / resolution))

    points = []
    for py in range(0, height, step):
        for px in range(0, width, step):
            if data[py * width + px] < 250:
                continue
            clearance = distance_to_obstacle(px, py, occupied, resolution, max_cells)
            if clearance < args.clearance:
                continue
            x, y = world_from_pixel(px, py, origin, resolution, height)
            points.append({
                "name": f"nav_{len(points):03d}",
                "x": round(x, 3),
                "y": round(y, 3),
                "clearance": round(clearance, 3),
            })

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "map": str(args.map),
        "spacing": args.spacing,
        "clearance": args.clearance,
        "points": points,
    }, indent=2))
    print(f"[free_points] {len(points)} 可通行点 -> {output}")

    generate_visualization(pgm_path, width, height, data, resolution, origin, points, output)


if __name__ == "__main__":
    main()
