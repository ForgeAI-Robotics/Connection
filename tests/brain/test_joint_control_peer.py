"""Current NAV-only Runtime integration. Old relay/VLA integration retired at dfee9bf."""
from copy import deepcopy
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch
from tests.brain.test_body_control import Fixture
from brain.adapters.body_control import ControlledBodyAdapter
from contracts.body_control import stamp


class RuntimeIntegrationTests(Fixture, unittest.TestCase):
    def test_existing_nav_only_runtime_reuses_one_gateway_session_for_all_legs(self):
        from brain.app import create_service
        from brain.packages.reception_skill import BASELINE_PATH
        from tests.brain.test_native_vla import FakeDream
        config=deepcopy(self.config)
        config.update(brain={'reception_backend':'real','capture_timeline':False,
                             'reasoning':{'recovery_advice':False}},reflection={'enabled':False})
        config['reception_real']['skill_file']=str(BASELINE_PATH)
        wire=self.wire
        class Nav(FakeDream):
            def status(self):return dict(wire.nav,updated_at=stamp())
            def command(self,command_id):
                raw=super().command(command_id);raw['current_hold']=wire.hold(raw);return raw
            def submit_navigation(self,body):
                super().submit_navigation(body)
                wire.commands=self.commands
                return {'accepted':True}
        nav=Nav()
        port=ControlledBodyAdapter(nav,SimpleNamespace(),self.authority)
        service=create_service(config,model=lambda _:self.fail('No model needed'),port_factory=lambda _:port)
        with patch.dict(os.environ,{'FQ_EXECUTION_STATE':str(self.root/'execution.json'),
                                    'FQ_EXECUTION_LOCK':str(self.root/'execution.lock'),
                                    'FQ_EXECUTION_BLOCK':str(self.root/'execution.blocked')}):
            service.publish('开始接待（仅导航）','test-task',options={'reflect':False})
        self.assertEqual(service.runtime.state,'succeeded',service.runtime.public_status())
        self.assertEqual(len(nav.commands),4)
        self.assertFalse(self.authority.recovery_status()['blocked'])
        self.assertEqual(len(self.posts('grant')),1)
