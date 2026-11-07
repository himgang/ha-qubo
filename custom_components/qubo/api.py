"""Validation shared by Qubo authentication callers."""

import math
import re


def validate_identifier(value):
    """Require one literal identifier, not a URL path or MQTT wildcard."""
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
    ):
        raise ValueError("Invalid account or device identifier")
    return value


def validate_login_response(data):
    """Reject incomplete credentials before saving or using an account."""
    if not isinstance(data, dict):
        raise ValueError("Invalid authentication response")
    for key in ("accessToken", "refreshToken", "uuid"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Invalid authentication response")
    # This identifier is used in a URL path and an MQTT username, not a URL.
    validate_identifier(data["uuid"])
    expires_in = data.get("expires_in", 3600)
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, (int, float))
        or not 60 < expires_in <= 604800
        or not math.isfinite(expires_in)
    ):
        raise ValueError("Invalid token lifetime")
    return data["accessToken"], data["refreshToken"], data["uuid"], expires_in
