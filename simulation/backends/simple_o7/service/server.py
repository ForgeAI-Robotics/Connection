"""HTTP command facade. Workers journal independently; queries never replay commands."""
import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .store import Journal, Conflict, VERSION, public


def scene(source):
    path = Path(source) / "data/scenes/g1_coke_tabletop/manifest.json"
    raw = path.read_bytes()
    manifest = json.loads(raw)
    return {"contract_version": VERSION, "backend": "simple_o7", "scene_id": manifest["scene_id"],
            "scene_revision": hashlib.sha256(raw).hexdigest(), "asset_version": manifest["asset_version"],
            "observation_source": "scene_manifest", "scope": "new_episode_template_not_live_camera",
            "capabilities": ["pick_hold_coke"], "episode_policy": "one_compound_action_per_task",
            "objects": {key: {"pos": value.get("pose"), "fixed": value.get("fixed"),
                              "graspable": key == "coke"} for key, value in manifest["assets"].items()},
            "limitations": ["No navigation or placement", "No live camera endpoint",
                            "New task explicitly creates a new simulation episode; same task never resets"]}


class Bridge:
    def __init__(self, config, launcher=None):
        self.config = config
        self.store = Journal(config["runtime"])
        self.launcher = launcher or self.launch

    def launch(self, command_id):
        log = Path(self.config["runtime"]) / "worker.log"
        with log.open("ab") as output:
            process = subprocess.Popen([sys.executable, "-m", "service.worker", "--config",
                                        self.config["config_path"], "--command", command_id],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       start_new_session=True)
        # Reap independently; losing this thread on a facade restart does not kill the worker.
        import threading
        threading.Thread(target=process.wait, daemon=True).start()

    def submit(self, request):
        required = {"contract_version", "command_id", "task_id", "step_id", "action", "object_id", "scene_revision"}
        if not isinstance(request, dict) or set(request) != required:
            raise ValueError("invalid_command_fields")
        if any(not isinstance(v, str) or not v or len(v) > 256 for v in request.values()):
            raise ValueError("invalid_command_value")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", request["command_id"]):
            raise ValueError("invalid_command_id")
        if (request["contract_version"] != VERSION or request["action"] != "pick_hold_coke"
                or request["object_id"] != "coke"):
            raise ValueError("unsupported_action")
        # A duplicate remains queryable even if the scene changed since original acceptance.
        try:
            existing = self.store.get(request["command_id"])
        except KeyError:
            existing = None
        if existing is None and request["scene_revision"] != scene(self.config["source"])["scene_revision"]:
            raise Conflict("scene_revision_changed")
        record, created = self.store.create(request)
        if created:
            try:
                self.launcher(request["command_id"])
            except Exception as exc:
                record = self.store.update(request["command_id"], state="unknown", error="worker_launch_uncertain: " + str(exc))
        return dict(public(record), accepted=True)


def handler(bridge, token):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, value):
            body = json.dumps(dict(value, contract_version=VERSION), allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            self.connection.settimeout(10)
            path = unquote(urlsplit(self.path).path)
            if path == "/health" and self.command == "GET":
                try:
                    info = scene(bridge.config["source"])
                    self.respond(200, {"ready": True, "backend": "simple_o7", "scene_revision": info["scene_revision"]})
                except Exception:
                    self.respond(503, {"ready": False, "error": "source_unavailable"})
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.respond(401, {"error": "unauthorized"})
                return
            try:
                body = None
                if self.command == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("invalid_body_length")
                    body = json.loads(self.rfile.read(length))
                if path == "/v1/scene" and self.command == "GET":
                    result = scene(bridge.config["source"])
                elif path == "/v1/commands" and self.command == "POST":
                    result = bridge.submit(body)
                elif path.startswith("/v1/commands/"):
                    command_id = path[len("/v1/commands/"):]
                    if command_id.endswith("/cancel") and self.command == "POST":
                        result = dict(public(bridge.store.cancel(command_id[:-7])), cancel_accepted=True)
                    elif self.command == "GET":
                        result = public(bridge.store.get(command_id))
                    else:
                        raise KeyError(path)
                else:
                    raise KeyError(path)
                self.respond(200, result)
            except KeyError:
                self.respond(404, {"error": "unknown_command_or_path"})
            except Conflict as exc:
                self.respond(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception as exc:
                print("bridge error:", repr(exc), flush=True)
                self.respond(500, {"error": "executor_internal_error"})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    config["config_path"] = str(Path(args.config).resolve())
    token = os.environ.get("SIMPLE_O7_TOKEN", "")
    if not token:
        raise SystemExit("Set SIMPLE_O7_TOKEN before exposing the execution service")
    server = ThreadingHTTPServer((config.get("host", "0.0.0.0"), config.get("port", 18770)), handler(Bridge(config), token))
    print("SIMPLE O7 bridge listening", server.server_address, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
