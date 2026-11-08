"""Phone API regression tests with fake HTTP responses; no actual OTP sent."""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components/qubo"
package = types.ModuleType("_phone_api_test")
package.__path__ = [str(ROOT)]
ha = types.ModuleType("homeassistant")
helpers = types.ModuleType("homeassistant.helpers")
client = types.ModuleType("homeassistant.helpers.aiohttp_client")
setattr(client, "async_get_clientsession", lambda hass: hass.session)
with patch.dict(sys.modules, {package.__name__: package, "homeassistant": ha,
    "homeassistant.helpers": helpers, "homeassistant.helpers.aiohttp_client": client}):
    spec = importlib.util.spec_from_file_location(package.__name__ + ".phone_auth", ROOT / "phone_auth.py")
    assert spec is not None and spec.loader is not None
    phone_auth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(phone_auth)


class Response:
    def __init__(self, data, status): self.data, self.status = data, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def json(self): return self.data


class Session:
    def __init__(self): self.calls, self.data, self.status = [], {}, 200
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.data, self.status)


class PhoneApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = Session()
        self.hass = types.SimpleNamespace(session=self.session)

    async def request(self):
        return await phone_auth.request_phone_otp(self.hass, "9876543210", "+91", "client-1")

    async def verify(self, code="123456"):
        return await phone_auth.verify_phone_otp(self.hass, "9876543210", "+91", "client-1", "challenge-1", code)

    async def test_request_wire_schema_and_safe_http_options(self):
        self.session.data = {"uuid": "challenge-1"}
        self.assertEqual(await self.request(), "challenge-1")
        url, options = self.session.calls[0]
        self.assertTrue(url.endswith("/user/phone/login"))
        self.assertEqual(options["json"], {"phone": "9876543210", "isdCode": "+91", "deviceAttribute": "HomeAssistant|Server|Integration"})
        self.assertEqual(options["headers"]["App-Id"], "com.hero.iot")
        self.assertEqual(options["headers"]["Source-Device-Id"], "client-1")
        self.assertIs(options["allow_redirects"], False)
        self.assertEqual(options["timeout"].total, 30)

    async def test_verify_wire_schema_and_alternate_fields(self):
        self.session.data = [{"userUUID": "user-1", "token": "test-token"},
                             {"userId": "user-2", "accessToken": "second", "expires_in": 600}]
        result = await self.verify()
        self.assertEqual(result, [{"uuid": "user-1", "accessToken": "test-token", "expires_in": 3600},
                                  {"uuid": "user-2", "accessToken": "second", "expires_in": 600}])
        url, options = self.session.calls[0]
        self.assertTrue(url.endswith("/user/phone/verify"))
        self.assertEqual(options["json"]["otp"], "123456")
        self.assertEqual(options["json"]["uuid"], "challenge-1")

    async def test_http_failure_does_not_leak_error_body(self):
        self.session.status, self.session.data = 401, {"otp": "123456", "token": "secret"}
        with self.assertRaises(phone_auth.InvalidOtp) as exc:
            await self.verify()
        self.assertNotIn("123456", str(exc.exception))
        self.assertNotIn("secret", str(exc.exception))

    async def test_reject_redirects(self):
        for status in (301, 302, 303, 307, 308):
            self.session.status = status
            with self.subTest(status=status), self.assertRaises(phone_auth.PhoneAuthError):
                await self.request()

    async def test_bad_inputs_prevent_any_request(self):
        for phone, country in (("a", "+91"), ("1234/567", "+91"), ("12345", "91")):
            with self.subTest(phone=phone, country=country), self.assertRaises(phone_auth.PhoneAuthError):
                await phone_auth.request_phone_otp(self.hass, phone, country, "client")
        with self.assertRaises(phone_auth.InvalidOtp): await self.verify("../../")
        self.assertEqual(self.session.calls, [])

    async def test_bad_challenges_profiles_lifetimes_and_duplicates(self):
        for result in (None, {}, {"uuid": "../other"}, {"uuid": ""}):
            self.session.data = result
            with self.subTest(challenge=result), self.assertRaises(phone_auth.PhoneAuthError): await self.request()
        for result in ([], None, "bad", [{"uuid": "user"}],
                       [{"uuid": "../x", "accessToken": "x"}],
                       [{"uuid": "user", "accessToken": "x", "expires_in": True}],
                       [{"uuid": "user", "accessToken": "x", "expires_in": float('nan')}],
                       [{"uuid": "user", "accessToken": "x"}]*2):
            self.session.data = result
            with self.subTest(profile=result), self.assertRaises(phone_auth.PhoneAuthError): await self.verify()

    async def test_jwt_expiry_can_shorten_but_never_extend(self):
        def token(exp):
            payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
            return f"head.{payload}.signature"
        with patch.object(phone_auth.time, "time", return_value=1000):
            for exp, expected in ((1300, 300), (20000, 3600)):
                self.session.data = {"uuid": "user", "accessToken": token(exp)}
                self.assertEqual((await self.verify())[0]["expires_in"], expected)
            self.session.data = {"uuid": "user", "accessToken": token(1050)}
            with self.assertRaises(phone_auth.PhoneAuthError): await self.verify()


if __name__ == "__main__": unittest.main()
