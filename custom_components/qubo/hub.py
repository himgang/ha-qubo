"""Qubo hub integration helper for Home Assistant."""

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
        changed = state is not None or bool(metrics) or self.available != available
        if state is not None:
            self.state = state
        self.metrics.update(metrics)
        self.available = available
        if changed:
            self._publish_update()

    async def start(self):
        """Start the MQTT connection and begin periodic metric refreshes."""

        def connect_mqtt():

            # Create a secure, default context that enforces certificate and hostname verification
            context = ssl.create_default_context()
            self._mqtt_client.tls_set_context(context)

            self._mqtt_client.connect("mqtt.platform.quboworld.com", 8883, 60)
            self._mqtt_client.loop_start()

        await self.hass.async_add_executor_job(connect_mqtt)

        # Request metrics immediately, then every 60 seconds
        await self._send_meter_refresh()
        self._unsub_refresh = async_track_time_interval(
            self.hass, self._send_meter_refresh, timedelta(seconds=60)
        )

    async def stop(self):
        """Stop the MQTT connection and cancel periodic metric refreshes."""
        if self._unsub_refresh:
            self._unsub_refresh()
        self._mqtt_client.loop_stop()
        self._mqtt_client.disconnect()

    async def _async_refresh_token_if_needed(self):
        if time.time() < self._expires_at:
            return

        # (INSERT YOUR EXACT HTTP REFRESH CALL HERE)
        # self._access_token = new_token
        # self._expires_at = new_expiration
        # self._mqtt_client.username_pw_set(username=self._user_uuid, password=self._access_token)
        # self._mqtt_client.disconnect() # Forces auto-reconnect with new token

        _LOGGER.info("Qubo access token expired. Refreshing")

        # --- YOUR REFRESH API CALL GOES HERE ---
        refresh_url = f"{BASE_URL}sms/api/v1/sp/d10e4bfb0153496e8e8bb955f7ebe413/users/{self._user_uuid}/auth/refresh"  # Replace with actual Qubo URL
        payload = {
            "accessToken": self._access_token,
            "refreshToken": self._refresh_token,
        }

        # set the headers as following
        headers = {
            "Host": "srvcapp.platform.quboworld.com",
            "User-Agent": "libcurl-agent restclient-cpp/2:1:1",
            "Accept": "*/*",
            "Login-Device-Name": LOGIN_DEVICE_NAME,
            "Source-Device-Id": self._client_id,  # Use the client_id we saved from the config flow
            "Token-Type": "USER",
        }

        try:
            async with self.session.post(
                refresh_url, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                data = await response.json()

                # Update our variables with the new data
                self._access_token = data.get("accessToken")
                # # Some APIs give you a new refresh token too, some don't.
                # if "refresh_token" in data:
                #     self._refresh_token = data.get("refresh_token")

                expires_in = data.get("expires_in", 3600)
                self._expires_at = time.time() + expires_in - 60

                _LOGGER.debug("Token refreshed successfully")
                # Force MQTT to reconnect with the new password (token)


                # self._mqtt_client.username_pw_set(
                #     username=self._user_uuid, password=self._access_token
                # )

                # # Disconnecting forces the background loop_start() thread to automatically
                # # reconnect using the newly set credentials.
                # self._mqtt_client.disconnect()

                # Replace with this proper reboot sequence:
                def restart_mqtt():
                    """Safely reboot the MQTT client in a background thread."""
                    _LOGGER.debug("Rebooting MQTT client with new token")
                    self._mqtt_client.loop_stop()     # Kill the dead thread
                    self._mqtt_client.disconnect()    # Ensure socket is closed

                    # Apply new credentials and reconnect
                    self._mqtt_client.username_pw_set(username=self._user_uuid, password=self._access_token)
                    self._mqtt_client.connect("mqtt.platform.quboworld.com", 8883, 60)
                    self._mqtt_client.loop_start()    # Start the fresh thread
                    _LOGGER.info("MQTT connection successfully restored")

                # Send this blocking sequence to the safe executor pool
                await self.hass.async_add_executor_job(restart_mqtt)


        except aiohttp.ClientError as err:
            _LOGGER.error("Failed to refresh Qubo token: %s", err)

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
