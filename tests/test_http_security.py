"""Offline HTTP security regression tests; never contact Qubo or Home Assistant."""

import asyncio
import importlib.util
import json
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "qubo"


def load_integration():
    """Import real integration source with only HA's framework stubbed."""
    names = ["homeassistant", "homeassistant.core", "homeassistant.config_entries",
             "homeassistant.helpers", "homeassistant.helpers.aiohttp_client",
             "homeassistant.helpers.event"]
    modules = {name: types.ModuleType(name) for name in names}
    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass
    setattr(modules["homeassistant.config_entries"], "ConfigFlow", ConfigFlow)
    setattr(modules["homeassistant.config_entries"], "ConfigEntry", object)
    setattr(modules["homeassistant"], "config_entries", modules["homeassistant.config_entries"])
    setattr(modules["homeassistant.core"], "HomeAssistant", object)
    setattr(modules["homeassistant.helpers.aiohttp_client"], "async_get_clientsession", lambda hass: hass.session)
    setattr(modules["homeassistant.helpers.event"], "async_track_time_interval", Mock())
    spec = importlib.util.spec_from_file_location(
        "http_security_qubo", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, spec.name: package}):
        spec.loader.exec_module(package)
        flow = importlib.import_module("http_security_qubo.config_flow")
        hub = importlib.import_module("http_security_qubo.hub")
    return package, flow, hub


PACKAGE, FLOW, HUB = load_integration()
AUTH = {"accessToken": "SENSITIVE-ACCESS", "refreshToken": "SENSITIVE-REFRESH", "uuid": "user-123"}
INPUT = {"username": "SENSITIVE-USER", "password": "SENSITIVE-PASSWORD"}


class RoutingSession:
    """Route fixed production URLs to our local server, preserving request flags."""
    def __init__(self, session, base):
        self.session, self.base, self.calls = session, base, []

    def post(self, url, **kwargs):
        path = "refresh" if url.endswith("/auth/refresh") else "sync" if url.endswith("/sync") else "login"
        self.calls.append((path, kwargs))
        return self.session.post(f"{self.base}/{path}", **kwargs)


class HttpSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.sink_requests = []
        self.replies = {"login": (200, AUTH), "sync": (200, {"devices": []}), "refresh": (200, AUTH)}
        async def sink(request):
            self.sink_requests.append((dict(request.headers), await request.read()))
            return web.json_response(AUTH)
        sink_app = web.Application()
        sink_app.router.add_route("*", "/sink", sink)
        self.sink_runner = web.AppRunner(sink_app)
        await self.sink_runner.setup()
        sink_site = web.TCPSite(self.sink_runner, "127.0.0.1", 0)
        await sink_site.start()
        self.sink_url = f"http://127.0.0.1:{self.sink_runner.addresses[0][1]}/sink"
        async def origin(request):
            self.requests.append((request.path, dict(request.headers), await request.read()))
            status, body = self.replies[request.path[1:]]
            if 300 <= status < 400:
                return web.Response(status=status, headers={"Location": self.sink_url})
            if isinstance(body, str):
                return web.Response(status=status, text=body, content_type="application/json")
            return web.json_response(body, status=status)
        app = web.Application()
        app.router.add_post("/{kind}", origin)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        self.session = aiohttp.ClientSession()
        self.routing = RoutingSession(self.session, base)
        self.hass = types.SimpleNamespace(session=self.routing, data={},
                                         async_add_executor_job=AsyncMock())
        self.entry = types.SimpleNamespace(data={**INPUT, "client_id": "client"}, entry_id="entry")

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        await self.sink_runner.cleanup()

    def hub(self):
        return HUB.QuboHub(self.hass, self.routing, "old", "old-refresh", "user", "device",
                           "unit", 0, False, "Plug", "handle", "client", False)

    async def expect_refresh_failure(self, hub):
        # The separate lifecycle patch deliberately changes failures from a
        # logged return to a controlled exception. Test both scoped branches.
        if hasattr(HUB, "QuboHubError"):
            with self.assertRaises(HUB.QuboHubError):
                await hub._async_refresh_token_if_needed()
        else:
            await hub._async_refresh_token_if_needed()

    async def test_config_redirects_never_reach_second_origin(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.replies["login"] = (status, {})
                with self.assertRaises(FLOW.CannotConnect):
                    await FLOW.validate_input(self.hass, INPUT)
                self.assertEqual(self.sink_requests, [])

    async def test_setup_login_redirects_never_reach_second_origin(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.replies["login"] = (status, {})
                self.assertFalse(await PACKAGE.async_setup_entry(self.hass, self.entry))
                self.assertEqual(self.sink_requests, [])

    async def test_sync_redirects_do_not_forward_custom_token_headers(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.replies["sync"] = (status, {})
                self.assertFalse(await PACKAGE.async_setup_entry(self.hass, self.entry))
                self.assertEqual(self.sink_requests, [])
        self.assertEqual(self.requests[-1][1]["Subscriber-Key"], AUTH["accessToken"])

    async def test_refresh_redirects_never_forward_tokens(self):
        hub = self.hub()
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.replies["refresh"] = (status, {})
                await self.expect_refresh_failure(hub)
                self.assertEqual(hub._access_token, "old")
                self.assertEqual(self.sink_requests, [])
        self.hass.async_add_executor_job.assert_not_awaited()

    async def test_valid_config_response(self):
        result = await FLOW.validate_input(self.hass, INPUT)
        self.assertEqual(result["title"], INPUT["username"])
        self.assertEqual(len(result["client_id"]), 16)

    async def test_valid_setup_uses_validated_credentials(self):
        self.replies["sync"] = (200, {"devices": [{
            "deviceName": "Smart Plug 10A", "deviceUUID": "device-1",
            "unitUUID": "unit-1", "handleName": "plug"}]})
        fake_hub = types.SimpleNamespace(start=AsyncMock())
        self.hass.config_entries = types.SimpleNamespace(async_forward_entry_setups=AsyncMock())
        with patch.object(PACKAGE, "QuboHub", return_value=fake_hub) as hub_class:
            self.assertTrue(await PACKAGE.async_setup_entry(self.hass, self.entry))
        self.assertEqual(hub_class.call_args.args[2:5],
                         (AUTH["accessToken"], AUTH["refreshToken"], AUTH["uuid"]))
        fake_hub.start.assert_awaited_once()

    async def test_sync_rejects_wildcard_or_missing_topic_identifiers(self):
        for value in (None, "", "+", "#", "other/device", [], 123, ".", "..",
                      "%2e%2e", "user\tname", "user\nname", "user%2Fother"):
            for key in ("deviceUUID", "unitUUID"):
                with self.subTest(value=value, key=key):
                    device = {"deviceName": "Smart Plug 10A", "deviceUUID": "device",
                              "unitUUID": "unit", "handleName": "plug", key: value}
                    self.replies["sync"] = (200, {"devices": [device]})
                    with patch.object(PACKAGE, "QuboHub") as hub_class:
                        self.assertFalse(await PACKAGE.async_setup_entry(self.hass, self.entry))
                        hub_class.assert_not_called()

    async def test_invalid_response_schema_is_rejected(self):
        for body in ({}, [], None, {"error": "invalid credentials"},
                     {**AUTH, "accessToken": None}, {**AUTH, "refreshToken": ""},
                     *({**AUTH, "uuid": value} for value in
                       ("../other", ".", "..", "%2e%2e", "user\tname", "user%2Fother")),
                     {**AUTH, "expires_in": True},
                     {**AUTH, "expires_in": 0}, {**AUTH, "expires_in": float("inf")},
                     {**AUTH, "expires_in": 10**1000}, "not-json"):
            with self.subTest(body_type=type(body).__name__):
                self.replies["login"] = (200, body)
                with self.assertRaises(FLOW.CannotConnect):
                    await FLOW.validate_input(self.hass, INPUT)
                self.assertFalse(await PACKAGE.async_setup_entry(self.hass, self.entry))

    async def test_unexpected_status_is_rejected(self):
        for status in (201, 204, 400, 500):
            with self.subTest(status=status):
                self.replies["login"] = (status, {})
                with self.assertRaises(FLOW.CannotConnect):
                    await FLOW.validate_input(self.hass, INPUT)

    async def test_unauthorized_remains_invalid_auth(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.replies["login"] = (status, {})
                with self.assertRaises(FLOW.InvalidAuth):
                    await FLOW.validate_input(self.hass, INPUT)

    async def test_error_body_secrets_never_logged(self):
        marker = "SENSITIVE-SECRET-MARKER"
        self.replies["login"] = (500, (marker + "\n") * 20000)
        with self.assertLogs(level=logging.ERROR) as logs:
            with self.assertRaises(FLOW.CannotConnect):
                await FLOW.validate_input(self.hass, INPUT)
            self.assertFalse(await PACKAGE.async_setup_entry(self.hass, self.entry))
        output = "\n".join(logs.output)
        self.assertNotIn(marker, output)
        self.assertLess(len(output), 1000)

    async def test_transport_exception_message_is_not_logged(self):
        self.hass.session = Mock()
        self.hass.session.post.side_effect = aiohttp.ClientError("SENSITIVE-SECRET")
        with self.assertLogs(level=logging.ERROR) as logs:
            with self.assertRaises(FLOW.CannotConnect):
                await FLOW.validate_input(self.hass, INPUT)
            self.assertFalse(await PACKAGE.async_setup_entry(self.hass, self.entry))
        self.assertNotIn("SENSITIVE-SECRET", "\n".join(logs.output))

    async def test_all_four_calls_have_explicit_redirect_and_timeout_policy(self):
        await FLOW.validate_input(self.hass, INPUT)
        await PACKAGE.async_setup_entry(self.hass, self.entry)
        await self.hub()._async_refresh_token_if_needed()
        self.assertEqual([x[0] for x in self.routing.calls], ["login", "login", "sync", "refresh"])
        for _, kwargs in self.routing.calls:
            self.assertIs(kwargs["allow_redirects"], False)
            self.assertEqual(kwargs["timeout"].total, 30)

    async def test_refresh_timeout_does_not_log_exception_or_replace_token(self):
        hub = self.hub()
        hub.session = Mock()
        hub.session.post.side_effect = TimeoutError("SENSITIVE-TIMEOUT")
        with patch.object(HUB._LOGGER, "error") as errors:
            await self.expect_refresh_failure(hub)
        self.assertEqual(hub._access_token, "old")
        self.hass.async_add_executor_job.assert_not_awaited()
        self.assertNotIn("SENSITIVE-TIMEOUT", str(errors.call_args_list))

    async def test_default_aiohttp_behavior_reproduces_redirect_leak(self):
        # Control: prove this local test rig exposes the pre-patch behavior.
        self.replies["login"] = (307, {})
        async with self.routing.post("https://unused/user/login", json=INPUT) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.sink_requests), 1)
        self.assertIn(INPUT["password"].encode(), self.sink_requests[0][1])


if __name__ == "__main__":
    unittest.main()
