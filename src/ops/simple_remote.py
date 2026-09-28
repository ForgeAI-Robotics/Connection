"""Panel access to the independent SIMPLE service; no brain imports."""
import json
import os
import shlex
import subprocess
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


def base_url():
    from ops.services import ROOT
    import yaml
    config = yaml.safe_load((ROOT / "config/robot_api.yaml").read_text()) or {}
    return os.environ.get("SIMPLE_O7_URL") or config.get("backends", {}).get("simple_o7", {}).get("url", "http://192.168.5.21:18770")


def request(path):
    url = base_url().rstrip("/") + path
    headers = {"Authorization": "Bearer " + os.environ.get("SIMPLE_O7_TOKEN", "")}
    with build_opener(ProxyHandler({})).open(Request(url, headers=headers), timeout=3) as response:
        result = json.load(response)
    if result.get("contract_version") != "connection/simple-o7/v1":
        raise ValueError("SIMPLE 合同不匹配")
    return result


def health(_service):
    try:
        info = request("/health")
        return {"ok": info.get("ready") is True,
                "detail": f"{info.get('robot_variant', '?').upper()} · {info.get('asset_version', '')}",
                "url": base_url()}
    except Exception as exc:
        return {"ok": False, "detail": str(exc), "url": base_url()}


def logs(lines):
    try:
        result = request(f"/v1/logs?lines={max(1, min(500, lines))}")
        return result["text"], result.get("path"), None
    except Exception as exc:
        return f"远端日志读取失败：{exc}", None, None


def run(action):
    if action not in {"start", "stop"}:
        raise ValueError("unsupported SIMPLE action")
    from ops.services import _load_dotenv
    _load_dotenv()
    target = os.environ.get("SIMPLE_SSH_TARGET", "fangqi@192.168.5.21")
    if target.startswith("-") or any(c.isspace() for c in target):
        raise ValueError("invalid SSH target")
    root = os.environ.get("SIMPLE_REMOTE_DIR", "/home/fangqi/connection-simple-o7")
    command = "python3 " + shlex.quote(root + "/manage.py") + " " + action
    argv = ["setsid", "-w", "ssh", "-o", "ConnectTimeout=8", "-o", "NumberOfPasswordPrompts=1",
            "-o", "StrictHostKeyChecking=accept-new"]
    env = os.environ.copy()
    password = os.environ.get("SIMPLE_SSH_PASSWORD")
    if password:
        argv += ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
        env.update(SSH_ASKPASS=str(Path(__file__).with_name("ssh_askpass.sh")), SSH_ASKPASS_REQUIRE="force",
                   DISPLAY=env.get("DISPLAY") or ":0", SSH_ASKPASS_PASSWORD=password)
    result = subprocess.run(argv + [target, command], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()
