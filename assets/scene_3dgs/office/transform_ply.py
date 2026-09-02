#!/usr/bin/env python3
"""把 3DGS PLY 点云应用和 MuJoCo 场景 mesh 相同的世界变换,使 gaussian 渲染和物理 mesh 对齐。

读 scene.xml 里 <body name="mesh"> 的 pos / euler,对 ply 的 xyz 做同样旋转+平移。
先调好 scene.xml 的摆正角度(README 第 2 步),再跑这个。

用法(repo 根):
  python assets/scene_3dgs/office/transform_ply.py
  python assets/scene_3dgs/office/transform_ply.py --dry-run   # 只看不写
"""
import argparse
import os
from pathlib import Path
import numpy as np
import xml.etree.ElementTree as ET


def read_scene_transform(scene_xml):
    tree = ET.parse(scene_xml)
    for body in tree.getroot().iter("body"):
        if body.get("name") == "mesh":
            pos = [float(x) for x in body.get("pos", "0 0 0").split()]
            euler = [float(x) for x in body.get("euler", "0 0 0").split()]
            return np.array(pos), np.array(euler)
    raise ValueError("No <body name='mesh'> found")


def euler_to_rot(euler_deg):
    """XYZ euler (deg) → 3x3 rotation matrix。"""
    rx, ry, rz = np.radians(euler_deg)
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def parse_ply_header(f):
    properties = []
    vertex_count = 0
    header_lines = []
    header_start = f.tell()
    while True:
        line = f.readline().decode("ascii", errors="replace").strip()
        header_lines.append(line + "\n")
        if line.startswith("element vertex"):
            vertex_count = int(line.split()[-1])
        if line.startswith("property float"):
            properties.append(line.split()[-1])
        if line.startswith("property uchar"):
            properties.append(line.split()[-1])
        if line == "end_header":
            break
    return properties, vertex_count, "".join(header_lines)


def transform_ply(input_path, output_path, pos, euler_deg, dry_run=False):
    print(f"Input:     {input_path}")
    print(f"Output:    {output_path}")
    print(f"Rotation:  euler XYZ = {euler_deg}°   Translate: {pos}")
    R = euler_to_rot(euler_deg)

    with open(input_path, "rb") as f:
        props, n, header_str = parse_ply_header(f)
        dt = np.dtype([(p, "f4") for p in props])
        data = np.fromfile(f, dtype=dt, count=n)

    print(f"Vertices:  {n}   Columns: {len(props)} ({', '.join(props[:6])} ...)")
    if dry_run:
        print("\nDry run — not writing.")
        return

    xyz = np.stack([data[props[i]] for i in range(3)], axis=-1)
    xyz_new = (R @ xyz.T).T + pos.reshape(1, 3)
    new_data = data.copy()
    for i in range(3):
        new_data[props[i]] = xyz_new[:, i]

    with open(output_path, "wb") as f:
        f.write(header_str.encode("ascii"))
        new_data.tofile(f)
    print(f"\nSaved: {output_path}  ({os.path.getsize(output_path) / 1e6:.1f} MB)")


def main():
    d = Path(__file__).parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default=str(d / "mjcf" / "scene.xml"))
    ap.add_argument("--input", default=str(d / "3dgs" / "pgsr_office.ply"))
    ap.add_argument("--output", default=str(d / "3dgs" / "pgsr_office_transformed.ply"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pos, euler = read_scene_transform(args.scene)
    transform_ply(args.input, args.output, pos, euler, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
