"""Renew and persist Qubo account sessions."""

import math
import time

import aiohttp

from .const import BASE_URL, LOGIN_DEVICE_NAME
from .api import jwt_remaining


class SessionRejected(Exception):
    """The server rejected the refresh credentials."""


async def refresh_credentials(session, user_uuid, client_id, access, refresh, *, now=None, phone=False):
    url = f"{BASE_URL}sms/api/v1/sp/d10e4bfb0153496e8e8bb955f7ebe413/users/{user_uuid}/auth/refresh"
    headers = {"Host": "srvcapp.platform.quboworld.com", "User-Agent": "libcurl-agent restclient-cpp/2:1:1",
               "Accept": "*/*", "Login-Device-Name": LOGIN_DEVICE_NAME,
               "Source-Device-Id": client_id, "Token-Type": "USER"}
    async with session.post(url, json={"accessToken": access, "refreshToken": refresh},
                            headers=headers, allow_redirects=False,
                            timeout=aiohttp.ClientTimeout(total=30)) as response:
        if response.status in (401, 403):
            raise SessionRejected
        if response.status != 200:
            raise aiohttp.ClientError("Unexpected refresh HTTP status")
        response.raise_for_status()
        data = await response.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid token response")
    new_access = data.get("accessToken")
    new_refresh = data.get("refreshToken", refresh)
    for value in (new_access, new_refresh):
        if not isinstance(value, str) or not value.strip() or len(value) > 32768:
            raise ValueError("Invalid token response")
    lifetime = data.get("expires_in", 3600)
    if (isinstance(lifetime, bool) or not isinstance(lifetime, (int, float))
            or not 60 < lifetime <= 604800 or not math.isfinite(lifetime)):
        raise ValueError("Invalid token lifetime")
    now = time.time() if now is None else now
    if phone:
        # Phone access tokens have been observed with 15-minute lifetimes.
        lifetime = min(lifetime, 900)
        remaining = jwt_remaining(new_access, now)
        if remaining is not None:
            lifetime = min(lifetime, remaining)
        if lifetime <= 60:
            raise ValueError("Invalid token lifetime")
    return new_access, new_refresh, now + lifetime - 60


def persist_phone_session(hass, entry, access, refresh, expires_at):
    """Store rotated credentials before they are needed after a restart."""
    hass.config_entries.async_update_entry(entry, data={**entry.data,
        "access_token": access, "refresh_token": refresh, "expires_at": expires_at})
