"""Offline MQTT security regressions; run with unittest, no HA install needed.

Loads the actual hub and constants under a synthetic package (never the
integration __init__). Input tests stub only Home Assistant imports; aiohttp
and paho-mqtt are real dependencies. The TLS-start regression substitutes a
fake MQTT client and refresh hook. No broker, credentials or network is used.
"""

import asyncio
from copy import deepcopy
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import ssl
import sys
import threading
import types
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt


SOURCE = Path(__file__).resolve().parents[1] / "custom_components" / "qubo"


def load_hub():
    package_name = "_qubo_mqtt_security_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(SOURCE)]
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    setattr(core, "HomeAssistant", object)
    helpers = types.ModuleType("homeassistant.helpers")
    event = types.ModuleType("homeassistant.helpers.event")
    setattr(event, "async_track_time_interval", lambda *args: None)
    modules = {
        package_name: package,
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.event": event,
    }
    with patch.dict(sys.modules, modules):
        for name in ("const", "hub"):
            fullname = f"{package_name}.{name}"
            spec = importlib.util.spec_from_file_location(fullname, SOURCE / f"{name}.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = module
            spec.loader.exec_module(module)
        return module


hub_module = load_hub()


class MQTTInputTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.hub = hub_module.QuboHub(
            types.SimpleNamespace(loop=self.loop), None, "test-access", "test-refresh",
            "test-user", "test-device", "test-unit", 0, False, "test-name",
            "test-handle", "test-client", False,
        )
        self.hub.metrics.update(power=7.0, current=0.5, voltage=230.0,
                                consumption=12.0, duration=30)
        self.notifications = []
        self.hub.register_callback(self.on_update)
        self.main_thread = threading.get_ident()

    def on_update(self):
        self.assertTrue(self.loop.is_running())
        self.assertEqual(threading.get_ident(), self.main_thread)
        self.notifications.append(self.snapshot())

    def snapshot(self):
        return self.hub.state, self.hub.available, deepcopy(self.hub.metrics)

    def drain(self):
        self.loop.run_until_complete(asyncio.sleep(0))

    def message(self, topic, payload):
        msg = mqtt.MQTTMessage(topic=topic if isinstance(topic, bytes) else topic.encode("utf-8"))
        msg.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        errors = []

        def receive():
            try:
                self.hub._on_message(self.hub._mqtt_client, None, msg)
            except BaseException as err:
                errors.append(err)

        thread = threading.Thread(target=receive)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

    @staticmethod
    def envelope(service, data):
        return {"devices": {"services": {service: {"events": {"stateChanged": data}}}}}

    def meter(self, data):
        self.message(self.hub._topic_monitor_meter, self.envelope("plugMetering", data))

    def switch(self, value):
        self.message(self.hub._topic_monitor_switch,
                     self.envelope("lcSwitchControl", {"power": value}))

    def heartbeat(self, value):
        self.message(self.hub._topic_monitor_heartbeat,
                     {"devices": {"operationState": value}})

    def assert_rejected(self, topic, payload):
        before = self.snapshot()
        count = len(self.notifications)
        self.message(topic, payload)
        self.assertEqual(self.snapshot(), before)
        self.drain()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.notifications), count)

    def test_real_dependencies_and_source(self):
        self.assertEqual(importlib.metadata.version("paho-mqtt"), "2.1.0")
        self.assertIsInstance(self.hub._mqtt_client, mqtt.Client)
        assert hub_module.__file__ is not None
        self.assertEqual(Path(hub_module.__file__).resolve(), SOURCE / "hub.py")

    def test_valid_metrics_atomic_on_loop(self):
        before = self.snapshot()
        self.meter({"power": "12.5", "current": 0.2, "voltage": 240,
                    "consumption": "13", "duration": "60"})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.notifications, [])
        self.drain()
        expected = (False, True, {"power": 12.5, "current": 0.2, "voltage": 240.0,
                                  "consumption": 13.0, "duration": 60})
        self.assertEqual(self.snapshot(), expected)
        self.assertEqual(self.notifications, [expected])

    def test_start_enforces_verified_tls_offline(self):
        calls = []
        contexts = []

        class FakeMQTTClient:
            def tls_set_context(self, context):
                contexts.append(context)
                calls.append("tls")

            def tls_insecure_set(self, *args, **kwargs):
                raise AssertionError("TLS verification must never be disabled")

            def connect(self, host, port, keepalive):
                calls.append((host, port, keepalive))

            def loop_start(self):
                calls.append("loop_start")

        async def executor(job):
            job()

        async def refresh():
            calls.append("refresh")

        self.hub._mqtt_client = FakeMQTTClient()
        self.hub.hass.async_add_executor_job = executor
        self.hub._send_meter_refresh = refresh
        self.loop.run_until_complete(self.hub.start())
        self.assertEqual(calls, ["tls", ("mqtt.platform.quboworld.com", 8883, 60),
                                 "loop_start", "refresh"])
        self.assertEqual(len(contexts), 1)
        self.assertIsInstance(contexts[0], ssl.SSLContext)
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexts[0].check_hostname)

    def test_switch_enums_apply_on_loop(self):
        for value in ("on", "off"):
            before = self.snapshot()
            self.switch(value)
            self.assertEqual(self.snapshot(), before)
            self.drain()
            self.assertEqual(self.hub.state, value == "on")
            self.assertTrue(self.hub.available)
        self.assertEqual(len(self.notifications), 2)

    def test_heartbeat_enums_apply_on_loop_and_deduplicate(self):
        for value in ("online", "offline", "offline", "online"):
            before = self.snapshot()
            self.heartbeat(value)
            self.assertEqual(self.snapshot(), before)
            self.drain()
            self.assertEqual(self.hub.available, value == "online")
        self.assertEqual(len(self.notifications), 3)

    def test_queued_heartbeats_use_loop_state(self):
        self.heartbeat("online")
        self.heartbeat("offline")
        self.assertFalse(self.hub.available)
        self.drain()
        self.assertFalse(self.hub.available)
        self.assertEqual([item[1] for item in self.notifications], [True, False])

    def test_omitted_metrics_preserved(self):
        expected = dict(self.hub.metrics, power=3.0)
        self.meter({"power": 3})
        self.drain()
        self.assertEqual(self.hub.metrics, expected)
        self.meter({"current": 0})
        self.drain()
        self.assertEqual(self.hub.metrics, dict(expected, current=0.0))

    def test_voltage_glitch_preserves_last_good_value(self):
        for value in (0, 0.11, 50, "50"):
            with self.subTest(value=value):
                self.meter({"voltage": value, "power": 2})
                self.drain()
                self.assertEqual(self.hub.metrics["voltage"], 230.0)
                self.assertEqual(self.hub.metrics["power"], 2.0)
        self.meter({"voltage": 50.1})
        self.drain()
        self.assertEqual(self.hub.metrics["voltage"], 50.1)

    def test_voltage_only_glitch_still_marks_online(self):
        self.meter({"voltage": 0})
        self.drain()
        self.assertTrue(self.hub.available)
        self.assertEqual(self.hub.metrics["voltage"], 230)

    def test_invalid_metrics_all_rejected_atomically(self):
        invalid = [True, False, None, [], {}, "", "secret-invalid-number", -1,
                   "-0.1", float("nan"), float("inf"), -float("inf"),
                   "NaN", "inf", "-inf", "1e10000", 10 ** 400]
        for key in self.hub.metrics:
            for value in invalid:
                with self.subTest(key=key, value=str(value)[:30]):
                    data = {"power": 999, "current": 9, "consumption": 90,
                            "duration": 99, "voltage": 240}
                    data[key] = value
                    self.assert_rejected(self.hub._topic_monitor_meter,
                                         self.envelope("plugMetering", data))

    def test_numeric_json_overflow_rejected(self):
        raw = json.dumps(self.envelope("plugMetering", {"power": "MARKER"})).encode()
        for token in (b"1e9999", b"NaN", b"Infinity", b"-Infinity"):
            self.assert_rejected(self.hub._topic_monitor_meter,
                                 raw.replace(b'"MARKER"', token))

    def test_exact_enum_validation(self):
        for value in (None, True, False, 0, 1, "", "ON", "OFF", " on", "on ",
                      "online", "unknown", [], {}):
            with self.subTest(value=value):
                self.assert_rejected(self.hub._topic_monitor_switch,
                                     self.envelope("lcSwitchControl", {"power": value}))
        for value in (None, True, False, 0, 1, "", "ONLINE", "OFFLINE", "online ",
                      "on", "unknown", [], {}):
            with self.subTest(value=value):
                self.assert_rejected(self.hub._topic_monitor_heartbeat,
                                     {"devices": {"operationState": value}})

    def test_dictionary_shapes_at_every_path(self):
        for service, topic in (("plugMetering", self.hub._topic_monitor_meter),
                               ("lcSwitchControl", self.hub._topic_monitor_switch)):
            path = ("devices", "services", service, "events", "stateChanged")
            for depth in range(len(path) + 1):
                for bad in (None, [], True, 1, "secret-bad-shape"):
                    payload = bad
                    for key in reversed(path[:depth]):
                        payload = {key: payload}
                    with self.subTest(service=service, depth=depth, bad=bad):
                        self.assert_rejected(topic, payload)
        for bad in (None, [], True, 1, "bad"):
            self.assert_rejected(self.hub._topic_monitor_heartbeat, {"devices": bad})

    def test_missing_optional_paths_do_not_reset(self):
        for topic in (self.hub._topic_monitor_switch, self.hub._topic_monitor_meter,
                      self.hub._topic_monitor_heartbeat):
            for payload in ({}, {"devices": {}}, {"devices": {"services": {}}}):
                self.assert_rejected(topic, payload)
        self.assert_rejected(self.hub._topic_monitor_meter,
                             self.envelope("plugMetering", {"unknown": 1}))

    def test_bad_utf8_json_and_deep_json(self):
        for payload in (b"\xff", b'{"secret":', b"", b"[" * 2000 + b"]" * 2000):
            self.assert_rejected(self.hub._topic_monitor_meter, payload)
        self.meter({"power": 2})
        self.drain()
        self.assertEqual(self.hub.metrics["power"], 2.0)

    def test_unknown_topics_filtered_before_parse(self):
        topics = [self.hub._topic_control_switch, self.hub._topic_control_meter,
                  self.hub._topic_monitor_meter + "/extra",
                  self.hub._topic_monitor_switch.upper(), "/other/device/heartbeat", "", b"\xff"]
        with patch.object(hub_module.json, "loads", side_effect=AssertionError("parsed")):
            for topic in topics:
                self.assert_rejected(topic, b"\xffsecret")

    def test_payload_limit_before_parse_and_exact_boundary(self):
        with patch.object(hub_module.json, "loads", side_effect=AssertionError("parsed")):
            self.assert_rejected(self.hub._topic_monitor_meter, b"x" * (65536 + 1))
        raw = json.dumps(self.envelope("plugMetering", {"power": 1})).encode()
        self.message(self.hub._topic_monitor_meter, raw.ljust(65536, b" "))
        self.drain()
        self.assertEqual(self.hub.metrics["power"], 1.0)

    def test_no_payload_or_exception_logs_or_per_message_flood(self):
        with self.assertNoLogs(hub_module._LOGGER, level="DEBUG"):
            for _ in range(100):
                self.message(self.hub._topic_monitor_meter, b'{"secret-payload":')
                self.message(self.hub._topic_monitor_meter, b"x" * 65537)
                self.switch("secret-invalid-enum")
                self.meter({"power": "secret-number"})
            self.meter({"power": 1, "secret-extra": "never-log-this"})
            self.drain()
        self.assertEqual(len(self.notifications), 1)


if __name__ == "__main__":
    unittest.main()
