"""exchange.py - 交换通道统一层:http(对方起服务,首选)与 dir(本地目录,离线测试)。

http 模式(对接文档 §2):
  - 数据:GET <base_url>/<文件名> 拉 map.yaml/map.pgm/场景图/位姿(对方在导出目录跑
    静态文件服务即可,如 `python3 -m http.server 8000`)。
  - 导航:POST <nav_url>/nav {x, y, target_yaw} → 对方 nav2_goal_bridge 同步执行
    NavigateToPose,走完返回 {success, ...},原样透传给上层。
"""

import json
import os
import re
import tempfile
import urllib.error
import urllib.request


def _ex(cfg):
    return cfg.get("exchange") or {}


def mode(cfg):
    return _ex(cfg).get("mode", "dir")


def base_dir(cfg, project_root):
    d = _ex(cfg).get("dir", "serve_dream/sample")
    return d if os.path.isabs(d) else os.path.join(project_root, d)


def base_url(cfg):
    return (_ex(cfg).get("base_url") or "").rstrip("/")


def nav_url(cfg):
    return (_ex(cfg).get("nav_url") or "").rstrip("/")


def fetch_bytes(cfg, filename, timeout=10.0):
    url = f"{base_url(cfg)}/{filename}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def load_json_file(cfg, project_root, key, default):
    """按通道读一个 JSON 文件。返回 (对象|None, 来源描述)。"""
    name = _ex(cfg).get(key, default)
    if mode(cfg) == "http":
        src = f"{base_url(cfg)}/{name}"
        try:
            return json.loads(fetch_bytes(cfg, name)), src
        except Exception as exc:
            return None, f"{src} ({exc})"
    path = os.path.join(base_dir(cfg, project_root), name)
    if not os.path.isfile(path):
        return None, path
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


def map_yaml_path(cfg, project_root):
    """返回可供 rosmap 解析的本地 map.yaml 路径。
    http 模式:把 map.yaml 及其引用的 pgm 拉到缓存目录再返回缓存路径。"""
    name = _ex(cfg).get("map_yaml", "map.yaml")
    if mode(cfg) != "http":
        path = os.path.join(base_dir(cfg, project_root), name)
        return path if os.path.isfile(path) else None
    cache = os.path.join(tempfile.gettempdir(), "serve_dream_cache")
    os.makedirs(cache, exist_ok=True)
    try:
        ydata = fetch_bytes(cfg, name)
        ypath = os.path.join(cache, os.path.basename(name))
        with open(ypath, "wb") as f:
            f.write(ydata)
        m = re.search(r"^image:\s*(.+)$", ydata.decode("utf-8"), re.M)
        pgm_name = m.group(1).strip()
        with open(os.path.join(cache, os.path.basename(pgm_name)), "wb") as f:
            f.write(fetch_bytes(cfg, pgm_name))
        return ypath
    except Exception:
        return None


def nav_forward(cfg, x, y, yaw_deg=None, timeout=180.0):
    """http 模式导航:转发给对方 /nav(nav2_goal_bridge 兼容),返回 (响应, http状态码)。"""
    payload = {"x": x, "y": y}
    if yaw_deg is not None:
        payload["target_yaw"] = yaw_deg  # bridge 自适应单位:>2π 按度转弧度
    url = nav_url(cfg) + "/nav"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), 200
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8")), exc.code
        except Exception:
            return {"success": False, "result": f"导航服务 HTTP {exc.code}"}, 502
    except Exception as exc:
        return {"success": False, "result": f"导航服务不可达: {exc}"}, 502
