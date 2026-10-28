import unittest
from unittest.mock import patch,Mock
from ops import gateway_service
from ops.services import by_id
from contracts.body_control import VERSION,REVISION


class GatewayServiceTests(unittest.TestCase):
    def setUp(self):
        self.config={'reception_real':{'body_control':{'gateway_base_url':'https://gateway.invalid:18001',
            'gateway_ca_file':'/private/trusted.crt','gateway_token_env':'GATEWAY_TOKEN'}}}
        self.value={'contract_version':VERSION,'coordination_revision':REVISION,
            'gateway_instance_id':'gateway-1','permit':None,'native':None,'observation_error':'NATIVE_DISCOVERY_PENDING'}
    def health(self,value=None):
        client=Mock();client.request_json.return_value=(value or self.value,200,{})
        with patch.object(gateway_service,'load_config',return_value=self.config),patch.object(gateway_service,'HttpClient',return_value=client) as factory:
            result=gateway_service.health(by_id('gateway'))
        return result,client,factory
    def test_read_only_gateway_status_does_not_claim_robot_readiness(self):
        result,client,factory=self.health()
        client.request_json.assert_called_once_with('GET','/v1/body-input/status')
        self.assertTrue(result['ok']);self.assertFalse(result['physical_readiness_verified'])
        self.assertIn('NATIVE_DISCOVERY_PENDING',result['detail'])
        self.assertEqual(client.auth_token_env,'GATEWAY_TOKEN')
        self.assertEqual(factory.call_args.kwargs['ca_file'],'/private/trusted.crt')
    def test_old_contract_not_healthy(self):
        result,_,_=self.health(dict(self.value,coordination_revision='old'))
        self.assertFalse(result['ok'])
    def test_gateway_has_no_robot_restart_button_or_relay_catalog_entry(self):
        self.assertFalse(by_id('gateway').controllable);self.assertFalse(by_id('gateway').remote_actions)
        with self.assertRaises(KeyError):by_id('relay')
    def test_http_or_missing_certificate_is_refused(self):
        self.config['reception_real']['body_control']['gateway_base_url']='http://gateway.invalid'
        result,_,factory=self.health();self.assertFalse(result['ok']);factory.assert_not_called()
