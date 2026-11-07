# HTTP credential-boundary fixes

Baseline reviewed: `295e1b50abb0b0cf77724400e049adbc72e2b3d9`.

## Confirmed defects

- Config flow and setup logged arbitrary authentication error response bodies.
  Synthetic secret markers in an offline error response were logged verbatim.
  Actual disclosure depends on upstream response contents; this is not evidence
  that Qubo echoes passwords.
- Four credential-bearing POST calls inherited aiohttp's automatic redirects.
  A local two-server control reproduces forwarding a login password through a
  307 response. Login/refresh JSON and the sync request's custom token headers
  must stay at the fixed API destination. This is conditional on an upstream
  redirect, not a demonstrated unauthenticated compromise of Qubo.
- Config flow accepted statuses below 400 without validating a response body.
  This could save unusable credentials; it did not bypass server authorization.
- Setup did not validate account/device identifiers before URL/topic use.

## Changes

Disable redirects on login, sync and refresh; reject unexpected HTTP statuses;
set a 30-second request timeout. Never log arbitrary authentication response
bodies or uncontrolled exception text. Require nonempty access/refresh tokens
and account ID before accepting login. Reject URL separators and MQTT wildcards
in account/device topic segments. Validate numeric token lifetime (greater than
60 seconds and at most 7 days; default 3600 when omitted).

The reverse-engineered API does not provide a stable published schema. These
checks preserve the existing field names but intentionally fail closed on
missing/invalid values. They have not been validated against a live Qubo account.

## Tests

Run `python -m unittest discover -s tests -p test_http_security.py -v` with aiohttp,
paho-mqtt 2.1.0 and voluptuous installed. Tests import the actual integration
source with only HA framework dependencies stubbed. All HTTP traffic is loopback
between ephemeral local servers, including redirect sinks. No real account,
MQTT broker or Home Assistant instance is contacted.

Tests exercise all four requests against 301/302/303/307/308 redirects, custom
sync token headers, success/failure authentication schemas, valid setup, invalid
topic identifiers, synthetic secret-log markers, explicit timeout policy and a
control reproducing the pre-fix 307 leak.

This branch does not fix the separate MQTT-input or token-refresh-concurrency
findings. Current baseline MQTT TLS already validates certificates and hostnames;
the historical CERT_NONE issue was fixed before this review.
