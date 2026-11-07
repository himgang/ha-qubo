# Dependency and workflow review

Reviewed on 2026-11-07 UTC against main `295e1b50abb0b0cf77724400e049adbc72e2b3d9`.
This is bounded supply-chain hardening, **not** an assertion of complete provenance,
absence of malicious code, or vulnerability-free action runtimes.

## Python requirements

- Pin the integration requirement to `paho-mqtt==2.1.0`. The existing hub uses
  `mqtt.CallbackAPIVersion.VERSION1`, an API introduced in Paho 2.x; the old
  `>=1.6.1` constraint permitted an incompatible version. The official
  `eclipse-paho/paho.mqtt.python` release `v2.1.0` was published 2024-04-29.
- CI runs CPython 3.13 with real `paho-mqtt==2.1.0`, `voluptuous==0.16.0`, and
  `aiohttp==3.14.5`. PyPI requires Python >=3.7, >=3.9 and >=3.10 respectively;
  a fresh CPython 3.13.5 environment installed these successfully.
- Do **not** use the originally proposed `aiohttp==3.13.3`: its version-specific
  PyPI metadata lists advisories, including GHSA-p998-jp59-783m (fixed 3.13.4),
  GHSA-jg22-mg44-37j8 (fixed 3.14.0), and GHSA-5wr8-gcqf-gqf3 (fixed 3.14.4).
  PyPI's current stable version was 3.14.5. Its version-specific PyPI advisory
  list and an OSV exact-version query were empty at review time.
- OSV exact-version queries for Paho 2.1.0 and voluptuous 0.16.0 were also empty.
  `pip-audit` against Paho alone returned exit 0, `No known vulnerabilities
  found`; a second audit of the **entire installed test dependency closure**
  returned the same result. These are database results, not proof of safety.
- Top-level test dependencies are exact pins, but transitive dependencies are
  resolved by pip and are not hash-locked. No claim of reproducible wheel builds
  or verification of all package source code is made.

## Action identity and limited static review

The GitHub REST commit/ref APIs resolved the following refs in their expected
upstream repositories. The full SHAs, not moving names, are used in workflows:

| Repository/action | Reviewed ref | Commit |
| --- | --- | --- |
| `actions/checkout` | `v4`, confirmed `v4.4.0` tag | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/setup-python` | `v6`, confirmed `v6.3.0` tag | `ece7cb06caefa5fff74198d8649806c4678c61a1` |
| `home-assistant/actions/hassfest` | `home-assistant/actions` master | `86d4564712fa02543c808e3a693dd04f9d3f8361` |
| `hacs/action` | main | `1ebf01c408f29afcb6406bd431bc98fd8cbb15aa` |

Review was static only: selected action descriptors; checkout's
`src/git-auth-helper.ts`; setup-python's `src/install-python.ts`; both actions'
`package.json` and `package-lock.json`; and the HACS/hassfest repository trees.
No downloaded action code, Node bundle, container, HA installation, or device
connection was executed. Upstream commit/ref APIs verify repository association,
not independent signatures or source-to-bundle reproducibility.

Important residual risks:

- Checkout executes a committed Node 20 bundle; setup-python executes a committed
  Node 24 bundle. Their bundled JS was not exhaustively reviewed or reproduced.
  Setup-python can download Python releases using the moving
  `actions/python-versions/main` manifest and run their installation scripts.
- OSV queries of 88 production lockfile entries across the two actions found
  advisory matches in checkout's `uuid`, `@fastify/busboy`, `undici`, and
  setup-python's `brace-expansion`, `undici`. Exact versions and advisory IDs
  are recorded in `security-review-evidence.json`. These are **not** clean
  dependency audits. Reachability through the shipped bundles was not established.
  For example, checkout's reviewed auth helper imports UUID v4, whereas the
  reported UUID advisory concerns v3/v5/v6 output buffers. That observation does
  not exonerate the entire bundle or its other dependencies.
- The pinned hassfest wrapper invokes **mutable**
  `ghcr.io/home-assistant/hassfest` with the workspace mounted into Docker.
  The pinned HACS wrapper selects **mutable** `ghcr.io/hacs/action:main`.
  Pinning these wrappers does not pin or audit the images, their source, or
  dependencies. Image digest pinning and image provenance remain future work.
- A follow-up comparison resolved newer official releases checkout `v7.0.1`
  (`3d3c42e5aac5ba805825da76410c181273ba90b1`) and setup-python `v7.1.0`
  (`6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`). Exact-version OSV queries
  against their production lockfiles still returned advisory matches (undici
  6.27.0 for checkout; minimatch 10.2.5 and undici 6.29.0 for setup-python).
  A major-version update alone did not establish a clean dependency closure;
  this bounded patch retains the reviewed versions above and explicitly leaves
  action-runtime reachability/remediation as an unresolved upstream risk.
- HACS keeps `permissions: {}`; `comment: "false"` disables its advertised
  optional PR-comment write behavior. The selected descriptor accepts a
  GitHub token and repository input, and this workflow validates the public
  repository without a checkout. Restricted-token behavior was reviewed only
  statically, not confirmed by a hosted Actions run or executing its container.
- Hassfest and unittest CI use `contents: read`. Checkout credentials are not
  persisted. The new test workflow handles ordinary `pull_request`/`push`, not
  `pull_request_target`, and neither uploads reports nor invokes an external
  audit/report action. PyPI config and extra-index environment overrides are
  disabled explicitly: a local initial install exposed a system-wide piwheels
  extra index, so validation was repeated using PyPI alone.

## Verification performed

Run from the worktree with the isolated Python 3.13 environment:

```sh
VENV=/home/himgang/.hermes/cache/scratch/ha-qubo-supplychain-venv
python3.13 -m venv "$VENV"
PIP_CONFIG_FILE=/dev/null PIP_EXTRA_INDEX_URL= "$VENV/bin/python" -m pip install \
  --index-url https://pypi.org/simple --only-binary=:all: --no-cache-dir \
  --force-reinstall -r requirements-test.txt
