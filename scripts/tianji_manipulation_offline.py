#!/usr/bin/env python3
"""Exercise the real Brain VLA client against a nonphysical Tianji test executor."""
import argparse
import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import io
import json
import math
import os
from pathlib import Path
import socket
import threading
import uuid

from brain.adapters.http_client import HttpContractError
from brain.adapters.tianji_camera import TianjiHeadCamera
from brain.adapters.vla_camera import VlaCameraClient
from brain.adapters.tianji_manipulation import TianjiManipulationAdapter
from brain.kernel.runner import Runner
from brain.kernel.verifier import Verifier
from brain.skills.tianji_manipulation import TianjiManipulationSkill, manipulation_request
from contracts.tasks import CONTRACT_VERSION
from PIL import Image


def stamp():
    return datetime.now(timezone.utc).isoformat()


def head_frame_fixture():
    """Synthetic image, never a camera observation or a VLM result."""
    output = io.BytesIO()
    Image.new('RGB', (16, 12), (32, 64, 96)).save(output, format='JPEG')
    raw = output.getvalue()
    now = stamp()
    return dict(schema='fq/vla-camera-frame/v1', camera_key='ego_view',
        frame_id='fixture-head-' + uuid.uuid4().hex, captured_at=now, received_at=now,
        age_ms=0, received_age_ms=0, width=16, height=12, content_type='image/jpeg',
        sha256=hashlib.sha256(raw).hexdigest(), image_base64=base64.b64encode(raw).decode('ascii'),
        service_instance_id='fixture-vla-service', camera_receiver_instance_id='fixture-head-receiver',
        robot_serial='fixture-tianji', simulated=True, test_fixture=True)


