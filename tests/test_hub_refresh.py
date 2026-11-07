"""Offline regression tests; run directly or with unittest (no HA/conftest).

    python -m unittest discover -s tests -p 'test_hub_refresh.py' -v

Only aiohttp and paho-mqtt are required. No requests or MQTT connections occur.
"""

import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import aiohttp


def load_hub():
    """Load only hub/const, without executing the integration's __init__."""
    root = Path(__file__).resolve().parents[1] / "custom_components" / "qubo"
    package = types.ModuleType("_qubo_refresh_tests")
    package.__path__ = [str(root)]
    core = types.ModuleType("homeassistant.core")
    setattr(core, "HomeAssistant", object)
    event = types.ModuleType("homeassistant.helpers.event")
    setattr(event, "async_track_time_interval", Mock(side_effect=lambda *args: Mock()))
    modules = {
        package.__name__: package,
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.core": core,
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.event": event,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            package.__name__ + ".hub", root / "hub.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


hub_module = load_hub()


class FakeResponse:
    def __init__(self, session):
        self.session = session

    @property
    def status(self):
        return self.session.status

    async def __aenter__(self):
        self.session.entered.set()
        if self.session.gate is not None:
            await self.session.gate.wait()
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.session.status_error:
            raise self.session.status_error

    async def json(self):
        await asyncio.sleep(0)  # Make the concurrency regression deterministic.
        if self.session.json_error:
            raise self.session.json_error
        return self.session.data


class FakeSession:
    def __init__(self, data):
        self.data = data
        self.calls = []
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.status_error: Exception | None = None
        self.status = 200
        self.json_error: Exception | None = None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self)


class FakeHass:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.executor_threads = []

    async def async_add_executor_job(self, job):
        def run():
            self.executor_threads.append(threading.get_ident())
            return job()
        return await asyncio.to_thread(run)


