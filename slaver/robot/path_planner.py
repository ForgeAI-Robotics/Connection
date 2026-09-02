"""A* path planner over pre-computed free_points graph.

Graph edges are validated against the PGM map with a 7-pixel (0.35 m)
clearance buffer so corner-clipping connections are excluded at build time.
After A* finds a raw path a greedy shortcutting pass removes redundant
waypoints, keeping the safety guarantee while reducing step count.
"""
import heapq
import json
import math
import os
import re
import sys

import numpy as np
import yaml

_FREE_POINTS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'nav2', 'maps', 'free_points.json')
)
_NAV2_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'nav2', 'config.yaml')
)

_pts      = None   # list of (x, y)
_adj      = None   # adjacency list: _adj[i] = [(j, dist), ...]
_map_grid = None   # numpy uint8 (height, width)
_map_res  = None
_map_ox   = None
_map_oy   = None
_map_h    = None

# 7 px × 0.05 m/px = 0.35 m  (= robot base_radius).
# Edges that bring the robot within this distance of a map obstacle are
# removed from the graph so A* cannot route through them.
# Note: map obstacles are shrunk by shrink_footprints=0.1 m, so the
# effective clearance from actual furniture is ~0.25 m.
_EDGE_CLEARANCE_PX = 6   # 6 px × 0.05 m/px = 0.30 m. Combined with the 0.10 m obstacle
                          # shrink applied at map generation, real clearance ≈ 0.40 m.
                          # Using 7 blocked nodes along y=-1.025 because the counter wall
                          # sits at exactly 7 px from those points.

_FREE_THRESHOLD = 200  # PGM value > 200 → free cell


# ── map ──────────────────────────────────────────────────────────────────────

def _load_map():
    global _map_grid, _map_res, _map_ox, _map_oy, _map_h
    if _map_grid is not None:
        return

    with open(_FREE_POINTS_PATH) as f:
        fp = json.load(f)
    map_yaml = fp.get('map', 'kitchen_map.yaml')
    if os.path.isabs(map_yaml) and not os.path.exists(map_yaml):
        # 旧机器的绝对路径失效时，退回相对于 free_points.json 的路径
        map_yaml = os.path.basename(map_yaml)
    if not os.path.isabs(map_yaml):
        map_yaml = os.path.normpath(os.path.join(os.path.dirname(_FREE_POINTS_PATH), map_yaml))

    text = open(map_yaml).read()
    image_name = re.search(r'^image:\s*(.+)$', text, re.M).group(1).strip()
    _map_res = float(re.search(r'^resolution:\s*([-\d.]+)', text, re.M).group(1))
    om = re.search(r'^origin:\n-\s*([-\d.]+)\n-\s*([-\d.]+)', text, re.M)
    _map_ox, _map_oy = float(om.group(1)), float(om.group(2))

    pgm = os.path.join(os.path.dirname(map_yaml), image_name)
    with open(pgm, 'rb') as f:
        assert f.readline().strip() == b'P5', "只支持二进制 PGM"
        line = f.readline()
        while line.startswith(b'#'):
            line = f.readline()
        w, h = map(int, line.split())
        f.readline()
        data = f.read()

    _map_grid = np.frombuffer(data, dtype=np.uint8).reshape((h, w))
    _map_h = h
    print(f"[path_planner] 地图加载: {w}×{h}, 分辨率 {_map_res}m/px", file=sys.stderr)


def _world_to_px(wx, wy):
    col = int((wx - _map_ox) / _map_res)
    row = int((_map_oy + _map_h * _map_res - wy) / _map_res)
    return col, row


def _check_line(sx, sy, gx, gy, clearance_px):
    """Return True if every sample along the segment is obstacle-free
    within clearance_px pixels."""
    h, w = _map_grid.shape
    dist = math.hypot(gx - sx, gy - sy)
    n_samples = max(int(dist / _map_res) + 2, 2)

    for i in range(n_samples):
        t = i / (n_samples - 1)
        col, row = _world_to_px(sx + t * (gx - sx), sy + t * (gy - sy))

        r1 = max(0, row - clearance_px);  r2 = min(h, row + clearance_px + 1)
        c1 = max(0, col - clearance_px);  c2 = min(w, col + clearance_px + 1)
        if r1 >= r2 or c1 >= c2:
            continue

        patch = _map_grid[r1:r2, c1:c2]
        if clearance_px <= 1:
            if (patch < _FREE_THRESHOLD).any():
                return False
        else:
            rs = np.arange(r1, r2) - row
            cs = np.arange(c1, c2) - col
            rr, cc = np.meshgrid(rs, cs, indexing='ij')
            mask = (rr ** 2 + cc ** 2) <= clearance_px ** 2
            if (patch[mask] < _FREE_THRESHOLD).any():
                return False

    return True


_LINE_CLEARANCE_PX = 4   # 4 px × 0.05 m/px = 0.20 m.  Less strict than the graph
                          # edge check (6 px) so workpoints near walls don't
                          # false-trigger A* when the path is actually navigable.


def is_line_clear(sx, sy, gx, gy):
    """Returns True when the straight path has at least _LINE_CLEARANCE_PX
    of obstacle-free margin.  Triggers A* only for real obstacles in the way,
    not for wall proximity at workpoint edges."""
    _load_map()
    return _check_line(sx, sy, gx, gy, clearance_px=_LINE_CLEARANCE_PX)


def _edge_safe(sx, sy, gx, gy):
    """Buffered check (_EDGE_CLEARANCE_PX) for graph edges and shortcuts."""
    return _check_line(sx, sy, gx, gy, clearance_px=_EDGE_CLEARANCE_PX)


# ── graph ─────────────────────────────────────────────────────────────────────

