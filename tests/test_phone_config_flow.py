"""Offline config flow tests with synthetic Home Assistant and API modules."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


class FlowAbort(Exception):
    pass


def load_flow():
    root = Path(__file__).resolve().parents[1] / 'custom_components' / 'qubo'
    pkg = types.ModuleType('_qubo_phone_flow_tests')
    pkg.__path__ = [str(root)]
    ha = types.ModuleType('homeassistant')
    entries = types.ModuleType('homeassistant.config_entries')

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()
        async def async_set_unique_id(self, value):
            self.unique_id = value
        def _abort_if_unique_id_configured(self):
            pass
        def async_show_form(self, **kwargs):
            return {'type': 'form', **kwargs}
        def async_create_entry(self, **kwargs):
            return {'type': 'create_entry', **kwargs}
        def async_abort(self, **kwargs):
            return {'type': 'abort', **kwargs}
        def _get_reauth_entry(self):
            return self.entry
        def _abort_if_unique_id_mismatch(self, *, reason='wrong_account'):
            if self.unique_id != self.entry.unique_id:
                raise FlowAbort(reason)
        def async_update_reload_and_abort(self, entry, *, data_updates):
            entry.data = {**entry.data, **data_updates}
            return self.async_abort(reason='reauth_successful')
    entries.ConfigFlow = ConfigFlow
    ha.config_entries = entries
    core = types.ModuleType('homeassistant.core')
    core.HomeAssistant = object
    client = types.ModuleType('homeassistant.helpers.aiohttp_client')
    client.async_get_clientsession = lambda hass: None
    vol = types.ModuleType('voluptuous')
    vol.Schema = lambda x: x
    vol.Required = lambda key, **kwargs: key
    vol.In = lambda x: x
    aiohttp = types.ModuleType('aiohttp')
    aiohttp.ClientError = type('ClientError', (Exception,), {})
    api = types.ModuleType(pkg.__name__ + '.api')
    api.validate_login_response = lambda x: None
    phone = types.ModuleType(pkg.__name__ + '.phone_auth')
    phone.PhoneAuthError = type('PhoneAuthError', (Exception,), {})
    phone.InvalidOtp = type('InvalidOtp', (phone.PhoneAuthError,), {})
    phone.request_phone_otp = AsyncMock(return_value='session')
    phone.verify_phone_otp = AsyncMock()
    modules = {pkg.__name__: pkg, 'homeassistant': ha, 'homeassistant.config_entries': entries,
               'homeassistant.core': core, 'homeassistant.helpers': types.ModuleType('homeassistant.helpers'),
               'homeassistant.helpers.aiohttp_client': client, 'voluptuous': vol, 'aiohttp': aiohttp,
               api.__name__: api, phone.__name__: phone}
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(pkg.__name__ + '.config_flow', root / 'config_flow.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, phone


flowmod, phone = load_flow()


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        phone.request_phone_otp.reset_mock(side_effect=True, return_value=True)
        phone.request_phone_otp.return_value = 'session'
        phone.verify_phone_otp.reset_mock(side_effect=True, return_value=True)
        self.flow = flowmod.QuboConfigFlow()
        self.flow.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
            async_update_entry=unittest.mock.Mock(), async_reload=AsyncMock()))

    async def start(self):
        result = await self.flow.async_step_user({'auth_mode': 'phone'})
        self.assertEqual(result['step_id'], 'phone')
        result = await self.flow.async_step_phone({'phone': '5550000', 'country_code': '+91'})
        self.assertEqual(result['step_id'], 'otp')

    async def test_single_profile_and_no_code_saved(self):
        await self.start()
        phone.verify_phone_otp.return_value = [{'uuid': 'profile-1234', 'accessToken': 'token', 'expires_in': 3600}]
        result = await self.flow.async_step_otp({'code': '123456'})
        self.assertEqual(result['type'], 'create_entry')
        self.assertEqual(result['data']['user_uuid'], 'profile-1234')
        self.assertNotIn('123456', str(result))
        self.assertEqual(self.flow.unique_id, 'profile-1234')

    async def test_multi_profile_selection(self):
        await self.start()
        phone.verify_phone_otp.return_value = [{'uuid': 'uuid-1111', 'accessToken': 'a', 'expires_in': 3600}, {'uuid': 'uuid-2222', 'accessToken': 'b', 'expires_in': 3600}]
        result = await self.flow.async_step_otp({'code': '123456'})
        self.assertEqual(result['step_id'], 'profile')
        result = await self.flow.async_step_profile({'profile': 'uuid-2222'})
        self.assertEqual(result['data']['access_token'], 'b')

    async def test_invalid_code_requires_new_session(self):
        await self.start()
        phone.verify_phone_otp.side_effect = phone.InvalidOtp('secret')
        result = await self.flow.async_step_otp({'code': '123456'})
        self.assertEqual(result['errors']['base'], 'invalid_otp')
        self.assertEqual(result['step_id'], 'phone')
        self.assertIsNone(self.flow._session_uuid)

    async def test_reauth_updates_existing_and_reloads(self):
        self.flow.entry = types.SimpleNamespace(entry_id='entry', unique_id='uuid-1234', data={'auth_mode':'phone', 'user_uuid':'uuid-1234', 'client_id':'existing', 'phone':'555', 'country_code':'+91', 'refresh_token':'old', 'other':'keep'})
        await self.flow.async_step_reauth(self.flow.entry.data)
        await self.flow.async_step_phone({'phone':'555', 'country_code':'+91'})
        phone.verify_phone_otp.return_value = [{'uuid':'uuid-1234', 'accessToken':'new', 'expires_in': 3600}]
        with patch.object(self.flow, 'async_update_reload_and_abort', wraps=self.flow.async_update_reload_and_abort) as helper:
            result = await self.flow.async_step_otp({'code':'123456'})
        self.assertEqual(result['reason'], 'reauth_successful')
        helper.assert_called_once()
        self.assertIs(helper.call_args.args[0], self.flow.entry)
        self.assertEqual(self.flow.entry.data['client_id'], 'existing')
        self.assertEqual(self.flow.entry.data['other'], 'keep')
        self.assertIsNone(self.flow.entry.data['refresh_token'])
        self.flow.hass.config_entries.async_reload.assert_not_awaited()
        self.flow.hass.config_entries.async_update_entry.assert_not_called()

    async def test_reauth_wrong_account_rejected(self):
        self.flow.entry = types.SimpleNamespace(entry_id='entry', unique_id='uuid-1234', data={'auth_mode':'phone', 'user_uuid':'uuid-1234', 'client_id':'existing'})
        await self.flow.async_step_reauth(self.flow.entry.data)
        await self.flow.async_step_phone({'phone':'555', 'country_code':'+91'})
        phone.verify_phone_otp.return_value = [{'uuid':'uuid-other', 'accessToken':'secret', 'expires_in':3600}]
        with patch.object(self.flow, 'async_update_reload_and_abort') as helper:
            result = await self.flow.async_step_otp({'code':'123456'})
        self.assertEqual(result['reason'], 'wrong_account')
        self.assertNotIn('secret', str(result))
        helper.assert_not_called()

    async def test_reauth_unique_id_mismatch_rejected(self):
        self.flow.entry = types.SimpleNamespace(entry_id='entry', unique_id='different', data={'auth_mode':'phone', 'user_uuid':'uuid-1234', 'client_id':'existing'})
        await self.flow.async_step_reauth(self.flow.entry.data)
        await self.flow.async_step_phone({'phone':'555', 'country_code':'+91'})
        phone.verify_phone_otp.return_value = [{'uuid':'uuid-1234', 'accessToken':'secret', 'expires_in':3600}]
        with patch.object(self.flow, 'async_update_reload_and_abort') as helper:
            with self.assertRaisesRegex(FlowAbort, 'wrong_account'):
                await self.flow.async_step_otp({'code':'123456'})
        helper.assert_not_called()

    async def test_delayed_selection_uses_original_expiry(self):
        await self.start()
        phone.verify_phone_otp.return_value = [{'uuid':'uuid-1111','accessToken':'a', 'expires_in':120}, {'uuid':'uuid-2222','accessToken':'b', 'expires_in':120}]
        with patch.object(flowmod.time, 'time', return_value=1000):
            await self.flow.async_step_otp({'code':'123456'})
        with patch.object(flowmod.time, 'time', return_value=1061):
            result = await self.flow.async_step_profile({'profile':'uuid-1111'})
        self.assertEqual(result['step_id'], 'phone')

    async def test_password_path(self):
        with patch.object(flowmod, 'validate_input', AsyncMock(return_value={'title':'mail', 'client_id':'id'})):
            self.assertEqual((await self.flow.async_step_user({'auth_mode':'password'}))['step_id'], 'password')
            result = await self.flow.async_step_password({'username':'mail','password':'pw'})
        self.assertEqual(result['data'], {'username':'mail', 'password':'pw', 'client_id':'id'})

    async def test_expired_profile_not_accepted(self):
        await self.start()
        phone.verify_phone_otp.return_value = [{'uuid':'uuid-1111','accessToken':'a'}, {'uuid':'uuid-2222','accessToken':'b'}]
        await self.flow.async_step_otp({'code':'123456'})
        self.flow._profiles_started -= 301
        self.assertEqual((await self.flow.async_step_profile({'profile':'uuid-1111'}))['step_id'], 'phone')


if __name__ == '__main__':
    unittest.main()
