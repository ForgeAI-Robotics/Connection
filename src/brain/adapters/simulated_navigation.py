"""Local NAV receipts plus real VLA; Runtime remains the only durable ledger."""
from copy import deepcopy
import threading

from brain.adapters.body_control import ControlledBodyAdapter
from brain.adapters.execution import interpret_observation, _unconfirmed
from brain.adapters.http_client import HttpContractError
from brain.adapters.vla_client import VlaClient
from contracts.body_control import stamp


class SimulatedNavigationMixin:
    def __init__(self, vla, authority):
        super().__init__(None, vla, authority)
        self._navigation = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config, authority):
        real = config['reception_real']
        return cls(VlaClient(real['vla_base_url'], request_timeout_sec=float(real.get('request_timeout_sec',10))), authority)

    def submit(self, command_id, request):
        if request['skill'] != 'navigate':
            return super().submit(command_id, request)
        with self._lock:
            old = self._navigation.get(command_id)
            if old and old['request'] != request:
                raise ValueError('模拟导航原命令身份冲突')
            if not old:
                raw = dict(deepcopy(request['body']), state='succeeded', simulated=True,
                    started_at=stamp(), completed_at=stamp(), result=dict(success=True, reached=True,
                        navigation_stopped=True, simulated=True, physical_execution=False,
                        evidence_level='simulated_navigation', message='导航模拟完成，机器人未实际移动'))
                self._navigation[command_id] = dict(request=deepcopy(request), raw=raw)
        return dict(accepted=True, unclear=False, completed=True, command_id=command_id)

    def query(self, command_id, request):
        if request['skill'] != 'navigate':
            return super().query(command_id, request)
        with self._lock:
            row = self._navigation.get(command_id)
            if not row or row['request'] != request:
                return _unconfirmed(command_id, '原模拟导航回执缺失；未重新生成成功')
            raw = deepcopy(row['raw'])
        viewed = interpret_observation(command_id, raw, request)
        viewed['evidence'].update(simulated=True, physical_execution=False, grade='simulated_navigation')
        return viewed

    def wait(self, command_id, request):
        return self.query(command_id, request) if request['skill'] == 'navigate' else super().wait(command_id, request)

    def cancel(self, command_id, request):
        if request['skill'] != 'navigate':
            return super().cancel(command_id, request)
        viewed = self.query(command_id, request)
        return dict(accepted=True, completed=viewed.get('stopped') is True, command_id=command_id)

    def restore_receipts(self, record):
        if record.get('execution_backend') != 'reception_sim_navigation_real':
            return
        with self._lock:
            for step in record.get('steps', {}).values():
                for attempt in step.get('attempts', []):
                    request = attempt.get('request') or {}
                    raw = (attempt.get('progress', {}).get('evidence') or {}).get('downstream') or {}
                    if request.get('skill') != 'navigate' or raw.get('simulated') is not True:
                        continue
                    command = attempt['command_id']
                    viewed = interpret_observation(command, raw, request)
                    evidence = viewed.get('evidence') or {}
                    if (viewed.get('terminal') == 'succeeded' and evidence.get('identity_ok') is True
                            and evidence.get('time_ok') is True and raw.get('task_id') == record.get('task_id')
                            and (raw.get('result') or {}).get('physical_execution') is False):
                        self._navigation[command] = dict(request=deepcopy(request), raw=deepcopy(raw))

    def handoff_with_context(self, context):
        request = context.get('source_request') or {}
        if request.get('skill') != 'navigate':
            return super().handoff_with_context(context)
        source = self.query(context.get('source_command_id'), request)
        ready = self._source_handoff_blocker(source) is None
        return dict(available=True, confirmed=ready, kind=context['kind'], source=source,
                    simulated=True, physical_control_transfer=False,
                    reason='simulated_navigation_boundary' if ready else 'source_unconfirmed')

    def observe_recovery(self):
        return dict(scope='vla_status_not_visual', manipulation=self.vla.control_status())


class SimulatedNavigationAdapter(SimulatedNavigationMixin, ControlledBodyAdapter):
    """Original unified-control backend, unchanged."""


from brain.adapters.source_gate import SourceGateBodyAdapter


class DirectSimulatedNavigationAdapter(SimulatedNavigationMixin, SourceGateBodyAdapter):
    """Same local NAV receipts, direct-v1 VLA business calls."""
