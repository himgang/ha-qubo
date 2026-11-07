# MQTT input validation fixes

Baseline: `295e1b5`. Previously, malformed remote metrics could install NaN or
infinite values, invalid enums could report OFF/offline, and a later invalid
field could leave an earlier metric partially applied. Arbitrary incoming
payloads were also parsed and logged without an application size limit.

This branch filters exact subscribed topics before decoding, caps payloads at
64 KiB, requires nested objects and valid enums, rejects nonfinite/negative or
boolean numeric values, and applies validated updates atomically on HA's loop.
Missing metrics retain their values, including the existing <=50 V voltage-glitch
filter. Incoming raw payload/error logging is removed rather than replaced by
an attacker-amplifiable per-message log.

Run `python -m unittest discover -s tests -p test_mqtt_input_validation.py -v`
with aiohttp and paho-mqtt 2.1.0 installed. Eighteen offline tests import actual
source with HA stubs and real Paho. They cover valid and malformed messages,
UTF-8/JSON/deep nesting, size/topic filtering, invalid enums/numerics, atomic
rejection and HA-loop application. A fake-transport startup test also checks
certificate verification and hostname checking stay enabled.

These input risks require messages delivered through the authenticated cloud
broker; no broker ACL bypass or malicious publisher was demonstrated. The size
cap limits application parsing, not Paho's prior wire-frame allocation. There is
no application-level valid-message rate limiter. No live broker, plug or full HA
runtime was tested. HTTP and token-refresh/lifecycle fixes are separate branches.
