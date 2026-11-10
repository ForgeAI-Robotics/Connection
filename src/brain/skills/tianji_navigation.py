"""Standalone NAV atomic skill, using the existing Runner and Verifier.

The caller owns task/attempt identity and persistence. This module creates no
business task, retries, ledger, workflow registration or control loop.
"""
from copy import deepcopy
import math

from brain.adapters.tianji_navigation import TianjiNavigationAdapter
from brain.kernel.runner import Runner
from brain.kernel.verifier import Verifier
from brain.skills.body_requests import navigation_body
from contracts.navigation import validate_navigation_body
from contracts.steps import StepSpec
from contracts.tasks import SkillContract, CONTRACT_VERSION


def navigation_request(task_id, command_id, *, target_id, goal_xyt,
                       route_phase='', leg_index=1, motion_mode='forward_path'):
    step = StepSpec('TIANJI_NAVIGATION', 'navigate', navigation=dict(
        target_id=target_id, goal_xyt=list(goal_xyt), route_phase=route_phase,
        leg_index=leg_index, motion_mode=motion_mode))
    return validate_navigation_body(navigation_body(step, task_id, command_id))


class TianjiNavigationSkill:
    def __init__(self, base_url, **settings):
        self.port = TianjiNavigationAdapter(base_url, **settings)

    @staticmethod
    def contract(body, *, deadline_sec=900, poll_interval_sec=.5,
                 step_id='TIANJI_NAVIGATION', attempt_id=None):
        validate_navigation_body(body)
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
               for v in (deadline_sec, poll_interval_sec)):
            raise ValueError('等待期限与查询间隔必须为有限正数')
        request = dict(skill='navigate', task_id=body['task_id'], step_id=step_id,
                       deadline_sec=deadline_sec, poll_interval_sec=poll_interval_sec,
                       body=deepcopy(body))
        return SkillContract(package='atomic.tianji_navigation', skill='navigate',
            version=CONTRACT_VERSION, task_id=body['task_id'], step_id=step_id,
            attempt_id=attempt_id or body['command_id'], goal=body['target_id'], object_id='',
            preconditions='由天机执行端核验实时定位、姿态与碰撞准入',
            evidence='原命令到位与导航停止回执', failure_budget=0,
            deadline_sec=deadline_sec, command_prefix='tianji-nav', command_id=body['command_id'],
            postconditions=('reached',), handoff='', tools=('navigate',),
            requires_object_evidence=False, requires_safe_idle=False, request=request)

    def _observe(self, body, *, requery=False, **timing):
        contract = self.contract(body, **timing)
        progress = Runner().execute(contract, self.port, requery=requery)
        return dict(verdict=Verifier().judge(contract, progress),
                    scope='offline_contract' if self.port.offline else 'navigation_command',
                    workflow_registered=False, progress=progress.as_dict())

    def execute(self, body, **timing):
        return self._observe(body, **timing)

    def query(self, body, **timing):
        return self._observe(body, requery=True, **timing)

    def submit(self, body, **timing):
        contract = self.contract(body, **timing)
        return self.port.submit(contract.command_id, contract.request)

    def cancel(self, body):
        contract = self.contract(body)
        return self.port.cancel(contract.command_id, contract.request)

    def check(self):
        return self.port.check()