class FakeClient:
    def __init__(self, *args):
        self.calls = []
        self.published = []
        self.connect_error: Exception | None = None
        self.stop_entered = threading.Event()
        self.stop_gate: threading.Event | None = None
        self.tls_configured = False

    def record(self, name, *args):
        self.calls.append((name, args, threading.get_ident()))

    def username_pw_set(self, **kwargs):
        self.record("credentials", kwargs)

    def tls_set_context(self, context):
        if self.tls_configured:
            raise ValueError("SSL/TLS has already been configured")
        self.tls_configured = True
        self.record("tls")

    def disconnect(self):
        self.record("disconnect")

    def loop_stop(self):
        self.record("loop_stop")
        self.stop_entered.set()
        if self.stop_gate is not None and not self.stop_gate.wait(5):
            raise RuntimeError("test stop gate timed out")

    def connect(self, *args):
        self.record("connect", *args)
        if self.connect_error:
            raise self.connect_error

    def loop_start(self):
        self.record("loop_start")

    def publish(self, *args):
        self.published.append(args)


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = FakeHass()
        self.session = FakeSession({"accessToken": "new-access"})
        with patch.object(hub_module.mqtt, "Client", FakeClient):
            self.hub = hub_module.QuboHub(
                self.hass, self.session, "old-access", "old-refresh", "user",
                "device", "unit", 0, False, "device-name", "handle", "client", True
            )
        self.client = self.hub._mqtt_client
        self.client.calls.clear()
        self.timer_patch = patch.object(
            hub_module, "async_track_time_interval",
            Mock(side_effect=lambda *args: Mock())
        )
        self.timer = self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)

    def credentials(self):
        return (self.hub._access_token, self.hub._refresh_token, self.hub._expires_at)

    def count(self, name):
        return sum(call[0] == name for call in self.client.calls)

    async def assert_invalid(self, data):
        self.session.data = data
        before = self.credentials()
        with self.assertRaises(hub_module.QuboHubError):
            await self.hub.turn_on()
        self.assertEqual(self.credentials(), before)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.published, [])

    async def test_concurrent_commands_refresh_and_restart_once(self):
        await asyncio.gather(*[
            command() for _ in range(8) for command in
            (self.hub.turn_on, self.hub.turn_off, self.hub._send_meter_refresh)
        ])
        self.assertEqual(len(self.session.calls), 1)
        for name in ("disconnect", "loop_stop", "credentials", "connect", "loop_start"):
            self.assertEqual(self.count(name), 1, name)
        self.assertEqual(len(self.client.published), 24)
        self.assertEqual(self.session.calls[0][1]["json"], {
            "accessToken": "old-access", "refreshToken": "old-refresh"
        })
        self.assertTrue(all(t != threading.get_ident() for t in self.hass.executor_threads))

    async def test_valid_token_does_not_refresh_or_restart(self):
        self.hub._expires_at = float("inf")
        await self.hub.turn_on()
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(self.client.published), 1)

    async def test_missing_and_invalid_access_tokens(self):
        for data in ({}, {"access_token": "wrong-key"}, *[
            {"accessToken": value} for value in (None, "", " \t", 3, True, [], {})
        ]):
            with self.subTest(data=data):
                await self.assert_invalid(data)

    async def test_non_object_responses(self):
        for data in (None, [], "token", 3, True):
            with self.subTest(data=data):
                await self.assert_invalid(data)

    async def test_invalid_rotated_refresh_token_preserves_all_credentials(self):
        for value in (None, "", " \t", 3, True, [], {}):
            with self.subTest(value=value):
                await self.assert_invalid({
                    "accessToken": "new", "refreshToken": value, "expires_in": 3600
                })

    async def test_invalid_expiry_preserves_all_credentials(self):
        for value in (True, False, None, "3600", float("nan"), float("inf"),
                      -float("inf"), -1, 0, 1, 59, 60, 604801, 10**1000, [], {}):
            with self.subTest(value=value):
                await self.assert_invalid({
                    "accessToken": "new", "refreshToken": "rotated", "expires_in": value
                })

    async def test_omitted_refresh_and_expiry_keep_default(self):
        with patch.object(hub_module.time, "time", return_value=1000):
            await self.hub._async_refresh_token_if_needed()
        self.assertEqual(self.credentials(), ("new-access", "old-refresh", 4540))

    async def test_rotated_refresh_token_and_valid_expiry_bounds(self):
        for lifetime in (60.5, 61, 3600.5, 604800):
            with self.subTest(lifetime=lifetime):
                self.hub._expires_at = 0
                self.session.data = {
                    "accessToken": "new-access", "refreshToken": "rotated",
                    "expires_in": lifetime,
                }
                with patch.object(hub_module.time, "time", return_value=1000):
                    await self.hub._async_refresh_token_if_needed()
                self.assertEqual(self.credentials(), (
                    "new-access", "rotated", 1000 + lifetime - 60
                ))

    async def test_refresh_failures_never_publish_any_command(self):
        for error in (aiohttp.ClientError("network"), asyncio.TimeoutError(),
                      ValueError("bad json"), TypeError("bad content")):
            for command in (self.hub.turn_on, self.hub.turn_off, self.hub._send_meter_refresh):
                with self.subTest(error=type(error).__name__, command=command.__name__):
                    self.session.json_error = error
                    before = self.credentials()
                    with self.assertRaises(hub_module.QuboHubError):
                        await command()
                    self.assertEqual(self.credentials(), before)
                    self.assertEqual(self.client.published, [])
                    self.assertEqual(self.client.calls, [])

    async def test_http_status_failure_never_publishes(self):
        self.session.status_error = aiohttp.ClientResponseError(
            Mock(real_url="https://invalid.example"), (), status=401
        )
        with self.assertRaises(hub_module.QuboHubError):
            await self.hub.turn_on()
        self.assertEqual(self.credentials(), ("old-access", "old-refresh", 0))
        self.assertEqual(self.client.published, [])

    async def test_redirects_and_unexpected_statuses_fail_closed(self):
        for status in (201, 204, 301, 302, 303, 307, 308, 401, 403, 500):
            with self.subTest(status=status):
                self.session.status = status
                with self.assertRaises(hub_module.QuboHubError):
                    await self.hub.turn_on()
                self.assertEqual(self.credentials(), ("old-access", "old-refresh", 0))
                self.assertEqual(self.client.published, [])
        for _, kwargs in self.session.calls:
            self.assertIs(kwargs["allow_redirects"], False)
            self.assertEqual(kwargs["timeout"].total, 30)

    async def test_refresh_error_traceback_does_not_disclose_transport_message(self):
        import traceback
        self.session.json_error = aiohttp.ClientError("SENSITIVE-TRANSPORT-MARKER")
        try:
            await self.hub.turn_on()
        except hub_module.QuboHubError:
            rendered = traceback.format_exc()
        else:
            self.fail("Expected fail-closed refresh")
        self.assertNotIn("SENSITIVE-TRANSPORT-MARKER", rendered)

    async def test_restart_failure_blocks_until_successful_retry(self):
        self.session.data["refreshToken"] = "rotated"
        self.client.connect_error = OSError("offline")
        for command in (self.hub.turn_on, self.hub.turn_off, self.hub._send_meter_refresh):
            with self.assertRaises(hub_module.QuboHubError):
                await command()
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.client.published, [])
        self.assertTrue(self.hub._mqtt_needs_restart)
        self.client.connect_error = None
        await self.hub.turn_on()
        self.assertFalse(self.hub._mqtt_needs_restart)
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(len(self.client.published), 1)
        self.assertEqual(self.hub._refresh_token, "rotated")

    async def test_stop_waits_for_inflight_refresh_then_prevents_restart(self):
        self.session.gate = asyncio.Event()
        refresh = asyncio.create_task(self.hub._async_refresh_token_if_needed())
        await asyncio.wait_for(self.session.entered.wait(), 2)
        stop = asyncio.create_task(self.hub.stop())
        await asyncio.sleep(0)
        self.assertFalse(stop.done())
        self.assertEqual(self.client.calls, [])
        self.session.gate.set()
        await asyncio.wait_for(asyncio.gather(refresh, stop), 2)
        self.assertEqual([c[0] for c in self.client.calls], [
            "disconnect", "loop_stop", "credentials", "connect", "loop_start",
            "disconnect", "loop_stop"
        ])
        for command in (self.hub.turn_on, self.hub.turn_off, self.hub._send_meter_refresh):
            with self.assertRaises(hub_module.QuboHubError):
                await command()
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.client.published, [])

    async def test_stop_runs_in_executor_and_cancels_timer_once(self):
        unsubscribe = Mock()
        self.hub._unsub_refresh = unsubscribe
        self.client.stop_gate = threading.Event()
        stop = asyncio.create_task(self.hub.stop())
        try:
            entered = await asyncio.to_thread(self.client.stop_entered.wait, 2)
            self.assertTrue(entered)
            self.assertFalse(stop.done())
            # The event loop is still responsive while loop_stop blocks.
            await asyncio.wait_for(asyncio.sleep(0.01), 0.5)
        finally:
            self.client.stop_gate.set()
            await stop
        await self.hub.stop()
        unsubscribe.assert_called_once_with()
        self.assertIsNone(self.hub._unsub_refresh)
        self.assertTrue(all(c[2] != threading.get_ident() for c in self.client.calls))

    async def test_start_after_stop_reuses_tls_and_reinstalls_timer(self):
        self.hub._expires_at = float("inf")
        await self.hub.start()
        unsubscribe = self.hub._unsub_refresh
        await self.hub.stop()
        await self.hub.start()
        self.assertFalse(self.hub._stopped)
        self.assertEqual(self.count("tls"), 1)
        self.assertEqual(self.count("connect"), 2)
        self.assertEqual(self.timer.call_count, 2)
        unsubscribe.assert_called_once_with()
        self.assertEqual(len(self.client.published), 2)

    async def test_stopped_hub_does_not_refresh_even_when_expired(self):
        await self.hub.stop()
        for command in (self.hub.turn_on, self.hub.turn_off, self.hub._send_meter_refresh):
            with self.assertRaises(hub_module.QuboHubError):
                await command()
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.client.published, [])
        self.assertEqual(self.credentials(), ("old-access", "old-refresh", 0))

    async def test_start_waits_for_blocking_stop(self):
        self.hub._expires_at = float("inf")
        self.client.stop_gate = threading.Event()
        stop = asyncio.create_task(self.hub.stop())
        start = None
        try:
            self.assertTrue(await asyncio.to_thread(self.client.stop_entered.wait, 2))
            start = asyncio.create_task(self.hub.start())
            await asyncio.sleep(0.01)
            self.assertFalse(start.done())
            self.assertEqual(self.count("connect"), 0)
        finally:
            self.client.stop_gate.set()
            await stop
            if start is not None:
                await start
        self.assertFalse(self.hub._stopped)
        self.assertEqual(self.count("connect"), 1)
        self.assertEqual(len(self.client.published), 1)

    async def test_cancelled_restart_retains_lock_until_executor_finishes(self):
        self.client.stop_gate = threading.Event()
        refresh = asyncio.create_task(self.hub._async_refresh_token_if_needed())
        stop = None
        try:
            self.assertTrue(await asyncio.to_thread(self.client.stop_entered.wait, 2))
            refresh.cancel()
            stop = asyncio.create_task(self.hub.stop())
            await asyncio.sleep(0.01)
            refresh.cancel()  # A second cancellation cannot abandon the worker.
            await asyncio.sleep(0.01)
            self.assertFalse(refresh.done())
            self.assertFalse(stop.done())
            self.assertEqual(self.count("disconnect"), 1)
        finally:
            self.client.stop_gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await refresh
            if stop is not None:
                await stop
        self.assertEqual([c[0] for c in self.client.calls][-2:], ["disconnect", "loop_stop"])
        self.assertEqual(self.client.published, [])


if __name__ == "__main__":
    unittest.main()
