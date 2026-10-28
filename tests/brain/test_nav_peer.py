"""NAV external receipt mapping, including early registration and current proof checks."""
from copy import deepcopy
import unittest
from contracts.body_control import ControlError
from tests.brain.test_body_control import Fixture, nav_request


class NavigationPeerTests(Fixture,unittest.TestCase):
    def test_registered_receipt_is_not_preparation_complete(self):
        self.wire.block_prepare=True
        with self.assertRaises(ControlError):self.prepare()
        self.assertTrue(self.posts('grant'));self.assertFalse(self.posts('allow_execute'))
    def test_historical_nav_prepare_does_not_authorize_execution(self):
        original=self.wire.call
        def call(service,method,path,payload=None):
            value=original(service,method,path,payload)
            if service=='nav' and value.get('action')=='prepare' and value.get('state')=='completed':value['historical']=True
            return value
        self.wire.call=call
        with self.assertRaises(ControlError):self.prepare()
        self.assertFalse(self.posts('allow_execute'))
    def test_preparation_cannot_change_registered_publisher(self):
        original=self.wire.call
        def call(service,method,path,payload=None):
            value=original(service,method,path,payload)
            if service=='nav' and value.get('action')=='prepare' and value.get('state')=='completed':
                value['evidence']['publisher']['input_session_id']='f'*32
            return value
        self.wire.call=call
        with self.assertRaises(ControlError):self.prepare()
        self.assertFalse(self.posts('allow_execute'))
    def test_static_prepare_requires_actual_fresh_output(self):
        original=self.wire.call
        def call(service,method,path,payload=None):
            value=original(service,method,path,payload)
            if service=='nav' and value.get('action')=='prepare' and value.get('state')=='completed':
                value['evidence']['fresh_preparation_output']=None
            return value
        self.wire.call=call
        with self.assertRaises(ControlError):self.prepare()
        self.assertFalse(self.posts('allow_execute'))
