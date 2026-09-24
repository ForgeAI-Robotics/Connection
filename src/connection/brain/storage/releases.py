"""Read-only binding of a published snapshot. No candidate evaluation imports."""
import json
from pathlib import Path
from copy import deepcopy
from connection.brain.storage.tasks import _gate_for


def bound_release(config, package):
    root = str(((config or {}).get("reflection") or {}).get("release_dir") or "").strip()
    if not root or not (Path(root) / "releases.json").is_file():
        return {"version_id": "", "rules": []}
    # Releases are atomically replaced; a read observes one complete revision.
    data = json.loads((Path(root) / "releases.json").read_text())
    active_id = (data.get("active") or {}).get(package)
    for item in data.get("versions", []):
        if item.get("version_id") == active_id and item.get("package") == package:
            return {"version_id": active_id, "rules": deepcopy(item.get("rules") or [])}
    if active_id:
        raise ValueError("活动规则版本不存在，拒绝使用空快照")
    return {"version_id": "", "rules": []}
