#!/usr/bin/env python3
"""从 pgsr_office.obj + scene.xml 的 mesh 世界变换,直接生成 Nav2 2D 占据地图。

不走仓库里 box-only 的 map_generator.py(那个只认 <geom type=box pos size>);这里直接把
mesh 顶点变换到世界系、按高度切片投影,更适合 PGSR mesh。给导航组 Nav2 用。

用法(repo 根;先把 scene.xml 摆正,见 README 第 2 步):
  python assets/scene_3dgs/office/gen_map2d.py --z-min 0.1 --z-max 1.8
输出: office/maps/scene_map.pgm + scene_map.yaml
"""
import argparse
from pathlib import Path
import numpy as np
import trimesh
import yaml
import xml.etree.ElementTree as ET
from PIL import Image
from scipy.ndimage import binary_dilation


def scene_transform(scene_xml):
    for body in ET.parse(scene_xml).getroot().iter("body"):
        if body.get("name") == "mesh":
            pos = np.array([float(x) for x in body.get("pos", "0 0 0").split()])
            e = np.radians([float(x) for x in body.get("euler", "0 0 0").split()])
            cx, sx = np.cos(e[0]), np.sin(e[0])
            cy, sy = np.cos(e[1]), np.sin(e[1])
            cz, sz = np.cos(e[2]), np.sin(e[2])
            Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
            Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
            Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
            return Rz @ Ry @ Rx, pos
    raise SystemExit("scene.xml 里没有 name='mesh' 的 body")


def main():
    d = Path(__file__).parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--obj", default=str(d / "meshes" / "pgsr_office.obj"))
    ap.add_argument("--scene", default=str(d / "mjcf" / "scene.xml"))
    ap.add_argument("--z-min", type=float, default=0.1, help="切片高度下限(过滤地板)")
    ap.add_argument("--z-max", type=float, default=1.8, help="切片高度上限(过滤天花板)")
    ap.add_argument("--res", type=float, default=0.05, help="米/像素")
    ap.add_argument("--inflate", type=float, default=0.15, help="障碍膨胀(米)")
    ap.add_argument("--out", default=str(d / "maps"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    R, pos = scene_transform(args.scene)
    m = trimesh.load(args.obj, force="mesh")
    V = (R @ np.asarray(m.vertices).T).T + pos                 # 顶点 → 世界系
    wall = V[(V[:, 2] >= args.z_min) & (V[:, 2] <= args.z_max)]  # 按高度切障碍
    if len(wall) == 0:
        raise SystemExit("该高度区间没有点 → 检查 z-min/z-max 或 scene.xml 摆正是否对")

    xy = wall[:, :2]
    xmin, ymin = xy.min(0) - 1.0
    xmax, ymax = xy.max(0) + 1.0
    W = int((xmax - xmin) / args.res) + 1
    H = int((ymax - ymin) / args.res) + 1

    occ = np.zeros((H, W), bool)
    ix = np.clip(((xy[:, 0] - xmin) / args.res).astype(int), 0, W - 1)
    iy = np.clip(((xy[:, 1] - ymin) / args.res).astype(int), 0, H - 1)
    occ[iy, ix] = True
    occ = binary_dilation(occ, iterations=max(1, int(args.inflate / args.res)))

    grid = np.full((H, W), 254, np.uint8)   # 254=free
    grid[occ] = 0                           # 0=occupied
    Image.fromarray(np.flipud(grid), "L").save(out / "scene_map.pgm")   # 图像 y 向下,翻转
    yaml.safe_dump(
        {"image": "scene_map.pgm", "resolution": args.res,
         "origin": [float(xmin), float(ymin), 0.0],
         "negate": 0, "occupied_thresh": 0.65, "free_thresh": 0.196},
        open(out / "scene_map.yaml", "w"))
    print(f"地图 {W}x{H} → {out}/scene_map.pgm  (origin {xmin:.2f},{ymin:.2f}  res {args.res})")


if __name__ == "__main__":
    main()
