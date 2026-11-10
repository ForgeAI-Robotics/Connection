"""Explicit Tianji NAV port; no G1 source gate, VLA, planner or business state."""
from copy import deepcopy
import math
import re

from brain.adapters.dream_client import DreamClient
from brain.adapters.execution import BodyAdapter, _unconfirmed
from brain.adapters.http_client import HttpContractError
from contracts.navigation import validate_navigation_body
from contracts.tasks import CONTRACT_VERSION


TOKEN_ENV = 'TIANJI_NAV_TOKEN'


class TianjiNavigationAdapter(BodyAdapter):
    requires_authoritative_handoff = False

    def __init__(self, base_url, *, token_env=TOKEN_ENV, request_timeout_sec=10,
                 allow_motion=False, offline=False):
        if not isinstance(token_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', token_env):
            raise ValueError('token_env 必须为凭据环境变量名称')
        if (type(request_timeout_sec) not in (int, float)
                or not math.isfinite(request_timeout_sec) or request_timeout_sec <= 0):
            raise ValueError('HTTP 超时必须为有限正数')
        client = DreamClient(base_url, request_timeout_sec=request_timeout_sec)
        client.http.auth_token_env = token_env
        super().__init__(client, None)
        self.allow_motion = allow_motion is True
        self.offline = offline is True
        if self.allow_motion and self.offline:
            raise ValueError('真实派发与离线协议模式不能同时启用')

    @property
    def gate_open(self):
        return self.allow_motion or self.offline

    def capabilities(self):
        return dict(operations={'navigate': {'configured': True}},
                    real_action_enabled=self.allow_motion, offline=self.offline,
                    workflow_registered=False)

    @staticmethod
    def _validate(command_id, request):
        if not isinstance(request, dict) or request.get('skill') != 'navigate':
            raise ValueError('天机导航端口只接受 navigate')
        body = validate_navigation_body(request.get('body'))
        if command_id != body['command_id'] or request.get('task_id') != body['task_id']:
            raise ValueError('导航信封与请求身份不一致')
        if request.get('requires_vla_clearance') or request.get('required_holding'):
            raise ValueError('当前原子导航未接入操控交接或持物流程')
        return body

    def _mode(self, status, *, dispatch=False):
        if (not isinstance(status, dict) or status.get('contract_version') != CONTRACT_VERSION
                or status.get('interface_online') is not True):
            raise ValueError('天机导航未确认合同版本和接口在线')
        sim = status.get('simulation') or {}
        if not isinstance(sim, dict):
            raise ValueError('导航 simulation 声明格式无效')
        if self.offline:
            if not (sim.get('enabled') is True and sim.get('physical_execution') is False
                    and sim.get('contract_test') is True):
                raise ValueError('离线模式要求隔离协议执行器的明确声明')
        elif sim.get('enabled') is True or sim.get('physical_execution') is False:
            raise ValueError('真实导航端口不接受协议模拟实例')
        if dispatch and not self.gate_open:
            raise ValueError('真实导航派发未显式启用 allow_motion')

    def check(self):
        health, status, world = self.dream.health(), self.dream.status(), self.dream.world()
        self._mode(status)
        if health.get('contract_version') != CONTRACT_VERSION or health.get('interface_online') is not True:
            raise ValueError('导航健康合同无效')
        if world.get('contract_version') != CONTRACT_VERSION or world.get('frame_id') != 'map':
            raise ValueError('导航世界合同或坐标系无效')
        graph = self.dream.relation_graph(world)
        contract = graph.get('agent_navigation_contract') or {}
        if (graph.get('frame_id') != 'map' or not isinstance(contract, dict)
                or contract.get('contract_version') != CONTRACT_VERSION
                or contract.get('frame_id') != 'map'):
            raise ValueError('关系图缺少 map 导航能力声明')
        return dict(health=health, status=status, world=world, graph=graph,
                    scope='offline_contract' if self.offline else 'read_only_metadata')

    def submit(self, command_id, request):
        body = self._validate(command_id, request)
        try:
            self._mode(self.dream.status(), dispatch=True)
        except (HttpContractError, ValueError) as exc:
            # This invocation did not POST. Without the caller's ledger we cannot
            # claim the same identity has never run during an earlier invocation.
            raw = dict(success=False, error=dict(code='TIANJI_DISPATCH_BLOCKED', message=str(exc),
                details=dict(task_id=body['task_id'], command_id=command_id,
                             current_call_not_submitted=True,
                             source='brain_tianji_before_business_submission')))
            return dict(accepted=False, unclear=True, completed=False, command_id=command_id,
                        raw=raw, error=str(exc), submission_rejected=None)
        return super().submit(command_id, request)

    def _scope_observation(self, command_id, viewed):
        raw = viewed.get('raw') or {}
        result = raw.get('result') if isinstance(raw.get('result'), dict) else {}
        simulated = raw.get('simulated') is True or result.get('simulated') is True
        nonphysical = raw.get('physical_execution') is False or result.get('physical_execution') is False
        if (self.offline and not (raw.get('simulated') is True and raw.get('physical_execution') is False)
                or not self.offline and (simulated or nonphysical)):
            return _unconfirmed(command_id, 'navigation_execution_scope_mismatch', raw=raw)
        viewed = deepcopy(viewed)
        if self.offline:
            viewed['evidence'].update(simulated=True, physical_execution=False)
        return viewed

    def query(self, command_id, request):
        self._validate(command_id, request)
        return self._scope_observation(command_id, super().query(command_id, request))

    def wait(self, command_id, request):
        self._validate(command_id, request)
        return self._scope_observation(command_id, super().wait(command_id, request))

    def cancel(self, command_id, request):
        body = self._validate(command_id, request)
        # An offline client must never cancel a real executor's command.
        if self.offline:
            self._mode(self.dream.status())
        try:
            raw = self.dream.cancel(body['task_id'], command_id)
        except HttpContractError as exc:
            return dict(accepted=False, completed=False, command_id=command_id,
                        error=str(exc), raw=exc.payload)
        matched = (raw.get('command_id') == command_id and raw.get('task_id') == body['task_id']
                   and raw.get('accepted') is True)
        return dict(accepted=matched, completed=False, command_id=command_id, raw=raw,
                    **({} if matched else {'error': 'cancel_acceptance_or_identity_unconfirmed'}))
