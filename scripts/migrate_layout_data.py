"""One-time, offline migration from the retired layout. Never starts services."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import yaml


TREES = {
    "master/sop/runtime/releases": "data/releases",
    "master/sop/runtime/kernel": "data/tasks",
    "master/sop/runtime/reception": "data/retired/reception",
    "master/memory": "data/memory",
    "master/scene": "config/scene",
    "deploy/task_timeline": "data/timelines",
    "integrations/feishu/runtime": "data/feishu",
    "serve_dream/integration": "data/dream/integration",
    "serve_dream/runtime": "data/dream",
    ".runtime": "data/system",
    "log": "logs",
    ".logs": "logs/retired/root",
    "master/.logs": "logs/retired/master",
}
CONFIGS = {
    "master/config.yaml": "config/brain.yaml",
    "slaver/config.yaml": "config/slaver.yaml",
    "robot_api/config.yaml": "config/robot_api.yaml",
    "serve_dream/config.yaml": "config/dream.yaml",
    "serve_dream/dream_navigation_sop.yaml": "config/dream_navigation_sop.yaml",
    "extensions/serve_real/config.yaml": "config/serve_real.yaml",
}


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def assert_idle(source: Path):
    """Unresolved commands in either generation must be settled before migration."""
    for relative in ("master/sop/runtime/kernel/current_task.json",
                     "master/sop/runtime/reception/current_task.json"):
        path = source / relative
        if not path.exists():
            continue
        record = json.loads(path.read_text())
        if not isinstance(record, dict) or not record.get("task_id"):
            raise ValueError(f"Invalid ledger: {relative}")
        if record.get("state") not in {"succeeded", "failed", "cancelled", "SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ValueError(f"Unresolved task: {relative}")
        if (record.get("open_command_id") or record.get("command_unknown") or record.get("holding")
                or record.get("resources_cleared") is False or record.get("blocks_new_motion")):
            raise ValueError(f"Unresolved command or resource: {relative}")


def translated_config(path: str, raw: bytes, source: Path) -> bytes:
    data = yaml.safe_load(raw) or {}
    if path == "master/config.yaml":
        real = data.setdefault("reception_real", {})
        existing = real.get("kernel_runtime_dir")
        if existing and existing not in {"./sop/runtime/kernel", "sop/runtime/kernel", str(source / "master/sop/runtime/kernel")}:
            raise ValueError("Custom task ledger path requires an explicit migration mapping")
        real["kernel_runtime_dir"] = "data/tasks"
        real.pop("runtime_dir", None)
        data.setdefault("brain", {})["scheduler"] = "runtime"
        scene = data.get("scene")
        if isinstance(scene, dict) and scene.get("path") in {"./scene/profile.yaml", "scene/profile.yaml"}:
            scene["path"] = "config/scene/profile.yaml"
        reflection = data.get("reflection") or {}
        if reflection.get("release_dir") in {"./sop/runtime/releases", "sop/runtime/releases"}:
            reflection["release_dir"] = str(source / "data/releases")
    # Other contract fields and credentials are preserved verbatim as YAML values.
    def translate(value):
        if isinstance(value, dict):
            return {k: translate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [translate(v) for v in value]
        if isinstance(value, str):
            for old, new in sorted(TREES.items(), key=lambda pair: -len(pair[0])):
                for prefix, replacement in ((str(source / old), str(source / new)), (old, new)):
                    if value == prefix or value.startswith(prefix + "/"):
                        return replacement + value[len(prefix):]
        return value
    return yaml.safe_dump(translate(data), allow_unicode=True, sort_keys=False).encode()


def plan(source: Path):
    assert_idle(source)
    pairs = {}
    for old, new in TREES.items():
        for path in (source / old).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                pairs[str(path.relative_to(source))] = str(Path(new) / path.relative_to(source / old))
    for old, new in CONFIGS.items():
        if (source / old).is_file():
            pairs[old] = new
    for path in (source / "master/sop").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".py", ".pyc"}:
            continue
        old = str(path.relative_to(source))
        if old not in pairs:
            rel = path.relative_to(source / "master/sop")
            pairs[old] = str(Path("config/business" if path.suffix == ".yaml" and len(rel.parts) == 1 else "data/business") / rel)
    return pairs


def migrate(source: Path, target: Path, backup: Path, *, apply=False):
    pairs = plan(source)
    prepared = []
    for old, new in sorted(pairs.items()):
        raw = (source / old).read_bytes()
        content = translated_config(old, raw, source) if old in CONFIGS else raw
        destination = target / new
        if destination.exists() and destination.read_bytes() != content:
            raise ValueError(f"Destination differs; refusing overwrite: {new}")
        prepared.append((old, new, raw, content))
    manifest = [{"source": old, "target": new, "before": digest(raw), "after": digest(content)}
                for old, new, raw, content in prepared]
    if not apply:
        return manifest
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Back up every original before any destination is changed.
    for old, _, raw, _ in prepared:
        path = backup / old
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    for old, new, raw, content in prepared:
        path = target / new
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        shutil.copymode(source / old, path)
        if old in CONFIGS:
            path.chmod(0o600)
        if digest(path.read_bytes()) != digest(content):
            raise IOError(f"Copy verification failed: {new}")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = migrate(args.source.resolve(), args.target.resolve(), args.backup.resolve(), apply=args.apply)
    print(json.dumps({"applied": args.apply, "files": len(result)}))
