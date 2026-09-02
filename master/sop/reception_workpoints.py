"""接待版工作点生成 —— 从 free_points,为【每个要操作的目标】各选一个工作点。

和 nav2/workpoints_generator(贪心最少覆盖)的区别:接待要【每个目标都能操作到】,不合并——
  · 取可乐:可乐/冰箱前一个工作点,yaw 朝可乐(取);
  · 摆放:每把椅子(座位)旁一个工作点,yaw 朝【桌子】(把可乐放到该座位前的桌面),不是朝椅子本身。

工作点仍【从 free_points 里选】(free_points 已是占据采样+clearance 过滤的可通行点),本模块不自己判占据。
数据源:PBD 语义地图(pbd_world 拉物体 + PBD 占据图→free_points);PBD 离线时用 --demo 假数据验证逻辑。

用法:
  python master/sop/reception_workpoints.py --demo     # 接待场景假数据,验证逻辑(PBD 离线时)
  python master/sop/reception_workpoints.py            # 接 PBD(需在线;联调时接)
"""

import argparse
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_ROOT, "nav2"))
from workpoints_generator import snap_to_90, compute_yaw   # noqa: E402  复用朝向计算

D_MIN, D_MAX = 0.3, 0.6   # 可操作距离窗(默认;真值由 G1 臂长/底盘定,联调调)


def _nearest_free_point(free_points, target_xy, d_min, d_max, taken):
    """从 free_points 里选离 target 在 [d_min,d_max] 内、最近且未被占用的一个。够不到返 None。"""
    cands = []
    for p in free_points:
        if p["name"] in taken:
            continue
        d = math.hypot(p["x"] - target_xy[0], p["y"] - target_xy[1])
        if d_min <= d <= d_max:
            cands.append((d, p))
    if not cands:
        return None
    cands.sort(key=lambda item: item[0])
    return cands[0][1]


def gen_reception_workpoints(free_points, cola_xy, chairs, table_xy, d_min=D_MIN, d_max=D_MAX):
    """每目标一个工作点。
      cola_xy: 可乐/冰箱位置 → 前面工作点,朝可乐(取);None 跳过。
      chairs:  [(name, xy)] 每把椅子 → 旁边工作点,朝 table(摆放)。
    返回 (waypoints, unreachable)。waypoints:[{name,serves,pos,yaw_deg,face}]。"""
    wps, unreachable, taken = [], [], set()

    # ① 取可乐工作点(朝可乐)
    if cola_xy is not None:
        p = _nearest_free_point(free_points, cola_xy, d_min, d_max, taken)
        if p:
            yaw = snap_to_90(compute_yaw([p["x"], p["y"]], cola_xy))
            wps.append({"name": p["name"], "serves": ["可乐"], "pos": [p["x"], p["y"]],
                        "yaw_deg": round(yaw, 1), "face": "可乐(取)"})
            taken.add(p["name"])
        else:
            unreachable.append("可乐")

    # ② 每把椅子一个工作点(朝桌子摆放)
    for name, xy in chairs:
        p = _nearest_free_point(free_points, xy, d_min, d_max, taken)
        if not p:
            unreachable.append(name)
            continue
        yaw = snap_to_90(compute_yaw([p["x"], p["y"]], table_xy))   # 朝桌子放可乐,不是朝椅子
        wps.append({"name": p["name"], "serves": [name], "pos": [p["x"], p["y"]],
                    "yaw_deg": round(yaw, 1), "face": "桌子(摆放)"})
        taken.add(p["name"])

    return wps, unreachable


def _demo_data():
    """接待场景假数据(PBD 离线时验证逻辑):会议室桌+3椅、茶水间可乐;free_points 网格。
    真实数据来自 PBD:桌/椅/可乐 = pbd_world 语义物体 center;free_points = PBD 占据图采样。"""
    table_xy = [2.0, 2.0]
    chairs = [("椅子1", [1.2, 1.2]), ("椅子2", [2.8, 1.2]), ("椅子3", [2.0, 3.2])]
    cola_xy = [5.0, 5.0]
    free_points, i, x = [], 0, 0.0
    while x <= 6.0:                                  # 0.4 网格覆盖 [0,6]²
        y = 0.0
        while y <= 6.0:
            free_points.append({"name": f"nav_{i:03d}", "x": round(x, 2), "y": round(y, 2)})
            i += 1
            y += 0.3
        x += 0.3
    return free_points, cola_xy, chairs, table_xy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="接待场景假数据,验证逻辑(PBD 离线时)")
    ap.add_argument("--d-min", type=float, default=D_MIN)
    ap.add_argument("--d-max", type=float, default=D_MAX)
    args = ap.parse_args()

    if args.demo:
        free_points, cola_xy, chairs, table_xy = _demo_data()
        print("[--demo] 接待场景假数据(PBD 离线,只验证选点/朝向逻辑)")
    else:
        # TODO 联调:pbd_world 拉 可乐/椅子/桌子 center + PBD 占据图 → free_points_generator 生成 free_points
        print("接 PBD 数据源需 PBD 在线(现在离线)。请用 --demo 验证逻辑,联调时接 pbd_world。")
        raise SystemExit(1)

    print(f"目标: 可乐@{cola_xy} · 桌子@{table_xy} · {len(chairs)} 把椅子 · free_points {len(free_points)} 个")
    wps, unreachable = gen_reception_workpoints(free_points, cola_xy, chairs, table_xy,
                                                args.d_min, args.d_max)
    print(f"\n生成 {len(wps)} 个工作点(每目标一个):")
    for w in wps:
        print(f"  {w['serves'][0]:6s} → 站位[{w['pos'][0]}, {w['pos'][1]}] "
              f"yaw={w['yaw_deg']:>5}° 朝 {w['face']}")
    if unreachable:
        print(f"\n⚠ 够不到(需大脑上报): {unreachable}")
    else:
        print("\n✓ 所有目标都有可操作工作点")


if __name__ == "__main__":
    main()
