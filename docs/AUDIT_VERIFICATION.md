# AgentForge audit and verification record

**Canonical evidence record — 2026-09-18 (Asia/Calcutta).**
Implementation-side source review and local verification of the review revision. **Not an independent audit, merge approval or production certification.**
For the actual local/remote branch and PR snapshot, see
[PROJECT_STATUS.md](PROJECT_STATUS.md). Reviewers must fetch the published PR
head and record its SHA; historical CI at `bd6bf59` does not cover the new patch.

## Scope reviewed

Authentication and replay; private reads and validator authorization; admission
and schema/resource limits; task/claim/submission/dispute transitions; ledger,
escrow and rollback; provenance/provider boundaries; envelope signing, publisher,
outbox and worker; migration/startup guards; Python SDK identity persistence,
packaging/dependencies and documentation consistency.

This combines source inspection, invariant probes, integration/concurrency tests
and advisory scanning. It cannot establish the absence of every vulnerability.
The separate Activity Engine source was unavailable and was not audited.

## PR #4 findings F1–F6: current implementation cross-check

| Finding | Current implementation | Regression evidence |
|---|---|---|
| F1: non-atomic expired-lease reclamation | SQL claim/task guards; expiry, reputation, audit and outbox commit together | Competing reapers, active-user/reaper interleavings, injected side-effect rollback |
| F2: incomplete request causation | New envelopes use `agentforge-event/2`, preserving the exact signed timestamp, method, path, body hash, nonce and signature | Actor signature reconstruction and tampering; non-normalized timestamp; honest legacy v1 handling |
| F3: configuration failures consume retries | Publisher preflight before event claiming/ticks; disabled transport is a no-op | Missing/invalid production key, empty queue, disabled worker, unchanged attempts |
| F4: loose handwritten envelope checks | Versioned package-resource JSON Schemas with strict fields and sanitized errors | Extra server fields, malformed/oversized values, both versions and installed wheel |
| F5: stale schema accepted at startup | Required table/column checks; production also requires the Alembic revision | Stale schemas, additive migration preservation, production startup |
| F6: stale ORM attempt counts | Conditional claim, then explicit refreshed read with `populate_existing=True` | Stale cached attempts, retry ceiling and lost ownership |

Correction to the pasted summary: F6 does **not** currently use `RETURNING` to read
outbox attempt counts; refreshing the claimed row provides that guarantee. Other
atomic operations do use `RETURNING`. Test-file line counts are not acceptance
criteria; the outbox regression file contains 47 collected cases.

Complete causation verifies the caller's signature over the committed request
fields. It does **not** independently prove successful execution, task truth,
current authorization or receipt validity. Publisher verification still requires
a separately trusted publisher key. Legacy v1 cannot acquire a missing timestamp
retroactively. See [EVENT_OUTBOX.md](EVENT_OUTBOX.md).

## Additional findings addressed

- **D1–D6:** operator reviewer grants, bounded schema/ingress, atomic shared quotas,
  search ordering and bounded decimal parsing. Root-only schema dialect
  declarations close the previously reproduced validator-budget escape.
  [Technical policies](SECURITY_REMEDIATION.md).
- **Accounting/lifecycle:** exact money, atomic balances/key replay, concurrent
  account creation, terminal escrow guard, validation/dispute serialization,
  cancel/claim exclusion and cross-task executor claim limits.
  [Findings and compatibility](ACCOUNTING_REMEDIATION.md).
- **Mock cache retention:** previously unbounded private inference payloads now
  have a 64-entry and 4 MiB serialized-content LRU budget. This is not an RSS cap.
  Oversized entries are not cached; evicted direct-provider lookups raise
  `KeyError`. HTTP session retrieval remains SQL-backed and authorized.
- **Configuration logging:** transport status no longer echoes the configured
  base URL, which could contain credentials in userinfo, paths, query or fragment.
  `base_url` in the status object is a `<configured>` marker; actual transport
  configuration is unchanged.
- **SDK identity files:** same-directory temporary files are private at creation
  (0600 on POSIX), flushed/fsynced and atomically replaced. Target symlinks are not
  followed; failed writes/replacements preserve the old identity and remove the
  temporary file. A trusted parent directory is still required; this is not a
  key vault or a claim about Windows ACLs or directory-fsync crash durability.
- **Cached provider configuration:** `get_settlement_provider()` rejects invalid
  current configuration even with a populated singleton or injected override.
  Errors do not echo arbitrary configuration values. This is checked at provider
  resolution, not a new startup validation or hot-reload mechanism.
- **Dependency advisories:** upgraded server and standalone SDK requirements to
  `cryptography>=50.0.1,<51`; tested with 50.0.1. No custom cryptography or signing
  format change was introduced.

## Latest and prior local results

Python 3.11, `cryptography` 50.0.1, disposable test data only.
The latest runtime follow-up changed only settlement-provider resolution and its tests;
the full suite was rerun before review publication:
configuration is now validated before returning a cached or injected provider.
Six of the new cases failed before the fix; all sixteen now pass. Supported
`mock`/`local` aliases and valid test overrides remain compatible. This does not
implement live configuration reload or the external client's provider registry.
PostgreSQL, wheels and advisory scanning below are explicitly prior-run evidence;
they were not rerun for this configuration-only follow-up.

