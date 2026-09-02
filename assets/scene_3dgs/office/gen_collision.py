#!/usr/bin/env python3
"""把 pgsr_office.obj 凸分解成碰撞几何(MuJoCo/MotrixSim 的 mesh 碰撞必须是凸的;大非凸场景直接
当碰撞会被取凸包 → 屋子变一坨实心、机器人进不去)。

⚠️ PGSR 重建 mesh 顶点极多(office 近千万顶点):
  - trimesh 的纯 Python OBJ 解析器读这么大的 obj **极慢(几十分钟)** → 用 open3d(C++)读+简化;
  - 碰撞用不着精细 → 先简化到 ~max_faces 再 coacd 凸分解。
装依赖:pip install coacd open3d          (open3d 装不上就只装 trimesh+fast-simplification,会退 numpy 解析,慢些)

生成(meshes/ 下,scene.xml 两个 <include> 引用):coll_*.obj / office_collision_assets.xml / office_collision.xml

用法(repo 根):
  python assets/scene_3dgs/office/gen_collision.py
  python assets/scene_3dgs/office/gen_collision.py --max-faces 40000 --threshold 0.12   # 更快更粗
"""
import argparse
import os
from pathlib import Path

import numpy as np
import trimesh
import coacd


def load_vf(obj_path, max_faces):
    """加载 obj 的顶点/面并简化。open3d(C++,快)优先;无则 numpy 手动解析(比 trimesh loader 快)。
    返回 (V[n,3] float, F[m,3] int)。"""
    # ---- 优先 open3d(读大 obj + 简化都是 C++)----
    try:
        import open3d as o3d
        print("  open3d 读取(C++,快)...", flush=True)
        me = o3d.io.read_triangle_mesh(obj_path)
        nf = len(me.triangles)
        print(f"  原始: {len(me.vertices)} 顶点 / {nf} 面", flush=True)
        if nf > max_faces:
            print(f"  open3d 简化到 ~{max_faces}...", flush=True)
            me = me.simplify_quadric_decimation(max_faces)
            print(f"  简化后: {len(me.triangles)} 面", flush=True)
        return np.asarray(me.vertices), np.asarray(me.triangles, dtype=np.int64)
    except Exception as e:
        print(f"  (open3d 不可用:{e} → 退 numpy 解析)", flush=True)

    # ---- 退路:numpy 手动解析 v/f(跳过 trimesh 慢 loader)----
    V, F = [], []
    with open(obj_path) as fh:
        for ln in fh:
            if ln[:2] == "v ":
                V.append(ln.split()[1:4])
            elif ln[:2] == "f ":
                F.append([p.split("/")[0] for p in ln.split()[1:4]])
    V = np.asarray(V, dtype=np.float64)
    F = np.asarray(F, dtype=np.int64) - 1        # obj 面索引是 1-based
    print(f"  解析: {len(V)} 顶点 / {len(F)} 面", flush=True)
    if len(F) > max_faces:
        print(f"  简化到 ~{max_faces}(需 fast-simplification)...", flush=True)
        m = trimesh.Trimesh(V, F, process=False).simplify_quadric_decimation(max_faces)
        V, F = np.asarray(m.vertices), np.asarray(m.faces, dtype=np.int64)
        print(f"  简化后: {len(F)} 面", flush=True)
    return V, F


def main():
    d = Path(__file__).parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--obj", default=str(d / "meshes" / "pgsr_office.obj"))
    ap.add_argument("--out-dir", default=str(d / "meshes"))
    ap.add_argument("--max-faces", type=int, default=60000,
                    help="碰撞用不着精细:先简化到这个面数再凸分解(越小越快;40000 更快)")
    ap.add_argument("--threshold", type=float, default=0.08,
                    help="coacd 凸分解精度(越大越粗越快,0.05~0.15)")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"载入 {args.obj} ...", flush=True)
    V, F = load_vf(args.obj, args.max_faces)

    print(f"凸分解中(threshold={args.threshold},这一步可能要几分钟)...", flush=True)
    parts = coacd.run_coacd(coacd.Mesh(V, F), threshold=args.threshold)
    print(f"  → {len(parts)} 个凸块", flush=True)

    asset_lines, geom_lines = [], []
    for i, (v, f) in enumerate(parts):
        name = f"coll_{i}"
        trimesh.Trimesh(np.asarray(v), np.asarray(f)).export(out / f"{name}.obj")
        asset_lines.append(f'  <mesh name="{name}" file="{name}.obj"/>')
        geom_lines.append(
            f'  <geom type="mesh" mesh="{name}" group="3" '
            f'contype="1" conaffinity="1" rgba="0 1 0 0"/>')

    # ⚠️ MotrixSim 的 <include> 是文本片段插入,文件要【裸列表】(无 <mujocoinclude> 根;
    #    见 nav_scene_1/mjcf/object/sugar_collision.xml —— 直接一堆 <geom>,没有根标签)。
    (out / "office_collision_assets.xml").write_text(
        "\n".join(asset_lines) + "\n", encoding="utf-8")
    (out / "office_collision.xml").write_text(
        "\n".join(geom_lines) + "\n", encoding="utf-8")
    print(f"写出 office_collision_assets.xml + office_collision.xml({len(parts)} 块)", flush=True)
    if len(parts) > 200:
        print("⚠ 凸块偏多(>200),物理会慢;可调大 --threshold", flush=True)


if __name__ == "__main__":
    main()
