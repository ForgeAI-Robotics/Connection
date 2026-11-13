"""Standalone Tianji VLA I/O using VlaClient; no robot/workflow/state ownership."""
from copy import deepcopy
import math
import re
from urllib.parse import quote

from brain.adapters.execution import _unconfirmed
from brain.adapters.http_client import HttpContractError
from brain.adapters.vla_client import VlaClient
from contracts.tasks import CONTRACT_VERSION
from contracts.submission import submission_refusal
from contracts.tianji_manipulation import (OPERATIONS, IDENTITY_FIELDS, validate_request,
                                          validate_effect, timestamp, observe_legacy_event)

TOKEN_ENV = 'TIANJI_VLA_TOKEN'


class TianjiManipulationAdapter:
    contract_version = CONTRACT_VERSION
    requires_authoritative_handoff = False

    def __init__(self, base_url, *, token_env=TOKEN_ENV, request_timeout_sec=10,
                 allow_motion=False, offline=False):
        if not isinstance(token_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', token_env):
            raise ValueError('token_env 必须为凭据变量名')
        if (type(request_timeout_sec) not in (float, int) or not math.isfinite(request_timeout_sec)
                or request_timeout_sec <= 0 or allow_motion and offline):
            raise ValueError('HTTP 超时须为有限正数；真实与离线模式互斥')
        self.vla = VlaClient(base_url, request_timeout_sec=request_timeout_sec)
        self.vla.http.auth_token_env = token_env
        self.allow_motion, self.offline = allow_motion is True, offline is True

    @property
    def gate_open(self):
        return self.allow_motion or self.offline

    def abandon_waits(self):
        self.vla.abandon_waits()

    @staticmethod
    def _validate(command_id, request):
        body = validate_request(request.get('body') if isinstance(request, dict) else None)
        if (command_id != body['command_id'] or request.get('task_id') != body['task_id']
                or request.get('skill') != body['operation']):
            raise ValueError('天机操控信封身份不一致')
        return body

    def _mode(self, status):
        if not isinstance(status, dict) or status.get('contract_version') != CONTRACT_VERSION:
            raise ValueError('操控状态合同无效')
        sim = status.get('simulation') or {}
        if not isinstance(sim, dict):
            raise ValueError('simulation 声明无效')
        if self.offline:
            if not (sim.get('enabled') is True and sim.get('physical_execution') is False
                    and sim.get('contract_test') is True):
                raise ValueError('离线调用要求明确的无动作隔离执行器')
        elif sim.get('enabled') is True or sim.get('physical_execution') is False:
            raise ValueError('真实操控客户端拒绝模拟实例')

    def check(self):
        health, status, caps = self.vla.health(), self.vla.control_status(), self.vla.capabilities()
        self._mode(status)
        if health.get('contract_version') != CONTRACT_VERSION or caps is None:
            raise ValueError('操控健康或能力声明缺失')
        if not OPERATIONS <= caps['operations'].keys():
            raise ValueError('能力声明须列出抓取和投放，包括尚不可用的能力')
        camera = caps.get('camera')
        if (not isinstance(camera, dict) or camera.get('head_rgb') is not True
                or camera.get('frame_schema') != 'fq/vla-camera-frame/v1'
                or not isinstance(camera.get('views'), list) or 'ego_view' not in camera['views']):
            raise ValueError('能力声明缺少已配置的头部取图接口；当前帧就绪仍需实际 GET 核验')
        return dict(health=health, control_status=status, capabilities=caps,
                    workflow_registered=False, scope='offline_contract' if self.offline else 'metadata_only')

    def capabilities(self):
        return self.vla.capabilities()

    def submit(self, command_id, request):
        body = self._validate(command_id, request)
        try:
            self._mode(self.vla.control_status())
            if not self.gate_open:
                raise ValueError('真实动作派发需要显式 allow_motion')
        except (ValueError, HttpContractError) as exc:
            # A local block proves only this invocation did not POST. The same
            # identity may already exist remotely from a prior invocation.
            return dict(accepted=False, unclear=True, completed=False, command_id=command_id,
                        error=str(exc), raw=None, submission_rejected=None)
        try:
            raw = self.vla.submit_task(body)
            return dict(accepted=True, unclear=False, completed=False, command_id=command_id, raw=raw)
        except HttpContractError as exc:
            refusal = submission_refusal(exc.status_code, exc.payload,
                                         command_id=command_id, task_id=body['task_id'])
            return dict(accepted=False, unclear=refusal is None, completed=False,
                        command_id=command_id, error=str(exc), raw=exc.payload, submission_rejected=refusal)

    def _view(self, command_id, raw, body):
        if not isinstance(raw, dict):
            return _unconfirmed(command_id, 'invalid_manipulation_response')
        result = raw.get('result') if isinstance(raw.get('result'), dict) else {}
        state = str(raw.get('state') or '').lower()
        terminal = state if state in {'succeeded', 'failed', 'cancelled'} else ''
        identity = all(raw.get(k) == body[k] for k in IDENTITY_FIELDS)
        simulated = raw.get('simulated') is True or result.get('simulated') is True
        nonphysical = raw.get('physical_execution') is False or result.get('physical_execution') is False
        scope_ok = (raw.get('simulated') is True and raw.get('physical_execution') is False
                    if self.offline else not simulated and not nonphysical)
        direct = (raw.get('execution_mode') == 'direct'
                  and (result.get('execution_mode') == 'direct' if terminal
                       else result.get('execution_mode') in (None, 'direct')))
        if not identity or not scope_ok or not direct:
            return _unconfirmed(command_id, 'manipulation_identity_scope_or_mode_mismatch', raw=raw)
        stopped = all(result.get(k) is True for k in
                      ('policy_stopped', 'publisher_stopped', 'action_port_released'))
        proof_ok = validate_effect(raw, body)
        contradicts = result.get('success') is False
        effect_fields = ('object_grasped', 'holding_at_event', 'carrying_posture_confirmed') \
            if body['operation'] == 'grasp_trash' else ('released', 'bin_verified', 'object_at_target')
        contradicts = contradicts or any(result.get(k) is False for k in effect_fields)
        supports = terminal == 'succeeded' and proof_ok and stopped
        evidence = dict(identity_ok=True, time_ok=timestamp(raw.get('completed_at')) is not None,
            supports=supports, contradicts=contradicts, grade='object' if proof_ok else '',
            reported_success=state == 'succeeded' and result.get('success') is True,
            event_id=raw.get('event_id'), run_id=raw.get('run_id'),
            effect='object_held' if body['operation'] == 'grasp_trash' else 'object_in_bin')
        if self.offline:
            evidence.update(simulated=True, physical_execution=False)
        return dict(command_id=command_id, terminal=terminal, timed_out=False,
            started=True if raw.get('started_at') else None, stopped=stopped,
            resources_released=stopped, raw=deepcopy(raw), evidence=evidence)

    def query(self, command_id, request):
        body = self._validate(command_id, request)
        try:
            return self._view(command_id, self.vla.task(command_id), body)
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)

    def wait(self, command_id, request):
        body = self._validate(command_id, request)
        try:
            raw = self.vla.wait_task(command_id, timeout_sec=request['deadline_sec'],
                poll_interval_sec=request['poll_interval_sec'])
            return self._view(command_id, raw, body)
        except HttpContractError as exc:
            return _unconfirmed(command_id, str(exc), raw=exc.payload)

    def cancel(self, command_id, request):
        body = self._validate(command_id, request)
        if self.offline:
            self._mode(self.vla.control_status())
        try:
            raw = self.vla.cancel(body['task_id'], command_id)
        except HttpContractError as exc:
            return dict(accepted=False, completed=False, command_id=command_id, raw=exc.payload, error=str(exc))
        matched = raw.get('task_id') == body['task_id'] and raw.get('command_id') == command_id \
            and raw.get('accepted') is True
        return dict(accepted=matched, completed=False, command_id=command_id, raw=raw)

    def legacy_check(self):
        return dict(scope='legacy_result_gateway_only',
                    health=self.vla.http.request_json('GET', '/healthz')[0],
                    capabilities=self.vla.http.request_json('GET', '/v1/capabilities')[0])

    def observe_event(self, task_id, run_id, arm, *, receipt_ttl_sec=5):
        # Validate caller identity before any HTTP. No legacy POST is exposed.
        observe_legacy_event({}, task_id=task_id, run_id=run_id, arm=arm, receipt_ttl_sec=receipt_ttl_sec)
        raw = self.vla.http.request_json('GET', '/v1/tasks/' + quote(task_id, safe=''))[0]
        return observe_legacy_event(raw, task_id=task_id, run_id=run_id, arm=arm, receipt_ttl_sec=receipt_ttl_sec)

    def capture_head(self, **constraints):
        from brain.adapters.tianji_camera import TianjiHeadCamera
        return TianjiHeadCamera(self.vla.http.base_url, token_env=self.vla.http.auth_token_env,
                                offline=self.offline).capture(**constraints)