@contextmanager
def fixture_server():
    """Test-only HTTP data; no robot imports, hardware, or controller subprocess."""
    rows, posts, cancels, lock = {}, [], [], threading.RLock()
    token_env = 'TIANJI_VLA_FIXTURE_' + uuid.uuid4().hex.upper()
    token = uuid.uuid4().hex
    os.environ[token_env] = token
    event_time = datetime.now(timezone.utc).timestamp()
    camera_overrides = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, data, status=200):
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def error(self, code, status=409, **details):
            self.reply(dict(success=False, error=dict(code=code, message=code,
                                                    retryable=False, details=details)), status)

        def authorized(self):
            if self.headers.get('Authorization') == 'Bearer ' + token:
                return True
            self.error('UNAUTHORIZED', 401)
            return False

        def do_GET(self):
            if not self.authorized():
                return
            with lock:
                if self.path == '/health':
                    return self.reply(dict(contract_version=CONTRACT_VERSION, status='ok'))
                if self.path == '/v1/vla/control/status':
                    active = next((c for c, r in rows.items() if r['state'] not in
                                   {'succeeded', 'failed', 'cancelled'}), None)
                    return self.reply(dict(contract_version=CONTRACT_VERSION, active_command_id=active,
                        nav_ready=False, holding_now='UNKNOWN', simulation=dict(
                            enabled=True, physical_execution=False, contract_test=True)))
                if self.path == '/v1/vla/capabilities':
                    return self.reply(dict(contract_version=CONTRACT_VERSION, real_action_enabled=False,
                        operations={op: dict(configured=True, arms=['left', 'right'])
                                    for op in ('grasp_trash', 'dispose_trash')},
                        camera=dict(head_rgb=True, views=['ego_view'], frame_schema='fq/vla-camera-frame/v1')))
                if self.path == '/v1/camera/frames/latest?camera=ego_view':
                    return self.reply(dict(head_frame_fixture(), **camera_overrides))
                if self.path == '/healthz':
                    return self.reply(dict(status='ok', controller_connected=False))
                if self.path == '/v1/capabilities':
                    return self.reply(dict(schema_version='cleanup.v0.1-draft', result_query=True,
                        grasp_start=False, cancel=False, dispose=False, nav_ready=False))
                if self.path == '/v1/tasks/legacy-task':
                    return self.reply(dict(schema_version='cleanup.v0.1-draft', task_id='legacy-task',
                        run_id='legacy-run', arm='left', state='GRASP_CONFIRMED_HOLDING_AT_EVENT',
                        event_id='legacy-run:grasp_confirmed', event_timestamp=event_time,
                        grasp_success=True, policy_stopped_at_event=True, holding_at_event=True,
                        holding_now='UNKNOWN', nav_ready=False, target_identity_verified=False))
                if not self.path.startswith('/v1/vla/tasks/'):
                    return self.error('NOT_FOUND', 404)
                command = self.path[len('/v1/vla/tasks/'):]
                row = rows.get(command)
                if row is None or row['object_id'] == 'fixture_post_unknown':
                    return self.error('TASK_NOT_FOUND', 404)
                if row['state'] not in {'succeeded', 'failed', 'cancelled'}:
                    row['_queries'] += 1
                    obj = row['object_id']
                    if obj != 'fixture_pending' and row['_queries'] >= 2:
                        row['state'] = 'failed' if obj == 'fixture_failure' else 'succeeded'
                        row['completed_at'] = stamp()
                        result = dict(execution_mode='direct', success=row['state'] == 'succeeded',
                            policy_stopped=True, publisher_stopped=obj != 'fixture_missing_stop',
                            action_port_released=True, arms_clear=True)
                        if row['operation'] == 'grasp_trash':
                            result.update(object_grasped=True, holding=row['object_id'],
                                          holding_at_event=True, carrying_posture_confirmed=True)
                        else:
                            result.update(released=True, holding=None, object_at_target=True,
                                          bin_verified=obj != 'fixture_missing_bin')
                        proof = dict(row['_request'], source='visual_verifier', reference='fixture-observation',
                            observed_at=row['completed_at'], run_id=row['run_id'], event_id=row['event_id'],
                            target_identity_verified=obj != 'fixture_wrong_target')
                        result['evidence'] = proof
                        row['result'] = result
                        if row['state'] == 'failed':
                            row['error'] = dict(code='EXECUTION_FAILED', message='injected fixture failure',
                                                retryable=False, details={})
                    else:
                        row['state'] = 'running'
                raw = {k: deepcopy(v) for k, v in row.items() if not k.startswith('_')}
                if row['object_id'] == 'fixture_foreign':
                    raw['task_id'] = 'foreign-task'
                if row['object_id'] == 'fixture_wrong_arm':
                    raw['arm'] = 'right' if row['arm'] == 'left' else 'left'
                if row['object_id'] == 'fixture_bad_time':
                    raw['completed_at'] = 'invalid'
                return self.reply(raw)

        def do_POST(self):
            if not self.authorized():
                return
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            with lock:
                if self.path == '/v1/vla/tasks':
                    posts.append(deepcopy(body))
                    cid = body['command_id']
                    if cid in rows:
                        if rows[cid]['_request'] != body:
                            return self.error('COMMAND_ID_CONFLICT')
                        return self.reply(dict(accepted=True, command_id=cid, task_id=body['task_id'],
                                               accepted_at=rows[cid]['accepted_at']))
                    active = next((c for c, r in rows.items() if r['state'] not in
                                   {'succeeded', 'failed', 'cancelled'}), None)
                    if active:
                        return self.error('VLA_BUSY', active_command_id=active)
                    if body['object_id'] == 'fixture_unavailable':
                        return self.error('CAPABILITY_UNAVAILABLE', 501, task_id=body['task_id'],
                            command_id=cid, action_started=False, action_state_uncertain=False,
                            original_code='SKILL_NOT_READY')
                    now = stamp()
                    rows[cid] = dict(body, state='accepted', execution_mode='direct', run_id='run-' + cid,
                        event_id='run-' + cid + ':completed', accepted_at=now, started_at=now,
                        completed_at=None, result=None, simulated=True, physical_execution=False,
                        test_evidence=dict(execution_count=1, motion_output_count=0),
                        _queries=0, _request=deepcopy(body))
                    if body['object_id'] in {'fixture_post_lost', 'fixture_post_unknown'}:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    return self.reply(dict(contract_version=CONTRACT_VERSION, accepted=True,
                        task_id=body['task_id'], command_id=cid, run_id=rows[cid]['run_id'],
                        state='accepted', accepted_at=now), 202)
                prefix, suffix = '/v1/vla/tasks/', '/cancel'
                if self.path.startswith(prefix) and self.path.endswith(suffix):
                    cid = self.path[len(prefix):-len(suffix)]
                    row = rows.get(cid)
                    if row is None:
                        return self.error('TASK_NOT_FOUND', 404)
                    if row['task_id'] != body.get('task_id'):
                        return self.error('INVALID_REQUEST', 422)
                    cancels.append(cid)
                    if row['state'] not in {'succeeded', 'failed', 'cancelled'}:
                        row.update(state='cancelled', completed_at=stamp(), result=dict(execution_mode='direct',
                            success=False, policy_stopped=True, publisher_stopped=True, action_port_released=True))
                    return self.reply(dict(accepted=True, task_id=body['task_id'], command_id=cid), 202)
                return self.error('NOT_FOUND', 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        yield dict(base_url='http://127.0.0.1:' + str(server.server_port), token_env=token_env,
                   rows=rows, posts=posts, cancels=cancels, camera_overrides=camera_overrides)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        os.environ.pop(token_env, None)


def run_suite(base_url, *, token_env='TIANJI_VLA_TOKEN', source='external_vla', deadline=10):
    skill = TianjiManipulationSkill(base_url, token_env=token_env, offline=True, request_timeout_sec=2)
    report = dict(status='RUNNING', source=source, base_url=base_url, started_at=stamp(),
        physical_execution=False, workflow_registered=False, navigation_allowed=False, requests=[], cases=[],
        brain_code={cls.__name__: hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest()
                    for cls in (TianjiManipulationSkill, TianjiManipulationAdapter, TianjiHeadCamera,
                                VlaCameraClient, Runner, Verifier)})
    task, sequence = 'vla-test-' + uuid.uuid4().hex[:16], 0

    def body(obj='fixture_success', operation='grasp_trash'):
        nonlocal sequence
        sequence += 1
        value = manipulation_request(task, task + '-' + str(sequence), operation=operation,
            object_id=obj, target_area='test_table' if operation == 'grasp_trash' else 'test_bin', arm='left')
        report['requests'].append(deepcopy(value))
        return value

    def ensure(value, message):
        if not value:
            raise AssertionError(message)

    def execute(value):
        return skill.execute(value, deadline_sec=deadline, poll_interval_sec=.05)

    def wait(value):
        contract = skill.contract(value, deadline_sec=deadline, poll_interval_sec=.05)
        skill.port.wait(contract.command_id, contract.request)
        return skill.query(value)

    def case(name, action):
        report['cases'].append(dict(name=name, passed=True, result=action()))

    try:
        case('metadata_and_isolation', skill.check)

        def head_frame():
            metadata, raw = skill.capture_head()
            ensure(metadata['camera_key'] == 'ego_view' and bool(raw), 'head frame unavailable')
            ensure(metadata.get('simulated') is True and metadata.get('test_fixture') is True,
                   'fixture head image not marked as simulated')
            return dict(camera_frame=metadata, image_bytes=len(raw), observation_only=True)
        case('head_frame_contract', head_frame)

        def legacy():
            value = skill.observe_event('legacy-task', 'legacy-run', 'left')
            again = skill.observe_event('legacy-task', 'legacy-run', 'left')
            ensure(value['event_received'] and value['event_id'] == again['event_id'], 'event not repeatably identified')
            ensure(not value['task_completed'] and not value['navigation_allowed'], 'legacy event promoted to task/navigation')
            return value
        case('legacy_event_without_navigation', legacy)

        def dedup():
            value = body()
            first, second = skill.submit(value), skill.submit(value)
            ensure(first['accepted'] and second['accepted'], 'duplicate not accepted as original')
            ensure(first['raw']['accepted_at'] == second['raw']['accepted_at'], 'original receipt replaced')
            skill.port._mode(skill.port.vla.control_status())
            try:
                skill.port.vla.submit_task(dict(value, target_area='changed-area'))
                raise AssertionError('same command conflict accepted')
            except HttpContractError as exc:
                ensure(exc.status_code == 409 and exc.payload['error']['code'] == 'COMMAND_ID_CONFLICT', 'wrong conflict')
            result = wait(value)
            ensure(result['verdict'] == 'PASS', 'grasp effect did not pass')
            evidence = result['progress']['evidence']['downstream']['test_evidence']
            ensure(type(evidence.get('execution_count')) is int and evidence['execution_count'] == 1
                   and type(evidence.get('motion_output_count')) is int and evidence['motion_output_count'] == 0,
                   'execution count/zero physical output not proven by fixture')
            return result
        case('grasp_idempotence_conflict_and_success', dedup)

        def expected(obj, verdict, operation='grasp_trash'):
            value = execute(body(obj, operation))
            ensure(value['verdict'] == verdict, obj + ' misclassified')
            return value
        case('dispose_success', lambda: expected('fixture_success', 'PASS', 'dispose_trash'))
        case('unavailable_skill_rejected', lambda: expected('fixture_unavailable', 'FAIL', 'dispose_trash'))
        case('execution_failure', lambda: expected('fixture_failure', 'FAIL'))
        case('missing_stop_is_unknown', lambda: expected('fixture_missing_stop', 'UNKNOWN'))
        case('foreign_identity_is_unknown', lambda: expected('fixture_foreign', 'UNKNOWN'))
        case('missing_bin_is_not_success', lambda: expected('fixture_missing_bin', 'FAIL', 'dispose_trash'))
        case('wrong_target_is_unknown', lambda: expected('fixture_wrong_target', 'UNKNOWN'))

        def cancel():
            old = body('fixture_pending')
            ensure(skill.submit(old)['accepted'], 'pending command rejected')
            rejected = skill.submit(body())
            ensure(not rejected['accepted'] and rejected['submission_rejected']['code'] == 'VLA_BUSY', 'busy not preserved')
            ensure(skill.cancel(old)['accepted'], 'cancel rejected')
            stopped = wait(old)
            ensure(stopped['progress']['terminal'] == 'cancelled' and stopped['progress']['publisher_stopped'], 'stop not proven')
            newer = body()
            ensure(skill.submit(newer)['accepted'], 'newer command rejected')
            ensure(skill.cancel(old)['accepted'], 'old cancel not idempotent')
            result = wait(newer)
            ensure(result['verdict'] == 'PASS', 'late old cancel affected new command')
            return dict(old=stopped, newer=result)
        case('busy_cancel_and_late_old_cancel', cancel)
        case('lost_post_original_query', lambda: expected('fixture_post_lost', 'PASS'))
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
    mode.add_argument('--base-url', help='Navigation-independent, nonphysical VLA compatible instance')
    parser.add_argument('--token-env', default='TIANJI_VLA_TOKEN')
    parser.add_argument('--deadline', type=float, default=10)
    parser.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    if not math.isfinite(args.deadline) or args.deadline <= 0:
        parser.error('--deadline 必须为有限正数')
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        if args.self_test:
            with fixture_server() as fixture:
                report = run_suite(fixture['base_url'], token_env=fixture['token_env'],
                                   source='builtin_fixture', deadline=args.deadline)
        else:
            report = run_suite(args.base_url, token_env=args.token_env, deadline=args.deadline)
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(dict(status=report['status'], source=report['source'], cases=len(report['cases']),
                          report=str(path.resolve()), physical_execution=False), ensure_ascii=False))
    return 0 if report['status'] == 'OFFLINE_CONTRACT_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
