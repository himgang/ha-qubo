# Token refresh and MQTT lifecycle fixes

Baseline: `295e1b5`. This branch serializes expiry checks, HTTP refresh, MQTT
restart and stop with one lifecycle lock. It validates a complete token response
before committing credentials, supports optional refresh-token rotation, and
blocks command publication when refresh/restart fails. A failed restart can be
retried without repeating token rotation. Blocking MQTT teardown runs off HA's
loop; cancellation retains the lock until executor work completes.

Refresh POSTs disable redirects, require HTTP 200 and use a 30-second timeout.
Transport errors are replaced by a fixed error with suppressed exception context,
so a framework traceback cannot expose arbitrary remote exception messages.
Expiry defaults to 3600 seconds when absent and otherwise must be finite numeric,
not bool, greater than 60 seconds and no more than seven days.

Run `python -m unittest discover -s tests -p test_hub_refresh.py -v` with aiohttp
and paho-mqtt 2.1.0 installed. Tests import actual hub source with fake HA,
HTTP responses and MQTT client; exercise overlapping calls, token validation,
rotation, restart retry, unexpected HTTP statuses, cancellation, executor-thread
teardown and error redaction. No cloud/broker/device traffic occurs.

This is an availability and credential-handling patch, not proof of an external
Qubo exploit. The live reverse-engineered refresh API and full HA runtime have
not been exercised. Separate HTTP-login/sync and MQTT-input fixes are required
to address the other audit findings.
