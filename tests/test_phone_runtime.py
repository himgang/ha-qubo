"""Synthetic, offline phone-session setup and lifecycle tests."""
import asyncio
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_http_security import PACKAGE
from test_hub_refresh import FakeClient, FakeHass, FakeSession, hub_module


class AuthFailed(Exception):
    pass


class Response:
    def __init__(self, status=200):
        self.status = status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def raise_for_status(self):
        pass
    async def json(self):
        return {"devices": [{"deviceName": "Smart Plug 10A", "deviceUUID": "device", "unitUUID": "unit", "handleName": "handle"}]}


class PhoneRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.exceptions = types.ModuleType("homeassistant.exceptions")
        self.exceptions.ConfigEntryAuthFailed = AuthFailed
        self.module_patch = patch.dict(sys.modules, {"homeassistant.exceptions": self.exceptions})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.entry = types.SimpleNamespace(data={"auth_mode": "phone", "client_id": "client", "access_token": "secret", "user_uuid": "user", "expires_at": time.time() + 3600}, entry_id="entry", async_start_reauth=Mock())

    async def test_setup_uses_session_and_skips_login(self):
        session = Mock()
        session.post.return_value = Response()
        hass = types.SimpleNamespace(session=session, data={}, config_entries=types.SimpleNamespace(async_forward_entry_setups=AsyncMock()))
        hub = types.SimpleNamespace(start=AsyncMock())
        with patch.object(PACKAGE, "QuboHub", return_value=hub) as ctor:
            self.assertTrue(await PACKAGE.async_setup_entry(hass, self.entry))
        session.post.assert_called_once()
        args, kwargs = session.post.call_args
        self.assertTrue(args[0].endswith("/units/sync"))
        self.assertEqual(kwargs["headers"]["Subscriber-Key"], "secret")
        self.assertEqual(kwargs["headers"]["User-UUID"], "user")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(ctor.call_args.kwargs["entry"], self.entry)
        hub.start.assert_awaited_once()

    async def test_expired_or_rejected_setup_raises_auth_failed(self):
        session = Mock()
        hass = types.SimpleNamespace(session=session)
        self.entry.data["expires_at"] = 0
        with self.assertRaises(AuthFailed):
            await PACKAGE.async_setup_entry(hass, self.entry)
        session.post.assert_not_called()
        self.entry.data["expires_at"] = time.time() + 60
        for status in (401, 403):
            session.post.return_value = Response(status)
            with self.assertRaises(AuthFailed):
                await PACKAGE.async_setup_entry(hass, self.entry)

    async def test_expired_saved_phone_session_refreshes_before_sync(self):
        self.entry.data.update(expires_at=0, refresh_token="old-refresh")
        session = Mock()
        session.post.return_value = Response()
        hass = types.SimpleNamespace(session=session, data={}, config_entries=types.SimpleNamespace(
            async_update_entry=Mock(), async_forward_entry_setups=AsyncMock()))
        hub = types.SimpleNamespace(start=AsyncMock())
        new_expiry = time.time() + 800
        with patch.object(PACKAGE, "refresh_credentials", AsyncMock(return_value=("renewed", "rotated", new_expiry))) as refresh, \
             patch.object(PACKAGE, "QuboHub", return_value=hub):
            self.assertTrue(await PACKAGE.async_setup_entry(hass, self.entry))
        refresh.assert_awaited_once()
        self.assertEqual(session.post.call_args.kwargs["headers"]["Subscriber-Key"], "renewed")
        saved = hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertEqual(saved["refresh_token"], "rotated")

    async def test_rejected_saved_phone_refresh_requires_reauth(self):
        self.entry.data.update(expires_at=0, refresh_token="old-refresh")
        hass = types.SimpleNamespace(session=Mock())
        with patch.object(PACKAGE, "refresh_credentials", AsyncMock(side_effect=PACKAGE.SessionRejected)):
            with self.assertRaises(AuthFailed):
                await PACKAGE.async_setup_entry(hass, self.entry)

    async def test_expiry_concurrent_commands_disconnect_and_reauth_once(self):
        hass = FakeHass()
        session = FakeSession({})
        with patch.object(hub_module.mqtt, "Client", FakeClient):
            hub = hub_module.QuboHub(hass, session, "secret", None, "user", "device", "unit", time.time() + 300, False, "Plug", "handle", "client", True, entry=self.entry, phone_auth=True)
        with patch.object(hub_module, "async_track_time_interval", return_value=Mock()) as timer:
            await hub.start()
        client = hub._mqtt_client
        client.calls.clear()
        client.published.clear()
        callback = Mock()
        hub.register_callback(callback)
        hub._expires_at = 0
        results = await asyncio.gather(hub.turn_on(), hub.turn_off(), hub._periodic_meter_refresh(), return_exceptions=True)
        self.assertIsInstance(results[0], hub_module.QuboHubError)
        self.assertIsInstance(results[1], hub_module.QuboHubError)
        self.assertIsNone(results[2])
        self.entry.async_start_reauth.assert_called_once_with(hass)
        self.assertFalse(hub.available)
        callback.assert_called_once()
        timer.return_value.assert_not_called()
        self.assertEqual([c[0] for c in client.calls], ["disconnect", "loop_stop"])
        self.assertFalse(client.published)
        self.assertFalse(session.calls)
        hub._apply_mqtt_update(True, {}, True)
        self.assertFalse(hub.available)
        await hub.stop()

    async def test_phone_refresh_is_shared_and_persisted(self):
        hass = FakeHass()
        hass.config_entries = types.SimpleNamespace(async_update_entry=Mock())
        session = FakeSession({"accessToken": "new", "refreshToken": "rotated", "expires_in": 900})
        auth = hub_module.QuboSession("secret", "old", 0)
        with patch.object(hub_module.mqtt, "Client", FakeClient):
            hubs = [hub_module.QuboHub(hass, session, "secret", "old", "user", device,
                    "unit", 0, False, "Plug", "handle", "client", True,
                    entry=self.entry, phone_auth=True, auth=auth) for device in ("one", "two")]
        await asyncio.gather(*(hub.turn_on() for hub in hubs))
        self.assertEqual(len(session.calls), 1)
        hass.config_entries.async_update_entry.assert_called_once()
        saved = hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("new", "rotated"))
        self.assertTrue(all(hub._access_token == "new" for hub in hubs))

    async def test_reauth_retry_throttled_after_dismissal(self):
        hass = FakeHass()
        with patch.object(hub_module.mqtt, "Client", FakeClient):
            hub = hub_module.QuboHub(hass, FakeSession({}), "secret", None, "user", "device", "unit", 0, False, "Plug", "handle", "client", True, entry=self.entry, phone_auth=True)
        with patch.object(hub_module, "time", wraps=time) as fake_time:
            clock = fake_time.monotonic
            clock.return_value = 100
            await asyncio.gather(*(hub._periodic_meter_refresh() for _ in range(5)))
            self.assertEqual(self.entry.async_start_reauth.call_count, 1)
            clock.return_value = 159
            await hub._periodic_meter_refresh()
            self.assertEqual(self.entry.async_start_reauth.call_count, 1)
            clock.return_value = 160
            await hub._periodic_meter_refresh()
            self.assertEqual(self.entry.async_start_reauth.call_count, 2)
        self.assertFalse(hub._mqtt_client.published)
        self.assertEqual([c[0] for c in hub._mqtt_client.calls].count("disconnect"), 1)
        await hub.stop()

    async def test_mqtt_rejections_threadsafe_and_transient_codes(self):
        hass = FakeHass()
        tasks = []
        def create_task(coro):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task
        hass.async_create_task = create_task
        with patch.object(hub_module.mqtt, "Client", FakeClient):
            hub = hub_module.QuboHub(hass, FakeSession({}), "secret", None, "user", "device", "unit", time.time() + 300, False, "Plug", "handle", "client", True, entry=self.entry, phone_auth=True)
        client = hub._mqtt_client
        for rc in (1, 2, 3):
            await asyncio.to_thread(hub._on_connect, client, None, None, rc)
        self.entry.async_start_reauth.assert_not_called()
        self.assertFalse(hub._mqtt_auth_rejected)
        await asyncio.gather(*(asyncio.to_thread(hub._on_connect, client, None, None, rc) for rc in (4, 5, 4)))
        await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        self.entry.async_start_reauth.assert_called_once_with(hass)
        self.assertFalse(hub.available)
        self.assertEqual([c[0] for c in client.calls].count("disconnect"), 1)
        with self.assertRaises(hub_module.QuboHubError):
            await hub.turn_on()
        await hub._periodic_meter_refresh()
        self.assertFalse(client.published)
        await hub.stop()
        hub._on_connect(client, None, None, 5)
        await asyncio.sleep(0)
        self.assertEqual(len(tasks), 3)
