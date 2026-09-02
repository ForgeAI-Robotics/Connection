"""scene_graph.py - DREAM total_scene_graph_latest.json → /objects、/fixtures 适配。

场景图结构(见 docs/对接手册-导航建图组.md):
  nodes: {node_id: {object_id, class_name, parent_id, children, center, bbox_min, bbox_max, ...}}
  - 根 floor_0;挂在 floor 上的大件(table_1/fridge_1)= 我们的 fixtures
  - 挂在大件上的小物体(source=local_fine_grained_image)= 我们的 objects

注意:物体坐标是 parent_proxy_layout(桌面代理布局,非真实测量 3D),精度只够
"选去哪个工作点",不够盲抓——抓取必须靠到位后本地感知(VLA/相机)。
"""

import json
import os


def load_graph(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _is_fixture(node):
    """挂在 floor 上的非 floor 节点 = 家具级 fixture。"""
    return node.get("parent_id") == "floor_0" and node.get("class_name") != "floor"


def graph_fixtures(graph):
    """→ serve /fixtures 同构: {name: {pos, size, type}}。size 取 bbox 全尺寸。"""
    result = {}
    for node in (graph.get("nodes") or {}).values():
        if not _is_fixture(node):
            continue
        name = node.get("object_id") or node["node_id"]
        bmin, bmax = node.get("bbox_min"), node.get("bbox_max")
        size = (
            [round(hi - lo, 4) for lo, hi in zip(bmin, bmax)]
            if bmin and bmax else [0.0, 0.0, 0.0]
        )
        result[name] = {
            "pos": list(node.get("center") or [0.0, 0.0, 0.0]),
            "size": size,
            "type": node.get("class_name", "unknown"),
            "confidence": node.get("confidence"),
        }
    return result


def graph_objects(graph):
    """→ serve /objects 同构: {name: {pos, grasped}}。

    物体 = 有 fixture 父节点的叶子(场景图里 local inspection 挂上来的小物体)。
    grasped 恒 False:场景图不知道机器人手上有什么,持有状态由大脑自己记。
    """
    nodes = graph.get("nodes") or {}
    fixture_ids = {nid for nid, n in nodes.items() if _is_fixture(n)}
    result = {}
    for node in nodes.values():
        if node.get("parent_id") not in fixture_ids:
            continue
        name = node.get("object_id") or node["node_id"]
        result[name] = {
            "pos": list(node.get("center") or [0.0, 0.0, 0.0]),
            "grasped": False,
            "parent": nodes.get(node["parent_id"], {}).get("object_id") or node.get("parent_id"),
            "class_name": node.get("class_name"),
            "confidence": node.get("confidence"),
        }
    return result


def graph_meta(graph):
    return {
        "graph_id": graph.get("graph_id"),
        "created_at": graph.get("created_at"),
        "root_id": graph.get("root_id"),
    }


def door_navigation_contract(graph, door_id="door_1"):
    """Return the reviewed narrow-door contract without guessing missing geometry."""
    node = (graph.get("nodes") or {}).get(door_id) or {}
    evidence = node.get("evidence") or {}
    contract = evidence.get("navigation_contract")
    return contract if isinstance(contract, dict) else None


def resolve_graph_path(exchange_dir, filename="total_scene_graph_latest.json"):
    path = os.path.join(exchange_dir, filename)
    return path if os.path.isfile(path) else None
