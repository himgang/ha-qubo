"""The Qubo integration."""

import asyncio
import logging
import math
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
from .hub import QuboHub, QuboSession
from .session import SessionRejected, persist_phone_session, refresh_credentials

_LOGGER = logging.getLogger(__name__)

# Add sensor to the platforms list
PLATFORMS = ["sensor", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Qubo from a config entry."""
    session = async_get_clientsession(hass)
    phone_auth = entry.data.get("auth_mode") == "phone"
    client_id = entry.data["client_id"]  # Retrieve the client_id we saved during config flow

    if phone_auth:
        # Phone sessions are already authenticated by the config flow. The
        # saved expiry includes its safety margin; never extend it here.
        from homeassistant.exceptions import ConfigEntryAuthFailed

        access_token = entry.data.get("access_token")
        refresh_token = entry.data.get("refresh_token")
        user_uuid = entry.data.get("user_uuid")
        expires_at = entry.data.get("expires_at")
        try:
            validate_identifier(user_uuid)
            if (not isinstance(access_token, str) or not access_token.strip()
                    or isinstance(expires_at, bool)
                    or not isinstance(expires_at, (int, float))
                    or not math.isfinite(expires_at)):
                raise ValueError("Invalid phone session")
        except ValueError:
            raise ConfigEntryAuthFailed("Qubo phone session expired or invalid") from None
        if expires_at <= time.time():
            if not isinstance(refresh_token, str) or not refresh_token.strip():
                raise ConfigEntryAuthFailed("Qubo phone session expired")
            try:
                access_token, refresh_token, expires_at = await refresh_credentials(
                    session, user_uuid, client_id, access_token, refresh_token, phone=True)
                persist_phone_session(hass, entry, access_token, refresh_token, expires_at)
            except SessionRejected:
                raise ConfigEntryAuthFailed("Qubo phone session rejected") from None
            except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
                from homeassistant.exceptions import ConfigEntryNotReady
                raise ConfigEntryNotReady("Qubo session refresh temporarily unavailable") from None
    else:
        # --- YOUR LOGIN API CALL GOES HERE ---
        login_url = f"{BASE_URL}sms/api/v4/sp/d10e4bfb0153496e8e8bb955f7ebe413/user/login"  # Replace with actual Qubo URL
        username = entry.data[CONF_USERNAME]
        password = entry.data[CONF_PASSWORD]
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
        if not phone_auth:
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
            if phone_auth and sync_response.status in (401, 403):
                raise ConfigEntryAuthFailed("Qubo phone session rejected")
            if sync_response.status != 200:
                _LOGGER.error("Qubo sync returned HTTP status %s", sync_response.status)
                return False
            sync_response.raise_for_status()
            sync_data = await sync_response.json()
            if not isinstance(sync_data, dict):
                raise ValueError("Invalid sync response")

            devices = sync_data.get("devices", [])
            if not isinstance(devices, list):
                raise ValueError("Invalid device list")

            plugs = {}
            shadows = {
                shadow.get("deviceUUID"): shadow
                for shadow in sync_data.get("deviceshadow", [])
                if isinstance(shadow, dict) and isinstance(shadow.get("deviceUUID"), str)
            }
            for device in devices:
                if not isinstance(device, dict):
                    continue
                name = device.get("deviceName") or device.get("name")
                model = device.get("deviceModel") or device.get("model") or device.get("aModelNo")
                kind = device.get("deviceType") or device.get("type")
                if not (
                    isinstance(model, str) and model.upper().startswith("HSP")
                    or isinstance(kind, str) and "PLUG" in kind.upper()
                    or name == "Smart Plug 10A"
                ):
                    continue

                device_uuid = validate_identifier(device.get("deviceUUID"))
                unit_uuid = validate_identifier(device.get("unitUUID"))
                if not isinstance(name, str) or not name.strip():
                    name = f"Qubo Smart Plug (…{device_uuid[-4:]})"
                shadow = shadows.get(device_uuid, {})
                initial_availability = shadow.get("operationState", {}).get("value") == "online"
                initial_state = False
                for service in shadow.get("services", []):
                    if service.get("service") == "lcSwitchControl":
                        initial_state = service.get("attributes", {}).get("power", {}).get("value") == "on"
                        break
                plugs[device_uuid] = (
                    unit_uuid, name.strip(), device.get("handleName"), initial_state, initial_availability,
                )

            if not plugs:
                _LOGGER.error("No supported smart plugs found in the Qubo account")
                return False

    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
        _LOGGER.error("Qubo setup failed: invalid response or connection failure")
        return False

    if phone_auth and time.time() >= expires_at:
        raise ConfigEntryAuthFailed("Qubo phone session expired")

    auth = QuboSession(access_token, refresh_token, expires_at)
    hubs = []
    try:
        for device_uuid, (unit_uuid, name, handle, state, available) in plugs.items():
            hub = QuboHub(
                hass, session, access_token, refresh_token, user_uuid, device_uuid,
                unit_uuid, expires_at, state, name, handle, client_id, available,
                entry=entry, phone_auth=phone_auth, auth=auth,
            )
            hubs.append(hub)
            await hub.start()
    except BaseException:
        await asyncio.gather(*(hub.stop() for hub in hubs), return_exceptions=True)
        raise

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = hubs

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hubs = hass.data[DOMAIN].pop(entry.entry_id)
        await asyncio.gather(*(hub.stop() for hub in hubs))
    return unload_ok
