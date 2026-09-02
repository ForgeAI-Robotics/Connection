#!/usr/bin/env python3
"""Read-only acceptance test for the PBD-AG brain HTTP gateway."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from pbd_ag_client import PBDAGClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8088")
    args = parser.parse_args()
    client = PBDAGClient(args.server, timeout=10.0)

    health = client.health()
    assert health.get("api_version") == "v1", health
    assert health.get("world_ready") is True, health

    snapshot = client.get_world_snapshot()
    assert snapshot.get("map_session_id"), snapshot
    assert snapshot.get("coordinate_convention", {}).get("frame_id") == "map", snapshot
    assert isinstance(snapshot.get("scene_graph", {}).get("nodes"), dict), snapshot

    metadata = client.get_map_metadata()
    assert float(metadata["map"]["resolution"]) > 0.0, metadata
    objects = client.get_objects()
    assert all(item.get("node_id") for item in objects), objects

    with tempfile.TemporaryDirectory(prefix="pbd_ag_acceptance_") as temporary:
        output = Path(temporary)
        files = client.download_map(output / "map")
        assert files["map.yaml"].stat().st_size > 0
        assert files["map.pgm"].stat().st_size > 0
        rgb = client.download_rgb(output / "rgb_latest")
        assert rgb.stat().st_size > 0

    result = {
        "success": True,
        "server": args.server,
        "map_session_id": health.get("map_session_id"),
        "world_revision": health.get("world_revision"),
        "object_count": len(objects),
        "navigation_mode": health.get("navigation", {}).get("mode"),
        "source_stale": health.get("source_stale"),
        "note": "No navigation command was issued by this read-only test.",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
