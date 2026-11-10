#!/usr/bin/env python3
"""Test the real Brain Tianji NAV port against an isolated protocol executor.

--self-test uses an in-process HTTP fixture. --base-url tests the navigation
maintainer's compatible service, which must explicitly declare isolation.
Neither mode starts a robot service or changes the Brain workflow/configuration.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import inspect
import math
import os
from pathlib import Path
import socket
import threading
import uuid

from brain.adapters.http_client import HttpContractError
from brain.adapters.tianji_navigation import TianjiNavigationAdapter
from brain.kernel.runner import Runner
from brain.kernel.verifier import Verifier
from brain.skills.tianji_navigation import TianjiNavigationSkill, navigation_request
from contracts.tasks import CONTRACT_VERSION


def stamp():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def fixture_server():
    """Test-only, loopback-only fake executor; no ROS, SDK, subprocess or motion I/O."""
    token_env = 'TIANJI_NAV_FIXTURE_TOKEN'
    token = uuid.uuid4().hex
    previous = os.environ.get(token_env)
    os.environ[token_env] = token
    rows, posts, lock = {}, [], threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, value, status=200):
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def error(self, code, status=409, **details):
            self.reply(dict(success=False, error=dict(code=code, message=code,
                            retryable=False, details=details)), status)

        def authorized(self):
            if self.headers.get('Authorization') != 'Bearer ' + token:
                self.error('UNAUTHORIZED', 401)
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            with lock:
                common = dict(contract_version=CONTRACT_VERSION, interface_online=True)
                if self.path == '/health':
                    return self.reply(dict(common, service='tianji-nav-contract-fixture', status='ok'))
                if self.path == '/v1/status':
                    active = next((cid for cid, row in rows.items()
                                   if row['state'] not in {'succeeded', 'failed', 'cancelled'}), None)
                    return self.reply(dict(common, motion_ready=False,
                        motion_blockers=['NONPHYSICAL_CONTRACT_FIXTURE'], active_command_id=active,
                        active_command_state=rows[active]['state'] if active else None,
                        simulation=dict(enabled=True, physical_execution=False, contract_test=True)))
                if self.path == '/v1/world':
                    return self.reply(dict(common, frame_id='map',
                        relation_graph_url='/total_scene_graph_latest.json', updated_at=stamp()))
                if self.path == '/total_scene_graph_latest.json':
                    return self.reply(dict(frame_id='map', objects=[], agent_navigation_contract=dict(
                        contract_version=CONTRACT_VERSION, frame_id='map', legs=[],
                        goal_admission=dict(strategy='independent_map_goal',
                                            reviewed_coordinates_enforced=False, route_metadata_required=False))))
                if self.path.startswith('/v1/commands/'):
                    cid = self.path[len('/v1/commands/'):]
                    row = rows.get(cid)
                    if row is None or row['target_id'] == 'contract_post_unknown':
                        return self.error('COMMAND_NOT_FOUND', 404)
                    if row['state'] not in {'failed', 'cancelled', 'succeeded'}:
                        row['_reads'] += 1
                        target = row['target_id']
                        if target not in {'contract_cancel', 'contract_pending'} and row['_reads'] >= 2:
                            row['state'] = 'failed' if target == 'contract_failure' else 'succeeded'
                            row['completed_at'] = stamp()
                            row['result'] = dict(success=row['state'] == 'succeeded',
                                reached=row['state'] == 'succeeded',
                                navigation_stopped=target != 'contract_stop_unknown',
                                final_xyt=list(row['goal_xyt']))
                            if target == 'contract_failure':
                                row['error'] = dict(code='NAVIGATION_EXECUTION_FAILED',
                                    message='isolated injected failure', retryable=False, details={})
                        else:
                            row['state'] = 'navigating'
                    raw = {k: deepcopy(v) for k, v in row.items() if not k.startswith('_')}
                    if row['target_id'] == 'contract_foreign':
                        raw['task_id'] = 'foreign-task'
                    if row['target_id'] == 'contract_bad_time' and raw.get('completed_at'):
                        raw['completed_at'] = 'not-a-timestamp'
                    return self.reply(raw)
                return self.error('NOT_FOUND', 404)

        def do_POST(self):
            if not self.authorized():
                return
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            with lock:
                if self.path == '/v1/navigation/goals':
                    posts.append(deepcopy(body))
                    if body.get('frame_id') != 'map':
                        return self.error('INVALID_FRAME_ID', 422)
                    cid = body['command_id']
                    old = rows.get(cid)
                    if old:
                        if old['_request'] != body:
                            return self.error('COMMAND_ID_CONFLICT')
                        return self.reply(dict(accepted=True, command_id=cid, task_id=body['task_id'],
                                               accepted_at=old['accepted_at'], state=old['state']), 200)
                    active = next((c for c, r in rows.items()
                                   if r['state'] not in {'succeeded', 'failed', 'cancelled'}), None)
                    if active:
                        return self.error('NAVIGATION_BUSY', active_command_id=active)
                    now = stamp()
                    rows[cid] = dict(deepcopy(body), state='accepted', accepted_at=now, started_at=now,
                        completed_at=None, result=None, simulated=True, physical_execution=False,
                        test_evidence=dict(execution_count=1, motion_output_count=0),
                        _request=deepcopy(body), _reads=0)
                    if body['target_id'] in {'contract_post_lost', 'contract_post_unknown'}:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    return self.reply(dict(contract_version=CONTRACT_VERSION, accepted=True,
                        task_id=body['task_id'], command_id=cid, target_id=body['target_id'],
                        state='accepted', status_url='/v1/commands/' + cid, accepted_at=now), 202)
                if self.path == '/v1/navigation/cancel':
                    row = rows.get(body.get('command_id'))
                    if row is None:
                        return self.error('COMMAND_NOT_FOUND', 404)
                    if row['task_id'] != body.get('task_id'):
                        return self.error('INVALID_REQUEST', 422)
                    if row['state'] not in {'succeeded', 'failed', 'cancelled'}:
                        row.update(state='cancelled', completed_at=stamp(),
                                   result=dict(success=False, reached=False, navigation_stopped=True))
                    return self.reply(dict(accepted=True, task_id=row['task_id'],
                                           command_id=row['command_id']), 202)
                return self.error('NOT_FOUND', 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield dict(base_url='http://127.0.0.1:' + str(server.server_port), token_env=token_env,
                   rows=rows, posts=posts)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if previous is None:
            os.environ.pop(token_env, None)
        else:
            os.environ[token_env] = previous


def run_suite(base_url, *, token_env='TIANJI_NAV_TOKEN', source='external_navigation', deadline=10):
    skill = TianjiNavigationSkill(base_url, token_env=token_env, offline=True, request_timeout_sec=2)
    report = dict(contract_version=CONTRACT_VERSION, source=source, base_url=base_url,
        started_at=stamp(), physical_execution=False, workflow_registered=False,
        brain_code={cls.__name__: hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest()
                    for cls in (TianjiNavigationSkill, TianjiNavigationAdapter, Runner, Verifier)},
        status='RUNNING', requests=[], cases=[])
    task = 'nav-contract-' + uuid.uuid4().hex
    sequence = 0

    def request(target):
        nonlocal sequence
        sequence += 1
        body = navigation_request(task, task + '-' + str(sequence), target_id=target,
                                  goal_xyt=[1.2, -.8, 1.57], leg_index=sequence)
        report['requests'].append(deepcopy(body))
        return body

    def ensure(condition, message):
        if not condition:
            raise AssertionError(message)

    def execute(body):
        return skill.execute(body, deadline_sec=deadline, poll_interval_sec=.05)

    def raw_of(result):
        return result['progress']['evidence'].get('downstream') or {}

    def expect_error(body, code):
        skill.port._mode(skill.port.dream.status(), dispatch=True)
        try:
            skill.port.dream.submit_navigation(body)
        except HttpContractError as exc:
            ensure(exc.status_code == 409 and (exc.payload.get('error') or {}).get('code') == code,
                   'expected HTTP 409 ' + code)
            return exc.payload
        raise AssertionError('expected refusal: ' + code)

    def case(name, action):
        result = action()
        report['cases'].append(dict(name=name, passed=True, result=result))

    try:
        # Refuse to POST anything until the external service explicitly confirms
        # a test-only, nonphysical executor on the same authenticated connection.
        case('metadata_and_isolation', skill.check)

        def dedup():
            body = request('contract_success')
            first = skill.submit(body)
            ensure(first.get('accepted') is True, 'initial command not accepted')
            second = skill.submit(body)
            ensure(second.get('accepted') is True, 'same command did not return original transaction')
            ensure(first['raw'].get('accepted_at') == second['raw'].get('accepted_at'), 'accepted_at changed')
            changed = dict(body, goal_xyt=[1.3, -.8, 1.57])
            conflict = expect_error(changed, 'COMMAND_ID_CONFLICT')
            result = execute_query(body)
            ensure(result['verdict'] == 'PASS', 'success receipt did not pass Brain verifier')
            evidence = raw_of(result).get('test_evidence') or {}
            ensure(type(evidence.get('execution_count')) is int and evidence['execution_count'] == 1,
                   'fixture must prove execution_count=1')
            ensure(type(evidence.get('motion_output_count')) is int and evidence['motion_output_count'] == 0,
                   'fixture must prove motion_output_count=0')
            return dict(observation=result, conflict=conflict)

        def execute_query(body):
            # Wait on the already submitted original command; never POST again.
            contract = skill.contract(body, deadline_sec=deadline, poll_interval_sec=.05)
            skill.port.wait(contract.command_id, contract.request)
            return skill.query(body)

        case('same_command_idempotence_conflict_and_success', dedup)

        def failure():
            result = execute(request('contract_failure'))
            ensure(result['verdict'] == 'FAIL' and raw_of(result).get('error'), 'failure missing or misclassified')
            return result
        case('execution_failure', failure)

        for target in ('contract_stop_unknown', 'contract_foreign', 'contract_bad_time'):
            def negative(target=target):
                result = execute(request(target))
                ensure(result['verdict'] == 'UNKNOWN', target + ' incorrectly accepted')
                return result
            case(target, negative)

        def cancellation():
            body = request('contract_cancel')
            ensure(skill.submit(body)['accepted'], 'cancel command not accepted')
            busy = expect_error(request('contract_pending'), 'NAVIGATION_BUSY')
            ensure(busy['error']['details'].get('active_command_id') == body['command_id'], 'busy identity wrong')
            ensure(skill.cancel(body)['accepted'], 'cancel not accepted')
            stopped = execute_query(body)
            ensure(stopped['progress']['terminal'] == 'cancelled' and stopped['progress']['publisher_stopped'],
                   'cancel terminal/stop missing')
            # Late duplicate cancel must not cancel a later original command.
            newer = request('contract_success')
            ensure(skill.submit(newer)['accepted'], 'later command not accepted')
            ensure(skill.cancel(body)['accepted'], 'duplicate old cancel not accepted')
            result = execute_query(newer)
            ensure(result['verdict'] == 'PASS', 'old cancellation affected new command')
            return dict(cancelled=stopped, next_command=result)
        case('busy_cancel_and_late_old_cancel', cancellation)

        def lost():
            result = execute(request('contract_post_lost'))
            ensure(result['verdict'] == 'PASS', 'lost POST response not recovered by original-ID query')
            return result
        case('post_response_lost_original_query', lost)
        report['status'] = 'OFFLINE_CONTRACT_PASS'
    except (AssertionError, ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        report['status'] = 'OFFLINE_CONTRACT_FAIL'
        report['cases'].append(dict(name='blocking_failure', passed=False, error=str(exc)))
    report['completed_at'] = stamp()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--self-test', action='store_true')
    mode.add_argument('--base-url', help='Navigation-side isolated compatible service; never a real-motion instance')
    parser.add_argument('--token-env', default='TIANJI_NAV_TOKEN')
    parser.add_argument('--deadline', type=float, default=10)
    parser.add_argument('--report', required=True, help='New JSON report path; existing files are not overwritten')
    args = parser.parse_args(argv)
    if not math.isfinite(args.deadline) or args.deadline <= 0:
        parser.error('--deadline 必须为有限正数')
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve before any HTTP to prevent overwriting a prior evidence file.
    with path.open('x', encoding='utf-8') as handle:
        if args.self_test:
            with fixture_server() as fixture:
                report = run_suite(fixture['base_url'], token_env=fixture['token_env'], source='builtin_fixture',
                                   deadline=args.deadline)
        else:
            report = run_suite(args.base_url, token_env=args.token_env, deadline=args.deadline)
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(dict(status=report['status'], source=report['source'], report=str(path.resolve()),
                          cases=len(report['cases']), physical_execution=False), ensure_ascii=False))
    return 0 if report['status'] == 'OFFLINE_CONTRACT_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
