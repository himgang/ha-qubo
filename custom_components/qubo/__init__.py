"""The Qubo integration."""

import logging
import time

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import validate_identifier, validate_login_response
from .const import (
    APP_ID,
    BASE_URL,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEVICE_ATTRIBUTE,
    DOMAIN,
    LOGIN_DEVICE_NAME,
)
from .hub import QuboHub

_LOGGER = logging.getLogger(__name__)

# Add sensor to the platforms list
PLATFORMS = ["sensor", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Qubo from a config entry."""
    session = async_get_clientsession(hass)
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    client_id = entry.data["client_id"]  # Retrieve the client_id we saved during config flow

    # --- YOUR EXISTING LOGIN & SYNC HTTP API CALLS GO HERE ---
    # After successfully parsing the sync response, you should have:
    # access_token, refresh_token, user_uuid, device_uuid, unit_uuid, expires_at, initial_state, device_name
    session = async_get_clientsession(hass)
    _LOGGER.info("Attempting to authenticate with Qubo API")

    # --- YOUR LOGIN API CALL GOES HERE ---
    login_url = f"{BASE_URL}sms/api/v4/sp/d10e4bfb0153496e8e8bb955f7ebe413/user/login"  # Replace with actual Qubo URL
    payload = {
        "accessToken": "",
        "deviceAttribute": DEVICE_ATTRIBUTE,
        "username": username,
        "password": password,
    }

    params = {"system": "CS"}

    headers = {
        "Host": "srvcapp.platform.quboworld.com",
        "User-Agent": "libcurl-agent restclient-cpp/2:1:1",
        "Accept": "*/*",
        "App-Id": APP_ID,
        "Login-Device-Name": LOGIN_DEVICE_NAME,  # Use the constant for the device name
        "Source": "ANDROID",
        "Source-Device-Id": client_id,
        "Token-Type": "USER",
    }

    try:
        async with session.post(
            login_url, json=payload, headers=headers, params=params,
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            # Check for errors BEFORE calling raise_for_status()
            if response.status != 200:
                _LOGGER.error(
                    "Qubo login returned HTTP status %s",
                    response.status,
                )
                return False  # Stop setting up the integration

            response.raise_for_status()
            data = await response.json()

            # Extract your tokens (Adjust keys based on Qubo's actual JSON response)
            access_token, refresh_token, user_uuid, expires_in = validate_login_response(data)

            # Calculate expiration time (current time + 3600 seconds)
            # We subtract 60 seconds just to give a safe buffer
            expires_at = time.time() + expires_in - 60

            _LOGGER.info("Successfully authenticated with Qubo API")

            # --- YOUR SYNC API CALL GOES HERE ---
            sync_url = f"{BASE_URL}unit-entity-management/api/v6/sp/d10e4bfb0153496e8e8bb955f7ebe413/units/sync"  # Replace with ACTUAL Sync API URL

            sync_headers = {
                "Host": "srvcapp.platform.quboworld.com",
                "User-Agent": "libcurl-agent restclient-cpp/2:1:1",
                "Accept": "*/*",
                "Login-Device-Name": LOGIN_DEVICE_NAME,  # Use the constant for the device name
                "Source-Device-Id": client_id,
                "Subscriber-Key": access_token,
                "Token-Type": "USER",
                "User-UUID": user_uuid,
            }

            sync_payload = {"syncType": 1}

            async with session.post(
                sync_url, headers=sync_headers, json=sync_payload,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30),
            ) as sync_response:
                if sync_response.status != 200:
                    _LOGGER.error("Qubo sync returned HTTP status %s", sync_response.status)
                    return False
                sync_response.raise_for_status()
                sync_data = await sync_response.json()
                if not isinstance(sync_data, dict):
                    raise ValueError("Invalid sync response")

                # 1. Extract Device & Unit UUIDs
                # Assuming you only have one device, or we just grab the first one
                devices = sync_data.get("devices", [])
                if not isinstance(devices, list):
                    raise ValueError("Invalid device list")
                if not devices:
                    _LOGGER.error("No devices found in Qubo account!")
                    return False

                # Search for the specific device by name
                qubo_device = None
                target_name = "Smart Plug 10A"

                for device in devices:
                    if device.get("deviceName") == target_name:
                        qubo_device = device
                        break

                # Stop setup if the device wasn't found in the API response
                if not qubo_device:
                    _LOGGER.error(
                        "Could not find a device named '%s' in your Qubo account!",
                        target_name,
                    )
                    return False

                device_uuid = validate_identifier(qubo_device.get("deviceUUID"))
                unit_uuid = validate_identifier(qubo_device.get("unitUUID"))
                device_name = qubo_device.get("deviceName")
                handle_name = qubo_device.get("handleName")

                # 2. Extract Initial State from Device Shadow
                initial_state = False
                initial_availability = False
                shadows = sync_data.get("deviceshadow", [])
                for shadow in shadows:
                    if shadow.get("deviceUUID") == device_uuid:
                        op_state_value = shadow.get("operationState", {}).get("value")
                        initial_availability = (op_state_value == "online")

                        services = shadow.get("services", [])
                        for service in services:
                            if service.get("service") == "lcSwitchControl":
                                power_attr = service.get("attributes", {}).get(
                                    "power", {}
                                )
                                initial_state = power_attr.get("value") == "on"
                                break

                _LOGGER.info("Successfully fetched Qubo data for: %s", device_name)

    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
        _LOGGER.error("Qubo setup failed: invalid response or connection failure")
        return False

    # Create the shared Hub and start it
    hub = QuboHub(
        hass,
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
        client_id,  # Pass the client_id to the Hub
        initial_availability
    )
    await hub.start()

    # Save the Hub to hass.data so switch.py and sensor.py can access it
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"hub": hub}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    await hub.stop()  # Disconnect MQTT safely

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