"$VENV/bin/python" -m pip check
"$VENV/bin/python" -m unittest discover -s tests -v
"$VENV/bin/python" -m compileall -q custom_components tests
git diff --check
```

Results: `No broken requirements found`; **6 tests passed**; compileall and
whitespace checks exited 0. The Paho test imports the real installed library
and constructs a VERSION2 client without connecting or starting a network loop.
Existing integration code's VERSION1 callback API remains supported by 2.1.0;
this change does not migrate callback signatures. Baseline had no tests; the new
standalone filename can coexist with separately developed security tests.

All three workflow files parsed successfully using PyYAML 6.0.3 `BaseLoader`
(avoiding YAML 1.1's `on` boolean conversion), with assertions on triggers,
permissions, HACS comments and checkout credential persistence. This validates
YAML structure, not GitHub's complete workflow schema or hosted execution.

Audit commands (the audit environment is separate from test dependencies):

```sh
AUDIT=/home/himgang/.hermes/cache/scratch/ha-qubo-audit-venv/bin/pip-audit
# paho-requirement.txt contains only paho-mqtt==2.1.0.
PIP_CONFIG_FILE=/dev/null PIP_EXTRA_INDEX_URL= "$AUDIT" --no-deps --disable-pip \
  -r ../ha-qubo-supplychain-evidence/paho-requirement.txt --progress-spinner off --format json
"$VENV/bin/python" -m pip freeze > ../ha-qubo-supplychain-evidence/resolved-test-requirements.txt
PIP_CONFIG_FILE=/dev/null PIP_EXTRA_INDEX_URL= "$AUDIT" --no-deps --disable-pip \
  -r ../ha-qubo-supplychain-evidence/resolved-test-requirements.txt --progress-spinner off --format json
```

Both audits exited 0; pip-audit warned that fully hash-pinned requirements are
preferable to `--no-deps`. The second command explicitly supplies every installed
package from `pip freeze`, rather than assuming the top-level pins cover transitives.

## Official verification endpoints

- GitHub: `https://api.github.com/repos/{owner}/{repo}/commits/{ref}` and
  `/git/ref/tags/{tag}` for the repositories and refs above.
- Paho release: `https://api.github.com/repos/eclipse-paho/paho.mqtt.python/releases/tags/v2.1.0`.
- PyPI: `https://pypi.org/pypi/{package}/{version}/json`; latest aiohttp metadata:
  `https://pypi.org/pypi/aiohttp/json`.
- OSV: `POST https://api.osv.dev/v1/query` with exact PyPI package/version;
  `POST https://api.osv.dev/v1/querybatch` with npm lockfile package/versions.

The adjacent JSON records selected review results without credentials. Raw
retrieved metadata and lockfiles remain in the local scratch evidence directory;
network reads alone are not an enduring attestation of those upstream artifacts.
