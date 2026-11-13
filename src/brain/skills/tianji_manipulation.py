"""Independent Tianji object skills; caller owns identity, persistence and SOP."""
from copy import deepcopy
import math

from brain.adapters.tianji_manipulation import TianjiManipulationAdapter
from brain.kernel.runner import Runner
from brain.kernel.verifier import Verifier
from contracts.tasks import CONTRACT_VERSION, SkillContract
from contracts.tianji_manipulation import validate_request


def manipulation_request(task_id, command_id, *, operation, object_id, target_area, arm):
    return validate_request(dict(contract_version=CONTRACT_VERSION, task_id=task_id,
        command_id=command_id, operation=operation, object_id=object_id, target_area=target_area, arm=arm))


class TianjiManipulationSkill:
    def __init__(self, base_url, **settings):
        self.port = TianjiManipulationAdapter(base_url, **settings)

    @staticmethod
    def contract(body, *, deadline_sec=300, poll_interval_sec=1,
                 step_id='TIANJI_MANIPULATION', attempt_id=None):
        validate_request(body)
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
               for v in (deadline_sec, poll_interval_sec)):
            raise ValueError('等待预算和查询间隔必须为有限正数')
        request = dict(skill=body['operation'], task_id=body['task_id'], step_id=step_id,
                       deadline_sec=deadline_sec, poll_interval_sec=poll_interval_sec, body=deepcopy(body))
        return SkillContract(package='atomic.tianji_manipulation', skill=body['operation'],
            version=CONTRACT_VERSION, task_id=body['task_id'], step_id=step_id,
            attempt_id=attempt_id or body['command_id'], goal=body['target_area'], object_id=body['object_id'],
            preconditions='天机执行端负责真实到站、姿态、控制归属及传感器准入',
            evidence='目标效果与原命令收尾证据', failure_budget=0, deadline_sec=deadline_sec,
            command_prefix='tianji-vla', command_id=body['command_id'], postconditions=(),
            handoff='', tools=(body['operation'],), requires_object_evidence=True,
            requires_safe_idle=False, request=request)

    def _observe(self, body, *, requery=False, **timing):
        contract = self.contract(body, **timing)
        progress = Runner().execute(contract, self.port, requery=requery)
        return dict(verdict=Verifier().judge(contract, progress), progress=progress.as_dict(),
                    scope='offline_contract' if self.port.offline else 'manipulation_command',
                    navigation_allowed=False, workflow_registered=False)

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

    def legacy_check(self):
        return self.port.legacy_check()

    def observe_event(self, task_id, run_id, arm, **timing):
        return self.port.observe_event(task_id, run_id, arm, **timing)

    def capture_head(self, **constraints):
        """Return (validated source metadata, JPEG bytes); no model or motion call."""
        return self.port.capture_head(**constraints)
