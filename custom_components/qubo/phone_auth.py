"""Phone authentication for the Qubo cloud API.

Protocol references: selvakk2k/qubopy auth.py and const.py at commit
2ff6a214792be5ff57f0edbc314c25c54e5e48c3 (MIT). This module is independently
implemented; these unofficial endpoints and session lifetimes may change.
"""

import base64
import json
import math
import re
import time

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import validate_identifier
from .const import BASE_URL, DEVICE_ATTRIBUTE

_PROVIDER = "d10e4bfb0153496e8e8bb955f7ebe413"
_PHONE_BASE = f"{BASE_URL}sms/api/v1/sp/{_PROVIDER}/user/phone/"
# Unlike the password endpoint, this route uses the Android package identifier.
_PHONE_APP_ID = "com.hero.iot"


class PhoneAuthError(Exception):
    """A phone login request could not be completed safely."""


class InvalidOtp(PhoneAuthError):
    """The phone verification request was rejected."""


def _validate_phone(phone, country_code):
    if not isinstance(phone, str) or not re.fullmatch(r"[0-9]{4,15}", phone):
        raise PhoneAuthError("Invalid phone number")
    if not isinstance(country_code, str) or not re.fullmatch(r"\+[1-9][0-9]{0,3}", country_code):
        raise PhoneAuthError("Invalid country code")


def _jwt_remaining(token, now):
    """Use unverified JWT expiry only to shorten, never extend, a session."""
    try:
        parts = token.split(".")
        if len(parts) != 3 or len(parts[1]) > 8192:
            return None
        body = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp = body.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
            return None
        return exp - now
    except (ValueError, TypeError, UnicodeError, OverflowError, AttributeError):
        return None


def _profile(data, now):
    """Normalize Qubo's documented profile variants, fail closed on bad fields."""
    if not isinstance(data, dict):
        raise PhoneAuthError("Invalid authentication response")
    token = data.get("accessToken") or data.get("token")
    account_id = data.get("uuid") or data.get("userUUID") or data.get("userId")
    if not isinstance(token, str) or not token.strip() or len(token) > 32768:
        raise PhoneAuthError("Invalid authentication response")
    try:
        validate_identifier(account_id)
    except ValueError:
        raise PhoneAuthError("Invalid authentication response") from None
    # Conservative 1-hour ceiling: the upstream OTP client observed 1-hour
    # sessions without refresh. Never trust an unverified JWT to extend it.
    lifetime = 3600
    if "expires_in" in data:
        raw = data["expires_in"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 60 < raw <= 604800:
            raise PhoneAuthError("Invalid authentication response")
        lifetime = min(lifetime, raw)
    remaining = _jwt_remaining(token, now)
    if remaining is not None:
        lifetime = min(lifetime, remaining)
    if not 60 < lifetime <= 3600:
        raise PhoneAuthError("Expired or invalid authentication response")
    refresh = data.get("refreshToken")
    if refresh is not None and (not isinstance(refresh, str) or not refresh.strip() or len(refresh) > 32768):
        raise PhoneAuthError("Invalid authentication response")
    result = {"uuid": account_id, "accessToken": token, "expires_in": lifetime}
    if refresh:
        result["refreshToken"] = refresh
    return result


async def _post(hass, action, payload, client_id):
    try:
        validate_identifier(client_id)
        async with async_get_clientsession(hass).post(
            _PHONE_BASE + action,
            json=payload,
            headers={"Content-Type": "application/json", "App-Id": _PHONE_APP_ID,
                     "Source-Device-Id": client_id, "Source": "USER"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                if action == "verify" and response.status in (400, 401, 403):
                    raise InvalidOtp("Phone verification rejected")
                raise PhoneAuthError("Phone authentication unavailable")
            return await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
        raise PhoneAuthError("Phone authentication unavailable") from None


async def request_phone_otp(hass, phone, country_code, client_id):
    """Request an SMS and return its opaque login challenge identifier."""
    _validate_phone(phone, country_code)
    response = await _post(hass, "login", {
        "phone": phone, "isdCode": country_code, "deviceAttribute": DEVICE_ATTRIBUTE,
    }, client_id)
    if not isinstance(response, dict):
        raise PhoneAuthError("Invalid authentication response")
    challenge = response.get("uuid")
    try:
        return validate_identifier(challenge)
    except ValueError:
        raise PhoneAuthError("Invalid authentication response") from None


async def verify_phone_otp(hass, phone, country_code, client_id, session_uuid, code):
    """Verify a user-entered OTP and return normalized, selectable profiles."""
    _validate_phone(phone, country_code)
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,10}", code):
        raise InvalidOtp("Phone verification rejected")
    try:
        validate_identifier(session_uuid)
    except ValueError:
        raise PhoneAuthError("Invalid challenge") from None
    response = await _post(hass, "verify", {
        "phone": phone, "isdCode": country_code, "otp": code,
        "deviceAttribute": DEVICE_ATTRIBUTE, "uuid": session_uuid,
    }, client_id)
    if isinstance(response, dict):
        response = [response]
    if not isinstance(response, list) or not 1 <= len(response) <= 50:
        raise PhoneAuthError("Invalid authentication response")
    now = time.time()
    profiles = [_profile(profile, now) for profile in response]
    if len({profile["uuid"] for profile in profiles}) != len(profiles):
        raise PhoneAuthError("Ambiguous authentication response")
    return profiles
