"""Scene mapping and UNIX-socket transport to the existing isolated 8001 service.

No command execution, task queue or success synthesis lives here. The original
8001 service owns command results; Brain owns the reception task. Only the
reviewed scene goals change. Manipulation is explicitly unavailable.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
from socketserver import ThreadingMixIn, UnixStreamServer
import threading
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler


def translate(body, contract, goals):
    required = {'contract_version', 'task_id', 'command_id', 'target_id', 'route_phase',
                'leg_index', 'frame_id', 'goal_xyt', 'motion_mode', 'require_final_orientation'}
    if not isinstance(body, dict) or set(body) != required:
        raise ValueError('Invalid navigation request fields')
    if body['contract_version'] != 'fq/reception-lan/v1':
        raise ValueError('Invalid contract version')
    if body['frame_id'] != 'map' or body['require_final_orientation'] is not True:
        raise ValueError('Expected map frame and final orientation')
    for key in ('task_id', 'command_id'):
        if not isinstance(body[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', body[key]):
            raise ValueError('Invalid ' + key)
    if type(body['leg_index']) is not int:
        raise ValueError('Invalid leg index')
    leg = next((v for v in contract.values() if v['leg_index'] == body['leg_index']), None)
    if leg is None or any(body.get(k) != v for k, v in leg.items()):
        raise ValueError('Request differs from the Brain reception contract')
    result = deepcopy(body)
    result['goal_xyt'] = list(goals[str(body['leg_index'])])
    return result


class Bridge:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(ProxyHandler({}))
        self.lock = threading.Lock()

    def upstream(self, path, payload=None):
        request = Request('http://127.0.0.1:8001' + path,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        try:
            response = self.opener.open(request, timeout=8)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.load(response)

    def handle(self, method, path, body=None):
        role = 'nav' if path.startswith('/reception/nav/') else 'vla'
        simulation = dict(kind='simple_physics', role=role,
                          instance_id=self.config['instance_id'], scene='office_ros_8001',
                          navigation='original 8001 -> original 9882 -> ROS -> SONIC -> MuJoCo',
                          navigation_only=True,
                          manipulation_supported=False, network='Docker network none; UNIX socket through SSH')
        if 'brain_contract' in self.config:
            simulation['navigation_legs'] = deepcopy(self.config['brain_contract'])
        if method == 'GET' and path in ('/reception/nav/health', '/reception/vla/health'):
            code, health = self.upstream('/health')
            return code, {**health, 'simulation': simulation}
        if not path.startswith('/reception/nav/'):
            return 501, {'success': False, 'error': {'code': 'UNSUPPORTED_CAPABILITY',
                         'message': 'This physical simulation supports navigation only', 'retryable': False}}
        sub = path.removeprefix('/reception/nav')
        if method == 'POST' and sub == '/v1/navigation/goals':
            mapped = translate(body, self.config['brain_contract'], self.config['office_goals'])
        elif method == 'POST' and sub == '/v1/navigation/cancel':
            mapped = body
        elif method == 'GET' and (sub in ('/v1/status', '/v1/world', '/total_scene_graph_latest.json')
                                 or re.fullmatch(r'/v1/commands/[A-Za-z0-9_.:-]+', sub)):
            mapped = None
        else:
            return 404, {'success': False, 'error': {'code': 'HTTP_ERROR', 'message': 'Unsupported path'}}
        code, value = self.upstream(sub, mapped)
        # Preserve upstream state, evidence, identity and coordinates verbatim.
        value['simulation'] = simulation
        event = dict(time=datetime.now(timezone.utc).isoformat(), method=method, path=sub,
                     brain_request=body, mapped_request=mapped, status=code, response=value)
        with self.lock, Path(self.config['events']).open('a') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        return code, value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    parser.add_argument('socket', type=Path)
    args = parser.parse_args()
    bridge = Bridge(json.loads(args.config.read_text()))

    class Handler(BaseHTTPRequestHandler):
        def run_request(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)))) if self.command == 'POST' else None
                code, value = bridge.handle(self.command, self.path, body)
            except (ValueError, TypeError) as exc:
                code, value = 400, {'success': False, 'error': {'code': 'INVALID_REQUEST', 'message': str(exc), 'retryable': False}}
            except Exception as exc:
                code, value = 502, {'success': False, 'error': {'code': 'UPSTREAM_UNAVAILABLE', 'message': str(exc)}}
            raw = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        do_GET = do_POST = run_request
        def log_message(self, *args):
            pass

    class Server(ThreadingMixIn, UnixStreamServer):
        daemon_threads = True

    # A fresh run requires a fresh socket, never replace another running service.
    with Server(str(args.socket), Handler) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
