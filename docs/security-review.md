# Security review of ha-qubo

## Scope

Reviewed baseline `295e1b50abb0b0cf77724400e049adbc72e2b3d9`: all tracked
integration modules, configuration, workflows, and the 18 reachable commits
containing 43 historical blobs. This was a source/history review with offline
reproductions, not a penetration test of Qubo or a live Home Assistant deployment.

## Malicious-behavior assessment

No evidence of deliberate malware was found in the reviewed code/history:
no shell execution, dynamic-code loaders, suspicious downloaders or credential
transmissions to unrelated destinations were observed. Runtime cloud traffic is
to Qubo's fixed HTTPS API and MQTT broker. That does not prove that the service,
GitHub Actions, dependency artifacts, or every future release is trustworthy.

Six historical Python 3.14 bytecode files were accidentally tracked, then removed.
Without executing their code, their instruction/constant/name/argument/closure
fingerprints were compared to compilation of the corresponding historical source
using a matching Python bytecode format. All six matched. The historical TLS
`CERT_NONE` setting was fixed in `d2feb28`; current baseline MQTT TLS already
requires certificate verification and hostname checks.

## Confirmed defects and conditional risks

- **Authentication log exposure:** complete error bodies are logged in config
  flow and setup. Offline synthetic secrets appeared verbatim in baseline logs.
  Whether real account secrets leak depends on upstream response contents.
- **Conditional credential forwarding:** credential-bearing HTTP requests follow
  redirects by default. A local two-origin 307 reproduction forwards a synthetic
  login password. This assumes an upstream redirect; no unauthenticated control
  of Qubo's redirect behavior was demonstrated.
- **Authentication response validation:** apparent HTTP success without usable
  tokens is accepted; invalid topic identifiers are not checked before use.
  This causes broken entries, not server-side authentication bypass.
- **MQTT data integrity:** invalid enums, nonfinite numbers and partial writes
  can corrupt reported state. Unbounded payload parsing/raw-payload logging
  permit resource/log amplification by data delivered on the authenticated
  broker connection. Broker ACL bypass was not established.
- **Refresh/lifecycle availability:** simultaneous expiry callers refresh and
  restart concurrently; incomplete responses replace credentials with unusable
  values, rotated refresh tokens are ignored, and shutdown blocks HA's loop.
- **Dependency/CI hardening:** the manifest permits incompatible Paho 1.x and
  unknown future versions while source requires the 2.x callback API. Workflow
  action refs are mutable, and hassfest does not explicitly minimize permissions.
  No action compromise or malicious dependency was demonstrated.

## Proposed patch branches

- `fix/http-credential-boundaries`: redirect containment, safe logs, validated
  authentication/identifiers, request timeouts; see `security-http.md`.
- `fix/mqtt-input-validation`: bounded parsing, valid enums/metrics, atomic
  event-loop state application and no raw payload logging.
- `fix/serialized-token-refresh`: serialized validated refresh and coordinated
  nonblocking MQTT lifecycle.
- `fix/pin-reviewed-dependencies`: minimum compatible MQTT dependency and verified
  immutable workflow references, least privilege and regression-test CI.

These are proposed changes, not a claim that all branches are already merged or
that Home Assistant has been upgraded. Individual branch tests and integration
checks must pass before merge. No live account credentials, broker, plug or HA
instance are used by the offline tests.

## Residual risks and operational notes

The reverse-engineered Qubo API has no stable public schema, so compatibility
must ultimately be checked against the real service with the owner's consent.
The integration intentionally stores the login password in HA's config entry;
protect the configuration directory and backups. This review did not establish
a supported password-free refresh-only setup flow. Account selection is still
limited to the configured Smart Plug 10A; multi-device discovery and general
reliability improvements are outside these security patches.

Baseline Bandit found one low-severity B105 match for
`CONF_PASSWORD = "password"`; this is a configuration key, not a hardcoded secret.
The scanner missed the logic defects above, demonstrating why a clean/high-level
scanner result is not a substitute for source and behavioral review.
