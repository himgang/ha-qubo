"""Qubo hub integration helper for Home Assistant."""

import asyncio
from datetime import timedelta
import json
import logging
import math
import ssl
import time

import aiohttp
import paho.mqtt.client as mqtt

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import BASE_URL, LOGIN_DEVICE_NAME

_LOGGER = logging.getLogger(__name__)


class QuboHubError(RuntimeError):
    """A refresh or lifecycle failure that prevents command publication."""


class QuboSession:
    """Share account credentials and serialize refresh across plug connections."""

    def __init__(self, access_token, refresh_token, expires_at):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.lock = asyncio.Lock()


class QuboHub:
    """Qubo hub integration helper for Home Assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        session,
        access_token,
        refresh_token,
        user_uuid,
        device_uuid,
        unit_uuid,
        expires_at,
        initial_state,
        device_name,
        handle_name,
        client_id,  # Accept the client_id in the constructor
        initial_availability,
        *, entry=None, phone_auth=False, auth=None
    ) -> None:
        """Initialize the Qubo hub."""
        self.hass = hass
        self.session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._auth = auth
        self._lifecycle_lock = auth.lock if auth is not None else asyncio.Lock()
        self._stopped = False
        self._entry = entry
        self._phone_auth = phone_auth
        self._reauth_started = False
        self._mqtt_auth_rejected = False
        self._last_reauth_attempt = None
        self._mqtt_needs_restart = False
        self._mqtt_tls_configured = False

        self._user_uuid = user_uuid
        self.device_uuid = device_uuid
        self._unit_uuid = unit_uuid
        self.device_name = device_name
        self._handle_name = handle_name
        self._client_id = client_id

        self.state = initial_state
        self.available = initial_availability
        self.metrics = {
            "power": None,
            "current": None,
            "voltage": None,
            "consumption": None,
            "duration": None,
        }
        self._callbacks = set()
        self._unsub_refresh = None

        self._topic_control_switch = (
            f"/control/{unit_uuid}/{device_uuid}/lcSwitchControl"
        )
        self._topic_monitor_switch = (
            f"/monitor/{unit_uuid}/{device_uuid}/lcSwitchControl"
        )
        self._topic_control_meter = (
            f"/control/{unit_uuid}/{device_uuid}/meteringRefresh"
        )
        self._topic_monitor_meter = f"/monitor/{unit_uuid}/{device_uuid}/plugMetering"
        self._topic_monitor_heartbeat = f"/monitor/{unit_uuid}/{device_uuid}/heartbeat"

        self._mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        # self._mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
        self._mqtt_client.username_pw_set(
            username=self._user_uuid, password=self._access_token
        )
        self._mqtt_client.on_connect = self._on_connect
        self._mqtt_client.on_message = self._on_message

    def register_callback(self, callback):
        """Register a callback to be called when the state updates."""
        self._callbacks.add(callback)

    def unregister_callback(self, callback):
        """Unregister a callback."""
        self._callbacks.discard(callback)

    def _publish_update(self):
        for callback in self._callbacks:
            callback()

    def _on_connect(self, client, userdata, flags, rc):
        if self._phone_auth and rc in (4, 5):
            # Paho invokes this on its network thread. Fail closed immediately,
            # then serialize shutdown and reauth on the HA event loop.
            self._mqtt_auth_rejected = True
            try:
                self.hass.loop.call_soon_threadsafe(self._schedule_mqtt_auth_failure)
            except RuntimeError:
                pass  # The HA event loop is already closed.
            return
        if self._phone_auth and (self._reauth_started or self._mqtt_auth_rejected or time.time() >= self._expires_at):
            return
        if rc == 0:
            _LOGGER.debug("Successfully connected to Qubo MQTT broker")
            client.subscribe(
                [(self._topic_monitor_switch, 0), (self._topic_monitor_meter, 0),(self._topic_monitor_heartbeat, 0)]
            )
        else:
            _LOGGER.error("Failed to connect to Qubo MQTT broker, return code %s", rc)

    def _schedule_mqtt_auth_failure(self):
        """Schedule rejection handling on the HA loop, not Paho's thread."""
        if not self._stopped:
            self.hass.async_create_task(self._handle_mqtt_auth_failure())

    async def _handle_mqtt_auth_failure(self):
        try:
            async with self._lifecycle_lock:
                if not self._stopped:
                    await self._expire_phone_session()
        except Exception:
            _LOGGER.exception("Unable to handle Qubo MQTT authentication failure")

    @staticmethod
    def _mqtt_object(data, *path):
        """Read an optional object path, rejecting non-object containers."""
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        for key in path:
            data = data.get(key, {})
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
        return data

    @staticmethod
    def _mqtt_number(value):
        """Accept numeric wire values, but not booleans or invalid readings."""
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("Expected a number")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("Expected a finite nonnegative number")
        return number

    @staticmethod
    def _reject_json_constant(value):
        """Reject nonstandard JSON constants without exposing input in logs."""
        raise ValueError("Invalid JSON constant")

    def _on_message(self, client, userdata, msg):
        # Filter before decoding: only these exact topics are subscribed.
        try:
            topic = msg.topic
        except UnicodeError:
            return
        if topic not in (
            self._topic_monitor_switch,
            self._topic_monitor_meter,
            self._topic_monitor_heartbeat,
        ):
            return
        if len(msg.payload) > 64 * 1024:
            return

        try:
            payload = json.loads(
                msg.payload.decode("utf-8"),
                parse_constant=self._reject_json_constant,
            )
            devices = self._mqtt_object(payload, "devices")
            if topic == self._topic_monitor_heartbeat:
                if "operationState" not in devices:
                    return
                operation_state = devices["operationState"]
                if operation_state not in ("online", "offline"):
                    return
                self.hass.loop.call_soon_threadsafe(
                    self._apply_mqtt_update, None, {}, operation_state == "online"
                )
                return

            service = (
                "lcSwitchControl"
                if topic == self._topic_monitor_switch
                else "plugMetering"
            )
            data = self._mqtt_object(
                devices, "services", service, "events", "stateChanged"
            )
            if topic == self._topic_monitor_switch:
                if "power" not in data or data["power"] not in ("on", "off"):
                    return
                state = data["power"] == "on"
                metrics = {}
            else:
                state = None
                metrics = {}
                for key in ("power", "current", "voltage", "consumption", "duration"):
                    if key not in data:
                        continue
                    number = self._mqtt_number(data[key])
                    # Keep the last good voltage for sensor glitches <= 50 V.
                    if key == "voltage" and number <= 50:
                        continue
                    metrics[key] = int(number) if key == "duration" else number
                if not any(
                    key in data
                    for key in ("power", "current", "voltage", "consumption", "duration")
                ):
                    return
        except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
            # No per-message logging: hostile input must not leak payloads or
            # create an unbounded log flood (even with debug logging enabled).
            return

        # Validation completes before queuing one atomic HA-loop update.
        self.hass.loop.call_soon_threadsafe(
            self._apply_mqtt_update, state, metrics, True
        )

    def _apply_mqtt_update(self, state, metrics, available):
        """Apply a fully validated message and notify only on the HA loop."""
        if self._phone_auth and (self._reauth_started or self._mqtt_auth_rejected or time.time() >= self._expires_at):
            return
        changed = state is not None or bool(metrics) or self.available != available
        if state is not None:
            self.state = state
        self.metrics.update(metrics)
        self.available = available
        if changed:
            self._publish_update()

    async def _async_mqtt_job(self, job):
        """Keep the lifecycle lock held until a cancelled executor job finishes."""
        task = asyncio.ensure_future(self.hass.async_add_executor_job(job))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated cancellation must not release the lock while the worker
            # can still reconnect a client that stop() is about to tear down.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    async def start(self):
        """Start MQTT without racing a refresh or an unload."""
        async with self._lifecycle_lock:
            if self._phone_auth and time.time() >= self._expires_at:
                from homeassistant.exceptions import ConfigEntryAuthFailed
                raise ConfigEntryAuthFailed("Qubo phone session expired")

            def connect_mqtt():
                if not self._mqtt_tls_configured:
                    context = ssl.create_default_context()
                    self._mqtt_client.tls_set_context(context)
                    self._mqtt_tls_configured = True
                self._mqtt_client.connect("mqtt.platform.quboworld.com", 8883, 60)
                self._mqtt_client.loop_start()

            await self._async_mqtt_job(connect_mqtt)
            self._stopped = False
            if self._unsub_refresh is None:
                self._unsub_refresh = async_track_time_interval(
                    self.hass, self._periodic_meter_refresh if self._phone_auth else self._send_meter_refresh, timedelta(seconds=60)
                )

        if self._phone_auth:
            await self._periodic_meter_refresh()
        else:
            await self._send_meter_refresh()

    async def stop(self):
        """Serialize teardown with refresh; never join Paho's thread on HA's loop."""
        async with self._lifecycle_lock:
            self._stopped = True
            if self._unsub_refresh:
                self._unsub_refresh()
                self._unsub_refresh = None

            def stop_mqtt():
                self._mqtt_client.disconnect()
                self._mqtt_client.loop_stop()

            await self._async_mqtt_job(stop_mqtt)

    async def _periodic_meter_refresh(self, now=None):
        """Keep a quiet timer to retry dismissed reauth flows."""
        try:
            await self._send_meter_refresh(now)
        except QuboHubError:
            if not self._reauth_started:
                raise

    async def _expire_phone_session(self):
        """Called under the lifecycle lock; phone tokens always require reauth.

        Even when a refresh token was supplied, avoid rotating an unpersisted
        phone session. A new OTP flow obtains a durable replacement instead.
        """
        if not self._reauth_started:
            self._reauth_started = True
            self.available = False
            self._publish_update()
            def disconnect():
                try:
                    self._mqtt_client.disconnect()
                finally:
                    self._mqtt_client.loop_stop()
            try:
                await self._async_mqtt_job(disconnect)
            except (OSError, RuntimeError):
                _LOGGER.warning("Unable to disconnect expired Qubo session")
        # HA deduplicates active flows. Retrying allows recovery if the user
        # dismissed a flow or entered a different account; no OTP is sent here.
        now = time.monotonic()
        if self._last_reauth_attempt is None or now - self._last_reauth_attempt >= 60:
            self._last_reauth_attempt = now
            try:
                self._entry.async_start_reauth(self.hass)
            except Exception:
                _LOGGER.exception("Unable to start Qubo reauthentication")

    async def _async_refresh_token_if_needed(self):
        # The expiry check must be inside the same lock as refresh and teardown:
        # concurrent commands must not rotate the same refresh token twice.
        async with self._lifecycle_lock:
            if self._stopped:
                raise QuboHubError("Qubo hub is stopped")
            if self._phone_auth and (self._reauth_started or self._mqtt_auth_rejected or time.time() >= self._expires_at):
                await self._expire_phone_session()
                raise QuboHubError("Qubo phone session requires reauthentication")
            if self._auth is not None:
                if self._access_token != self._auth.access_token:
                    self._mqtt_needs_restart = True
                self._access_token = self._auth.access_token
                self._refresh_token = self._auth.refresh_token
                self._expires_at = self._auth.expires_at
            if time.time() < self._expires_at and not self._mqtt_needs_restart:
                return

            try:
                if time.time() >= self._expires_at:
                    _LOGGER.info("Qubo access token expired. Refreshing")
                    refresh_url = f"{BASE_URL}sms/api/v1/sp/d10e4bfb0153496e8e8bb955f7ebe413/users/{self._user_uuid}/auth/refresh"
                    payload = {
                        "accessToken": self._access_token,
                        "refreshToken": self._refresh_token,
                    }
                    headers = {
                        "Host": "srvcapp.platform.quboworld.com",
                        "User-Agent": "libcurl-agent restclient-cpp/2:1:1",
                        "Accept": "*/*",
                        "Login-Device-Name": LOGIN_DEVICE_NAME,
                        "Source-Device-Id": self._client_id,
                        "Token-Type": "USER",
                    }
                    async with self.session.post(
                        refresh_url, json=payload, headers=headers,
                        allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status != 200:
                            raise aiohttp.ClientError("Unexpected refresh HTTP status")
                        response.raise_for_status()
                        data = await response.json()

                    if not isinstance(data, dict):
                        raise ValueError("Invalid token response")
                    access_token = data.get("accessToken")
                    refresh_token = data.get("refreshToken", self._refresh_token)
                    if not isinstance(access_token, str) or not access_token.strip():
                        raise ValueError("Invalid access token")
                    if not isinstance(refresh_token, str) or not refresh_token.strip():
                        raise ValueError("Invalid refresh token")
                    expires_in = data.get("expires_in", 3600)
                    # Allow at most seven days, and require a lifetime longer than
                    # the 60-second safety margin. Missing lifetime keeps the
                    # historical one-hour default; malformed values fail closed.
                    if (
                        isinstance(expires_in, bool)
                        or not isinstance(expires_in, (int, float))
                        or not 60 < expires_in <= 604800
                        or not math.isfinite(expires_in)
                    ):
                        raise ValueError("Invalid token lifetime")

                    # Commit only after the entire response is validated.
                    self._access_token = access_token
                    self._refresh_token = refresh_token
                    self._expires_at = time.time() + expires_in - 60
                    if self._auth is not None:
                        self._auth.access_token = access_token
                        self._auth.refresh_token = refresh_token
                        self._auth.expires_at = self._expires_at
                    self._mqtt_needs_restart = True

                def restart_mqtt():
                    self._mqtt_client.disconnect()
                    self._mqtt_client.loop_stop()
                    self._mqtt_client.username_pw_set(
                        username=self._user_uuid, password=self._access_token
                    )
                    self._mqtt_client.connect("mqtt.platform.quboworld.com", 8883, 60)
                    self._mqtt_client.loop_start()

                await self._async_mqtt_job(restart_mqtt)
                self._mqtt_needs_restart = False
            except (
                aiohttp.ClientError, TimeoutError, ValueError, TypeError,
                OSError, RuntimeError,
            ):
                # Do not leak response bodies/tokens or allow commands to proceed
                # after an HTTP, validation, or MQTT restart failure.
                raise QuboHubError("Unable to refresh Qubo connection") from None


    async def turn_on(self):
        """Turn on the device."""
        await self._async_refresh_token_if_needed()
        payload = {
            "command": {
                "devices": {
                    "deviceUUID": self.device_uuid,
                    "handleName": self._handle_name,
                    "services": {
                        "lcSwitchControl": {
                            "attributes": {"power": "on"},
                            "instanceId": 0,
                        }
                    },
                }
            },
            "deviceUUID": self.device_uuid,
            "msgSequenceId": int(time.time() * 1000),
            "srcDeviceId": self._client_id,  # Use the client_id we saved from the config flow
            "timestamp": int(time.time() * 1000),
        }
        self._mqtt_client.publish(self._topic_control_switch, json.dumps(payload))

    async def turn_off(self):
        """Turn off the device."""
        await self._async_refresh_token_if_needed()
        payload = {
            "command": {
                "devices": {
                    "deviceUUID": self.device_uuid,
                    "handleName": self._handle_name,
                    "services": {
                        "lcSwitchControl": {
                            "attributes": {"power": "off"},
                            "instanceId": 0,
                        }
                    },
                }
            },
            "deviceUUID": self.device_uuid,
            "msgSequenceId": int(time.time() * 1000),
            "srcDeviceId": self._client_id,  # Use the client_id we saved from the config flow
            "timestamp": int(time.time() * 1000),
        }
        self._mqtt_client.publish(self._topic_control_switch, json.dumps(payload))

    async def _send_meter_refresh(self, now=None):
        _LOGGER.debug("Requesting meter refresh for Qubo device %s", self.device_uuid)
        await self._async_refresh_token_if_needed()
        payload = {
            "command": {
                "devices": {
                    "deviceUUID": self.device_uuid,
                    "handleName": self._handle_name,
                    "services": {
                        "meteringRefresh": {
                            "attributes": {"duration": "60"},
                            "instanceId": 0,
                        }
                    },
                }
            },
            "deviceUUID": self.device_uuid,
            "msgSequenceId": int(time.time() * 1000),
            "srcDeviceId": self._client_id,  # Use the client_id we saved from the config flow
            "timestamp": int(time.time() * 1000),
        }
        _LOGGER.debug("Publishing meter refresh payload to topic %s: %s", self._topic_control_meter, payload)
        self._mqtt_client.publish(self._topic_control_meter, json.dumps(payload))
