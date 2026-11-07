# Security patch delivery and integration verification

Review baseline: `295e1b50abb0b0cf77724400e049adbc72e2b3d9`.

## Branches

Four scoped branches are available for separate review:

- `fix/http-credential-boundaries`: HTTP redirect containment, response/identifier
  validation, timeouts and safe authentication logs. 15 standalone tests.
- `fix/mqtt-input-validation`: bounded parsing, strict enums/finite metrics,
  atomic HA-loop updates and removal of raw incoming payload logs. 18 tests.
- `fix/serialized-token-refresh`: serialized refresh/lifecycle, validated token
  rotation, executor teardown, fail-closed commands and safe errors. 19 tests.
- `fix/pin-reviewed-dependencies`: compatible exact MQTT dependency, SHA-pinned
  action wrappers, least privilege and Python regression CI. 6 tests.

`security/combined-validation` contains all four patch sets with the overlapping
HTTP/refresh implementation resolved and all tests executed together. It is the
simplest branch to review if one all-fixes change is preferred. If filing scoped
PRs instead, the independent HTTP/refresh branches overlap and need the same
resolution; do not discard the fail-closed refresh, redirect flags or validations.

No PR, main-branch merge, live deployment, device control or real-account test was
performed as part of this delivery. Branch publication alone does not imply a
successful GitHub Actions run.

## Executed integration checks

On the combined branch, a clean isolated Python 3.13 test environment used exact
requirements from `requirements-test.txt`: aiohttp 3.14.5, paho-mqtt 2.1.0 and
voluptuous 0.16.0. The unittest command imported actual integration source with
HA stubs and fake MQTT transports; HTTP redirect tests used local loopback
servers. **58 tests passed** with all patches applied together.

```sh
python -m pip install --index-url https://pypi.org/simple -r requirements-test.txt
python -m unittest discover -s tests -v
python -m compileall -q custom_components tests
python -m pip check
git diff main --check
```

The reviewed resolved Python test dependency closure had no known pip-audit
matches at the time of checking. This is not a complete audit of HA's dependency
closure and is not a guarantee of package safety.

## Remaining limitations

- No deliberate malware was found in reviewed source/history. This is not an
  absolute assurance about the cloud service, dependencies, binaries or future
  updates. All six historical Python bytecode files matched compilation of their
  corresponding source without executing their code.
- Current baseline MQTT TLS already verified certificates/hostnames; the old
  CERT_NONE setting was historical and already fixed.
- Real Qubo response compatibility and full Home Assistant behavior remain
  untested. Synthetic tests cannot establish that a reverse-engineered cloud API
  remains stable. Protect HA configuration/backups because the integration still
  stores the account password intentionally.
- Action wrappers are pinned but their hassfest/HACS containers remain mutable.
  npm lockfile advisory matches were found in official action dependencies;
  runtime reachability was not established, and checking newer official major
  releases did not produce an advisory-free closure. See
  `security-supplychain.md` and `security-review-evidence.json`. These upstream
  risks remain unresolved; the patch does not claim clean action runtimes.
- A 64 KiB MQTT application limit does not bound Paho's earlier wire-frame
  allocation or impose a valid-message rate limiter.

See `security-review.md` for the audit and the per-area documents for threat
models, code changes, test coverage and qualifications.
