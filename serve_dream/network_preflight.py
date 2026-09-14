"""Read-only DREAM 8001 network preflight.  This script never sends POST."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.request


def get_json(base_url, path, timeout):
    with urllib.request.urlopen(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}", timeout=timeout
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="DREAM Agent HTTP 只读网络预检")
    parser.add_argument("--base-url", default="http://192.168.0.185:8001")
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    host_port = args.base_url.split("//", 1)[-1].split("/", 1)[0]
    host, port_text = host_port.rsplit(":", 1)
    port = int(port_text)

    print(f"[1] TCP {host}:{port}")
    try:
        with socket.create_connection((host, port), timeout=args.timeout):
            print("  OK")
    except OSError as exc:
        print(f"  FAILED: {exc}")
        print("  本机必须与 DREAM 主机处于可路由网络；不要在未确认网段/网关时添加静态路由。")
        return 2

    try:
        print("[2] GET /v1/world")
        world = get_json(args.base_url, "/v1/world", args.timeout)
        print(json.dumps(world, ensure_ascii=False, indent=2))
        if world.get("frame_id") != "map":
            raise RuntimeError(f"frame_id 应为 map，实际为 {world.get('frame_id')}")

        print("[3] GET /v1/status")
        status = get_json(args.base_url, "/v1/status", args.timeout)
        summary = {key: status.get(key) for key in (
            "interface_online", "world_state_available", "motion_ready",
            "motion_blockers", "localization_initialized",
            "localization_approved", "gateway_ready", "token_ready",
            "active_command_id",
        )}
        print(json.dumps(summary, ensure_ascii=False, indent=2))

        print("[4] GET relation graph")
        graph_path = world.get("relation_graph_url") or "/total_scene_graph_latest.json"
        graph = get_json(args.base_url, graph_path, args.timeout)
        nodes = graph.get("nodes") or {}
        print(f"  nodes={len(nodes)}, table_2={'table_2' in nodes}, door_1={'door_1' in nodes}")
        if "table_2" not in nodes:
            raise RuntimeError("在线关系图缺少 table_2")
        if "door_1" not in nodes:
            print("  WARNING: 在线关系图缺少 door_1；本轮只能验收 table2，不执行穿门。")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 3

    print("只读网络预检通过；未发送任何导航命令。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
