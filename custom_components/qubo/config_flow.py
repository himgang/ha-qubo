"""Config flow for the Qubo integration."""

import logging
import time
import uuid

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import validate_login_response
from .phone_auth import InvalidOtp, PhoneAuthError, exchange_phone_profile, request_phone_otp, verify_phone_otp
from .const import (
    APP_ID,
    BASE_URL,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEVICE_ATTRIBUTE,
    DOMAIN,
    LOGIN_DEVICE_NAME,
)

_LOGGER = logging.getLogger(__name__)

# The form layout users will see in the UI
DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict) -> dict:
    """Validate the user input allows us to connect."""
    session = async_get_clientsession(hass)
    login_url = f"{BASE_URL}sms/api/v4/sp/d10e4bfb0153496e8e8bb955f7ebe413/user/login"

    payload = {
        "accessToken": "",
        "deviceAttribute": DEVICE_ATTRIBUTE,
        "username": data[CONF_USERNAME],
        "password": data[CONF_PASSWORD],
    }

    # Generate a random 16-character hex string for this specific Home Assistant instance
    client_id = uuid.uuid4().hex[:16]

    headers = {
        "Content-Type": "application/json",
        "Host": "srvcapp.platform.quboworld.com",
        "User-Agent": "libcurl-agent restclient-cpp/2:1:1",
        "App-Id": APP_ID,
        "Login-Device-Name": LOGIN_DEVICE_NAME,  # Use the constant for the device name
        "Source": "ANDROID",
        "Source-Device-Id": client_id,
        "Token-Type": "USER",
    }
    params = {"system": "CS"}

    _LOGGER.debug("Attempting to login to Qubo with URL: %s", login_url)
    # Don't log password or full payload to avoid leaking credentials
    _LOGGER.debug("Login headers: %s", headers)

    try:
        async with session.post(
            login_url, json=payload, headers=headers, params=params,
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            _LOGGER.debug("Login response status: %s", response.status)
            
            if response.status != 200:
                _LOGGER.error("Qubo login returned HTTP status %s", response.status)
                
            if response.status in {401, 403}:
                raise InvalidAuth
            if response.status != 200:
                raise CannotConnect

            validate_login_response(await response.json())
            return {"title": data[CONF_USERNAME], "client_id": client_id}
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
        # Remote response bodies and exception messages may contain credentials.
        _LOGGER.error("Qubo login failed: invalid response or connection failure")
        raise CannotConnect from None


class QuboConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Qubo."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Choose an authentication method; accept legacy password submissions."""
        if user_input and CONF_USERNAME in user_input:
            return await self.async_step_password(user_input)
        if user_input:
            if user_input["auth_mode"] == "phone":
                return await self.async_step_phone()
            return await self.async_step_password()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("auth_mode", default="password"): vol.In(["password", "phone"])}),
        )

    async def async_step_password(self, user_input=None):
        """Keep the existing username/password login behavior."""
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data={
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    "client_id": info["client_id"],
                })
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except (ValueError, TypeError, aiohttp.ClientError, TimeoutError):
                errors["base"] = "unknown"
        return self.async_show_form(step_id="password", data_schema=DATA_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data):
        """Reauthenticate the existing phone entry, retaining its client ID."""
        self._reauth_entry = self._get_reauth_entry()
        if self._reauth_entry.data.get("auth_mode") != "phone":
            return self.async_abort(reason="reauth_not_supported")
        return await self.async_step_phone()

    async def async_step_phone(self, user_input=None):
        """Request a new one-time code; each request resets old state."""
        errors = {}
        if user_input is not None:
            self._phone = user_input["phone"].strip()
            self._country_code = user_input["country_code"].strip()
            self._client_id = (self._reauth_entry.data.get("client_id")
                               if getattr(self, "_reauth_entry", None) else None) or uuid.uuid4().hex[:16]
            self._session_uuid = None
            self._profiles = None
            try:
                self._session_uuid = await request_phone_otp(
                    self.hass, self._phone, self._country_code, self._client_id
                )
                self._session_started = time.monotonic()
                return await self.async_step_otp()
            except PhoneAuthError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
        old = getattr(self, "_reauth_entry", None)
        data = old.data if old else {}
        schema = vol.Schema({
            vol.Required("phone", default=data.get("phone", "")): str,
            vol.Required("country_code", default=data.get("country_code", "+91")): str,
        })
        return self.async_show_form(step_id="phone", data_schema=schema, errors=errors)

    async def async_step_otp(self, user_input=None):
        """Verify the code without persisting it or logging remote errors."""
        if not getattr(self, "_session_uuid", None) or time.monotonic() - self._session_started > 300:
            self._session_uuid = None
            return await self.async_step_phone()
        errors = {}
        if user_input is not None:
            try:
                profiles = await verify_phone_otp(
                    self.hass, self._phone, self._country_code, self._client_id,
                    self._session_uuid, user_input["code"],
                )
                self._session_uuid = None
                self._profiles = {p["uuid"]: p for p in profiles if p.get("uuid") and p.get("accessToken")}
                self._profiles_received_at = time.time()
                if not self._profiles:
                    errors["base"] = "invalid_auth"
                else:
                    self._profiles_started = time.monotonic()
            except InvalidOtp:
                errors["base"] = "invalid_otp"
            except PhoneAuthError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            if self._profiles:
                if len(self._profiles) == 1:
                    return await self._finish_phone(next(iter(self._profiles)))
                return await self.async_step_profile()
            # Failed verification requires a fresh challenge, never reuse a session.
            self._session_uuid = None
            return self.async_show_form(step_id="phone", data_schema=vol.Schema({
                vol.Required("phone", default=self._phone): str,
                vol.Required("country_code", default=self._country_code): str,
            }), errors=errors)
        return self.async_show_form(step_id="otp", data_schema=vol.Schema({vol.Required("code"): str}), errors=errors)

    async def async_step_profile(self, user_input=None):
        """Select among verified profiles without revealing profile details."""
        if not getattr(self, "_profiles", None) or time.monotonic() - self._profiles_started > 300:
            self._profiles = None
            return await self.async_step_phone()
        if user_input is not None and user_input.get("profile") in self._profiles:
            return await self._finish_phone(user_input["profile"])
        choices = {key: f"Account {index + 1} (…{key[-4:]})" for index, key in enumerate(self._profiles)}
        return self.async_show_form(step_id="profile", data_schema=vol.Schema({vol.Required("profile"): vol.In(choices)}))

    async def _finish_phone(self, profile_id):
        """Store only the chosen profile's credentials and reload on reauth."""
        profile = self._profiles[profile_id]
        reauth = getattr(self, "_reauth_entry", None)
        if reauth and reauth.data.get("user_uuid") != profile_id:
            self._profiles = None
            return self.async_abort(reason="wrong_account")
        ttl = profile["expires_in"]
        expires_at = self._profiles_received_at + ttl - 60
        if expires_at <= time.time():
            self._profiles = None
            return await self.async_step_phone()
        try:
            profile = await exchange_phone_profile(self.hass, profile, self._client_id)
        except PhoneAuthError:
            self._profiles = None
            return self.async_show_form(step_id="phone", data_schema=vol.Schema({
                vol.Required("phone", default=self._phone): str,
                vol.Required("country_code", default=self._country_code): str,
            }), errors={"base": "cannot_connect"})
        expires_at = time.time() + profile["expires_in"] - 60
        data = {
            "auth_mode": "phone", "phone": self._phone, "country_code": self._country_code,
            "client_id": self._client_id, "user_uuid": profile_id,
            "access_token": profile["accessToken"], "expires_at": expires_at,
        }
        if profile.get("refreshToken"):
            data["refresh_token"] = profile["refreshToken"]
        self._profiles = None
        if reauth:
            await self.async_set_unique_id(profile_id)
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            # data_updates merges with existing entry data, so explicitly clear an
            # old refresh token when the newly authenticated profile has none.
            data.setdefault("refresh_token", None)
            return self.async_update_reload_and_abort(reauth, data_updates=data)
        await self.async_set_unique_id(profile_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"Qubo (…{profile_id[-4:]})", data=data)


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate there is invalid auth."""
