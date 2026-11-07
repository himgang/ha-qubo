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
        initial_availability
    ) -> None:
        """Initialize the Qubo hub."""
        self.hass = hass
        self.session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._lifecycle_lock = asyncio.Lock()
        self._stopped = False
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
        if rc == 0:
            _LOGGER.debug("Successfully connected to Qubo MQTT broker")
            client.subscribe(
                [(self._topic_monitor_switch, 0), (self._topic_monitor_meter, 0),(self._topic_monitor_heartbeat, 0)]
            )
        else:
            _LOGGER.error("Failed to connect to Qubo MQTT broker, return code %s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            topic = msg.topic
            _LOGGER.debug("Received MQTT message on topic %s: %s", topic, payload)

            if topic == self._topic_monitor_switch:
                state_data = (
                    payload.get("devices", {})
                    .get("services", {})
                    .get("lcSwitchControl", {})
                    .get("events", {})
                    .get("stateChanged", {})
                )
                if "power" in state_data:
                    _LOGGER.debug("Updating switch state to: %s", state_data["power"])
                    self.available = True  # If we got a message, it's online
                    self.state = state_data["power"] == "on"
                    self.hass.loop.call_soon_threadsafe(self._publish_update)
                else:
                    _LOGGER.debug("No 'power' attribute in switch state_data: %s", state_data)

            elif topic == self._topic_monitor_meter:
                metrics_data = (
                    payload.get("devices", {})
                    .get("services", {})
                    .get("plugMetering", {})
                    .get("events", {})
                    .get("stateChanged", {})
                )
                if metrics_data:
                    _LOGGER.debug("Updating metrics with: %s", metrics_data)
                    self.available = True  # If we got metrics, it's online
                    self.metrics["power"] = float(metrics_data.get("power", 0))
                    self.metrics["current"] = float(metrics_data.get("current", 0))
                    # self.metrics["voltage"] = float(metrics_data.get("voltage", 0))
                    self.metrics["consumption"] = float(
                        metrics_data.get("consumption", 0)
                    )
                    self.metrics["duration"] = int(metrics_data.get("duration", 0))

                    # Prevent zero-voltage drops from hitting the dashboard
                    new_voltage = float(metrics_data.get("voltage", 0))

                    # If the voltage is above 50V, it's a real reading.
                    # If it's below 50V (like 0.11V or 0V), it's a sensor glitch,
                    # so we ignore it and keep the last known good value.
                    if new_voltage > 50:
                        self.metrics["voltage"] = new_voltage

                    self.hass.loop.call_soon_threadsafe(self._publish_update)
                else:
                    _LOGGER.debug("No stateChanged data found in plugMetering payload")

            elif topic == self._topic_monitor_heartbeat:
                operation_state = payload.get("devices", {}).get("operationState")
                if operation_state:
                    # Update status and notify HA if it changed
                    is_available = (operation_state == "online")
                    if self.available != is_available:
                        self.available = is_available
                        _LOGGER.debug(f"Qubo plug went {'online' if is_available else 'offline'}")
                        self.hass.loop.call_soon_threadsafe(self._publish_update)
                else:
                    _LOGGER.debug("No operationState found in heartbeat payload")

        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as err:
            _LOGGER.error("Error processing MQTT message: %s. Payload: %s", err, msg.payload)
        except Exception as err:
            _LOGGER.error("Unexpected error in _on_message: %s", err)

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
                    self.hass, self._send_meter_refresh, timedelta(seconds=60)
                )

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

    async def _async_refresh_token_if_needed(self):
        # The expiry check must be inside the same lock as refresh and teardown:
        # concurrent commands must not rotate the same refresh token twice.
        async with self._lifecycle_lock:
            if self._stopped:
                raise QuboHubError("Qubo hub is stopped")
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
