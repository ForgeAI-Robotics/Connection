"""Current control-session exit observations never rewrite historical unknowns."""
from copy import deepcopy
import unittest
from contracts.body_control import ControlError
from tests.brain.test_body_control import Fixture


class CurrentExitTests(Fixture,unittest.TestCase):
    def receipt(self):
        self.prepare()
        row=self.row();q=dict(self.driver.payload(row,'yield'),request_id='yield-1')
        r=self.wire.call('nav','POST','/v1/navigation/handoff/yield',q)
        return r,q

    def test_current_yield_matches_exact_control_session(self):
        r,q=self.receipt()
        v=self.driver.operation_receipt(r,q,'nav')
        self.assertTrue(v['navigation_stopped']);self.assertTrue(v['output_fenced'])

    def test_historical_or_foreign_instance_cannot_prove_exit(self):
        r,q=self.receipt()
        for mutate in (lambda v:v.update(historical=True),
                       lambda v:v.update(query_nav_instance_id='new'),
                       lambda v:v['identity'].update(control_session_id='foreign'),
                       lambda v:v['freshness'].update(body_sample_fresh=False)):
            v=deepcopy(r);mutate(v)
            with self.assertRaises(ControlError):self.driver.operation_receipt(v,q,'nav')

    def test_unknown_receipt_remains_unknown_even_with_current_exit(self):
        r,q=self.receipt();r.update(state='unknown',accepted=False)
        r['current_exit']={'exit_confirmed':True}
        self.assertEqual(self.driver.operation_receipt(r,q,'nav')['state'],'unknown')
