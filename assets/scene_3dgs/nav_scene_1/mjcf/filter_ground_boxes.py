"""Filter out collision boxes near the ground plane from sugar_collision.xml."""

from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

# ── 配置参数 ──────────────────────────────────────────
FILTER_Z_MIN = 0.0    # 世界坐标 z 下界（米）
FILTER_Z_MAX = 0.5    # 世界坐标 z 上界（米），此区间内的 box 会被删除

SCENE_XML = Path(__file__).parent / "scene.xml"
COLLISION_XML = Path(__file__).parent / "object" / "sugar_collision.xml"


def parse_body_transform(scene_xml: Path):
    """Extract body pos and euler from scene.xml."""
    with open(scene_xml) as f:
        for line in f:
            line = line.strip()
            if '<body' in line and 'sugar_collision' in line:
                pos_part = line.split('pos="')[1].split('"')[0].split()
                euler_part = line.split('euler="')[1].split('"')[0].split()
                return (
                    np.array([float(x) for x in pos_part]),
                    np.array([float(x) for x in euler_part]),
                )
    raise ValueError("Could not find mesh body with sugar_collision in scene.xml")


def filter_collision_boxes(collision_xml: Path, pos, euler):
    """Remove boxes whose world-space z falls in [FILTER_Z_MIN, FILTER_Z_MAX)."""
    rot = Rotation.from_euler('xyz', euler, degrees=True)

    lines_out = []
    removed = 0
    kept = 0

    with open(collision_xml) as f:
        for line in f:
            stripped = line.strip()
            if 'pos="' in stripped:
                parts = stripped.split('pos="')[1].split('"')[0].split()
                local_pt = np.array([float(x) for x in parts])
                world_z = rot.apply(local_pt)[2] + pos[2]
                if FILTER_Z_MIN <= world_z < FILTER_Z_MAX:
                    removed += 1
                    continue
                kept += 1
            lines_out.append(line)

    return lines_out, removed, kept


def main():
    print(f"Scene XML: {SCENE_XML}")
    print(f"Collision XML: {COLLISION_XML}")
    print(f"Filter range: z in [{FILTER_Z_MIN}, {FILTER_Z_MAX})")

    pos, euler = parse_body_transform(SCENE_XML)
    print(f"Body pos: {pos}, euler: {euler}")

    lines_out, removed, kept = filter_collision_boxes(COLLISION_XML, pos, euler)
    print(f"Removed: {removed}, Kept: {kept}")

    with open(COLLISION_XML, 'w') as f:
        f.writelines(lines_out)
    print("Done.")


if __name__ == "__main__":
    main()