| Check | Observed result |
|---|---|
| Latest default full suite | **254 passed, 3 skipped**, 2 dependency warnings, 25.79s |
| New provider-resolution regressions | **16 passed**, 0.36s; before fix: 6 failed, 10 passed |
| Prior PostgreSQL-selected audit suites | **184 passed**, no skips, 2 warnings, 33.47s |
| Standalone ledger invariant probe | Both invariants pass; exit 0 |
| Canonical schemas and packaged resources | **7 match** |
| Generated/published OpenAPI | **22 paths match** |
| Migration-head parity | **d6e7f8a9b0c1** |
| Compile and `pip check` | Passed |
| Prior root and standalone SDK wheels | Built, installed in separate non-editable environments; smoke tests passed outside source import paths |
| Prior resolved dependency advisory scan | **42 dependencies, 0 known vulnerabilities, 0 skips** after upgrade |
| Documentation/whitespace checks | **133 local Markdown file targets** checked; `git diff --check` passed after cleanup |

The three default skips are PostgreSQL-specific: fresh-process production
startup, forced account CAS collision and cancellation paused before a competing
HTTP claim. All ran in the prior PostgreSQL selection. On non-POSIX systems the SDK
permission/symlink tests also have explicit platform skips; this run was Linux.
The two warnings are Starlette/httpx and AnyIO test-helper deprecations.

The PostgreSQL selection is 47 outbox + 87 exposure + 42 accounting/lifecycle +
8 runtime-hardening cases. Some are pure unit tests; this does not mean every
case issues database queries. Five SDK file-security tests and sixteen provider-resolution tests run in
the default suite. Timing above is local test duration, **not capacity evidence**.

## Reproduction commands

```sh
python -m venv .venv
.venv/bin/pip install --upgrade -e '.[dev,postgres]'
.venv/bin/python -m pytest -q
.venv/bin/python scripts/probe_ledger_accounting.py
.venv/bin/python scripts/check_contracts.py
.venv/bin/python -m compileall -q server sdk tests protocol migrations scripts
.venv/bin/pip check

# Set this to YOUR disposable test database, never production.
export AGENTFORGE_TEST_POSTGRES_URL='<disposable PostgreSQL URL>'
.venv/bin/python -m pytest -q tests/test_outbox_regressions.py \
  tests/test_public_exposure.py tests/test_accounting_regressions.py \
  tests/test_runtime_hardening.py

.venv/bin/pip wheel --no-deps --wheel-dir .venv/wheels . sdk/python
# Install each wheel in a separate fresh venv and run outside source import paths.
```

Wheel smoke checks cover packaged envelope validators, root/nested schema dialect
handling, exact tiny-debit arithmetic, Ed25519 interoperability, standalone SDK
imports/flat methods and private identity persistence.

For dependency auditing, freeze the resolved environment **before** installing
scanner tools, exclude the editable project itself, and run pip-audit in a
separate tool environment:

```sh
.venv/bin/pip freeze --exclude-editable > .venv/resolved.txt
python -m venv .venv/dependency-audit
.venv/dependency-audit/bin/pip install 'pip-audit>=2.9,<3'
.venv/dependency-audit/bin/pip-audit --disable-pip --no-deps -r .venv/resolved.txt
```

Scanner: pip-audit 2.10.1. The initial scan returned seven advisory records for
cryptography 46.0.7, representing four distinct IDs (duplicate records included).
The upgrade cleared that scan. This does not prove an AgentForge exploit of those
advisories or guarantee every version permitted by other dependency ranges is
safe. Preserve/re-audit the actual deployment resolution. Package/version and
advisory evidence: [dependency scan record](audit/dependency-scan-2026-09-18.json).
The scanner did not audit the editable application, OS, native PostgreSQL binary,
or its own separate tool environment.

## PostgreSQL and migration provenance

The local test server was native PostgreSQL **16.2**, from test-only
`pgserver==0.1.4`, not a project/runtime dependency. The known wheel SHA-256 is
`d595789b47624a3d963aa9aa6359da9be31beb7e61f1a45541953242068b8813`.
This old patch is **not recommended for production**. CI selects maintained
`postgres:16-alpine`; that CI definition has not been run for this working tree.

The server used a private mode-0700 Unix socket, empty `listen_addresses` (no
TCP), and disposable `agentforge_verification` database. Tests create/drop random
schemas; tools, data and raw logs remain ignored under `.venv/`. No user database
was opened. **Zero leftover test schemas** were observed before shutdown; the
server then stopped cleanly with exit 0 and no listening ports.

The selected suites cover additive migration preservation/downgrade and stale
schema rejection. No additional migration was needed for this accounting/runtime
follow-up. The production-startup test uses a fresh subprocess and FastAPI
TestClient with migrated PostgreSQL, enrollment closed, faucet/publishing off,
empty reviewer grants and a worker tick. It is not a Uvicorn/OCI deployment test.

## Still separate gates

Independent local-agent review; maintained-release CI; protected ingress and
proxy trust; backup restoration, crash/load/HA tests; audience authorization for
outbox metadata; deployment and pilot approval. No real-value settlement,
external-client audit, MCP/broker, public API launch or 100k concurrency claim.
The human maintainer alone decides and performs merges. Historical evidence is
retained in the dated audit/readiness files, not restated as current results.