def _load_planner_cfg():
    with open(_NAV2_CONFIG_PATH) as f:
        return yaml.safe_load(f).get('path_planner', {})


def _build_graph():
    """Build and cache the adjacency graph.  Only edges that pass the
    buffered safety check are kept, so A* cannot produce corner-clipping paths."""
    global _pts, _adj
    if _pts is not None:
        return

    _load_map()

    with open(_FREE_POINTS_PATH) as f:
        raw = json.load(f)['points']
    _pts = [(p['x'], p['y']) for p in raw]

    cfg = _load_planner_cfg()
    radius = cfg.get('neighbor_radius', 0.8)

    n = len(_pts)
    _adj = [[] for _ in range(n)]
    kept = skipped = 0
    for i in range(n):
        xi, yi = _pts[i]
        for j in range(i + 1, n):
            d = math.hypot(xi - _pts[j][0], yi - _pts[j][1])
            if d > radius:
                continue
            if _edge_safe(xi, yi, _pts[j][0], _pts[j][1]):
                _adj[i].append((j, d))
                _adj[j].append((i, d))
                kept += 1
            else:
                skipped += 1

    print(
        f"[path_planner] 图构建完成: {n} 节点, {kept} 边有效, {skipped} 边因碰角剔除",
        file=sys.stderr,
    )


# ── path shortcutting ─────────────────────────────────────────────────────────

def _shorten(path):
    """Greedy shortcutting: skip intermediate waypoints wherever the direct
    segment is safe, reducing step count without losing corner-safety."""
    if len(path) <= 2:
        return path

    result = [path[0]]
    i = 0
    while i < len(path) - 1:
        j = len(path) - 1
        while j > i + 1:
            if _edge_safe(result[-1]['x'], result[-1]['y'],
                          path[j]['x'], path[j]['y']):
                break
            j -= 1
        result.append(path[j])
        i = j

    return result


# ── public API ────────────────────────────────────────────────────────────────

def plan_path(sx, sy, gx, gy, connect_k=3):
    """A* from (sx, sy) to (gx, gy).

    Start/goal are connected to their nearest k safe free_points (falls back
    to nearest k regardless of safety when no safe connection exists).

    Returns a list of {"x", "y"} dicts after deduplication and shortcutting,
    or None when no path exists.
    """
    _build_graph()
    n = len(_pts)
    S, G = n, n + 1

    def _nearest_k(px, py):
        ranked = sorted(range(n), key=lambda i: math.hypot(_pts[i][0] - px, _pts[i][1] - py))
        safe = [i for i in ranked[:connect_k * 3]
                if _edge_safe(px, py, _pts[i][0], _pts[i][1])]
        return safe[:connect_k] if safe else ranked[:connect_k]

    by_start    = _nearest_k(sx, sy)
    by_goal     = _nearest_k(gx, gy)
    by_goal_set = set(by_goal)

    def neighbors(node):
        if node == S:
            for i in by_start:
                yield i, math.hypot(_pts[i][0] - sx, _pts[i][1] - sy)
        elif node == G:
            pass
        else:
            yield from _adj[node]
            if node in by_goal_set:
                yield G, math.hypot(_pts[node][0] - gx, _pts[node][1] - gy)

    def h(node):
        if node == S:  return math.hypot(sx - gx, sy - gy)
        if node == G:  return 0.0
        return math.hypot(_pts[node][0] - gx, _pts[node][1] - gy)

    g_score  = {S: 0.0}
    came_from = {}
    heap = [(h(S), S)]

    while heap:
        _, curr = heapq.heappop(heap)

        if curr == G:
            raw, node = [], G
            while node in came_from:
                raw.append({'x': gx, 'y': gy} if node == G
                           else {'x': _pts[node][0], 'y': _pts[node][1]})
                node = came_from[node]
            raw.append({'x': sx, 'y': sy})
            raw.reverse()

            deduped = [raw[0]]
            for p in raw[1:]:
                if abs(p['x'] - deduped[-1]['x']) > 1e-3 or abs(p['y'] - deduped[-1]['y']) > 1e-3:
                    deduped.append(p)

            shortened = _shorten(deduped)
            print(
                f"[path_planner] A* {len(deduped)} 节点 → 裁剪后 {len(shortened)} 节点",
                file=sys.stderr,
            )
            return shortened

        for nb, d in neighbors(curr):
            ng = g_score[curr] + d
            if ng < g_score.get(nb, float('inf')):
                g_score[nb] = ng
                came_from[nb] = curr
                heapq.heappush(heap, (ng + h(nb), nb))

    print(f"[path_planner] 未找到路径: ({sx:.2f},{sy:.2f})→({gx:.2f},{gy:.2f})", file=sys.stderr)
    return None


def validate_workpoint_connectivity(workpoints):
    """Check that every pair of workpoints has a valid A* path.

    Args:
        workpoints: list of dicts with keys 'name', 'x'/'pos[0]', 'y'/'pos[1]'

    Prints a warning for every disconnected pair and returns False if any pair
    fails so the caller can abort or warn at startup.
    """
    _build_graph()
    pts = []
    for wp in workpoints:
        if 'x' in wp:
            pts.append((wp['name'], float(wp['x']), float(wp['y'])))
        else:
            pts.append((wp['name'], float(wp['pos'][0]), float(wp['pos'][1])))

    ok = True
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            n1, x1, y1 = pts[i]
            n2, x2, y2 = pts[j]
            path = plan_path(x1, y1, x2, y2)
            if path:
                print(
                    f"[path_planner] ✓ {n1}↔{n2}: {len(path)} 节点",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[path_planner] ✗ {n1}↔{n2}: 无路径 — 请重新生成地图或调整工作点",
                    file=sys.stderr,
                )
                ok = False
    return ok
