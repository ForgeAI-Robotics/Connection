#!/usr/bin/env python3
"""Transform 3DGS PLY point cloud to match MuJoCo scene mesh rotation.

Usage:
    python transform_ply.py [--dry-run] [--input PLY] [--output PLY]
"""
import argparse
import os
import struct
import numpy as np
from pathlib import Path
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
    """XYZ euler (deg) → 3x3 rotation matrix."""
    rx, ry, rz = np.radians(euler_deg)
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def parse_ply_header(f):
    """Parse PLY header, return (property_names, vertex_count, is_binary, header_bytes)."""
    properties = []
    vertex_count = 0
    is_binary = False
    header_lines = []
    header_start = f.tell()
    while True:
        line = f.readline().decode("ascii", errors="replace").strip()
        header_lines.append(line + "\n")
        if line.startswith("format binary"):
            is_binary = True
        if line.startswith("element vertex"):
            vertex_count = int(line.split()[-1])
        if line.startswith("property float"):
            properties.append(line.split()[-1])
        if line.startswith("property uchar"):
            properties.append(line.split()[-1])
        if line == "end_header":
            break
    header_bytes = f.tell() - header_start
    return properties, vertex_count, is_binary, "".join(header_lines), header_bytes


def transform_ply(input_path, output_path, pos, euler_deg, dry_run=False):
    print(f"Input:     {input_path}")
    print(f"Output:    {output_path}")
    print(f"Rotation:  euler XYZ = {euler_deg}°")
    print(f"Translate: {pos}")

    R = euler_to_rot(euler_deg)

    with open(input_path, "rb") as f:
        props, n, is_binary, header_str, header_bytes = parse_ply_header(f)

        # Build dtype for numpy
        dt_list = []
        xyz_cols = []
        for i, p in enumerate(props):
            if i < 3:  # x, y, z
                dt_list.append((p, "f4"))
                xyz_cols.append(i)
            else:
                dt_list.append((p, "f4"))

        dt = np.dtype(dt_list)
        data = np.fromfile(f, dtype=dt, count=n)

    print(f"Vertices:  {n}")
    print(f"Columns:   {len(props)} ({', '.join(props[:6])} ...)")

    if dry_run:
        print("\nDry run — not writing.")
        return

    # Transform xyz
    xyz = np.stack([data[props[i]] for i in xyz_cols], axis=-1)  # (n, 3)
    xyz_new = (R @ xyz.T).T + pos.reshape(1, 3)

    # Write output
    with open(output_path, "wb") as f:
        f.write(header_str.encode("ascii"))
        # Write transformed data
        new_data = data.copy()
        new_data[props[0]] = xyz_new[:, 0]
        new_data[props[1]] = xyz_new[:, 1]
        new_data[props[2]] = xyz_new[:, 2]
        new_data.tofile(f)

    # Verify
    file_size = os.path.getsize(output_path)
    print(f"\nSaved:     {output_path}")
    print(f"Size:      {file_size / 1024 / 1024:.1f} MB")


def main():
    parser = argparse.ArgumentParser(description="Transform PLY to match scene rotation")
    parser.add_argument("--scene", default=None)
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    d = Path(__file__).parent
    scene_xml = args.scene or str(d / "mjcf" / "scene.xml")
    input_ply = args.input or str(d / "3dgs" / "point_cloud.ply")
    output_ply = args.output or str(d / "3dgs" / "point_cloud_transformed.ply")

    pos, euler = read_scene_transform(scene_xml)
    transform_ply(input_ply, output_ply, pos, euler, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
