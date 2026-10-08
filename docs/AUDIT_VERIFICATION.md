# AgentForge audit and verification record

**Canonical evidence record — updated 2026-10-09 (Asia/Calcutta).**
Implementation-side source review and local verification of the review revision. **Not an independent audit, merge approval or production certification.**
For the actual local/remote branch and PR snapshot, see
[PROJECT_STATUS.md](PROJECT_STATUS.md). Reviewers must fetch the published PR
head and record its SHA; historical CI at `bd6bf59` does not cover the new patch.

## Phase 2.5 — current implementation-side verification

Scope: dormant chain-agnostic settlement commands and attempt reconciliation,
not a chain adapter or external provider. Architecture, state transitions,
trusted-port assumptions, activation and rollback boundaries are in
[settlement attempts](SETTLEMENT_ATTEMPTS.md). Python 3.11, Linux, disposable data.

Before editing, `git fetch origin`, `gh pr view 14` and `git log origin/main`
confirmed PR #14 merged at `38eda8073439b3dd5b31843d111afbc3c7867a38` and the
assigned branch started there. The fresh default suite measured **417 passed,
3 skipped**, not the older checked-in 416 snapshot. Contracts at that baseline
were 7 schemas / 23 paths / migration `b0c9d8e7f6a5`.

| Command/check | Measured result |
|---|---|
| `.venv/bin/python -m pytest -q` | **489 passed, 3 skipped**, 1 Starlette/httpx deprecation warning, 94.69s |
| `.venv/bin/python -m pytest -q tests/test_settlement_attempts.py` | **72 passed**, 1 warning, 14.17s |
| PostgreSQL runner below: new suite + existing CI-selected audit suites | **267 passed, 0 skipped**, 1 warning, 75.00s; PostgreSQL 16.2, Unix socket only; **0 leftover test schemas**; server stopped afterwards |
| `.venv/bin/python scripts/check_contracts.py` | `SCHEMAS_OK: 7; packaged resources match`, `OPENAPI_MATCH: 23 paths`, `MIGRATION_HEAD_MATCH: c1d2e3f4a5b6` |
| `.venv/bin/python -m compileall -q server sdk tests examples protocol migrations scripts` | Exit 0 |
| `.venv/bin/pip check` | `No broken requirements found.` |
| Migration regression in the new suite, on both database dialects | Upgrade head; live HTTP task/funding + enqueue; metadata/index parity; downgrade to prior head preserving task/escrow; stale-schema startup refusal; re-upgrade head |
| Both non-editable wheels, commands below | `ROOT_WHEEL_SMOKE_OK`, `SDK_WHEEL_SMOKE_OK`; both `agentforge-cli --version` → `agentforge-cli 0.1.0`; both `--help` exit 0 |
| Live CI on implementation commit `855992ef5f5ae1c4cad0114512139958f831cd54` | [Run 37842401207](https://github.com/CryptoExplor/agentforge/actions/runs/37842401207): `test`, `postgres-audit-regressions`, `dependency-audit` all **success**; verified with `gh run view 37842401207 --json conclusion,headSha,url,jobs`. CI job outcomes are verified; raw log download was blocked by this sandbox's outbound host restrictions, so the measured counts/timings above are explicitly local. |
| `git diff --cached --check` before implementation/publication commits | Exit 0 |
| Existing tests and public contracts | No existing test changed; no change to `protocol/`, SDK, routes, signing or mock settlement provider |

### New coverage and trust limits

Live app registration, signed task creation/funding and deterministic mock payout
remain exercised, with **no HTTP-layer mocks**. Test-only injected producers prove
that the actual task route commits or rolls back the intent, ledger and task
together; production mock never enqueues. SQL concurrency tests exercise duplicate
producer keys, conflicting terminal decisions, competing worker leases and stale
completion fences (including expiry while acquiring a database lock).

Fault tests cover rollback of a real journal insert, crash before/after a send,
submission timeout, unknown and unavailable receipts, subsequent read-only
recovery, finality depth, wrong intent/domain/asset/party/amount/fee/transaction
binding, malformed observations, replacement cycles, independent successor
verification, deep reorg observation and sanitized errors. Session/pool tracking
asserts that both I/O ports run with **zero worker sessions/connections open**;
a real HTTP read and independent SQL writer succeed inside each port callback.

The two simulated ports are test-only, not external-network evidence. No concrete
receipt verifier ships: the type boundary and core commitment checks do not
replace future authentication of actual receipts. No ledger callback applies
external finality, no retry/reset/force-finalize interface exists, and no chain
provider can be selected. Ambiguous sends may remain unresolved indefinitely;
this is the deliberate safety trade-off, not demonstrated liveness. No load,
chaos-deployment, pilot or independent audit claim is made.

### Local PostgreSQL reproduction and provenance

The CI PostgreSQL job now includes the new suite against maintained
`postgres:16-alpine`. Local evidence instead uses test-only `pgserver==0.1.4`
(native PostgreSQL **16.2**, not a deployment recommendation or project dependency)
with the existing per-test random-schema fixture. No TCP listener is opened.
The ignored runner used for the recorded command is reproduced here:

```python
# Save as .venv/run_pg_tests.py after: .venv/bin/pip install pgserver==0.1.4
from pathlib import Path
import os
import subprocess
import sys
import pgserver
from sqlalchemy import create_engine, text

folder = Path('.venv/pg-attempt-tests').resolve()
folder.mkdir(mode=0o700, exist_ok=True)
with pgserver.get_server(folder, cleanup_mode='stop') as server:
    url = server.get_uri().replace('postgresql://', 'postgresql+psycopg://', 1)
    engine = create_engine(url)
    with engine.connect() as conn:
        print('PostgreSQL:', conn.scalar(text('SHOW server_version')), flush=True)
        print('Listen addresses:', repr(conn.scalar(text('SHOW listen_addresses'))), flush=True)
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', *sys.argv[1:]],
                            env={**os.environ, 'AGENTFORGE_TEST_POSTGRES_URL': url})
    with engine.connect() as conn:
        print('Remaining test schemas:', conn.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'agentforge_test_%'")), flush=True)
    engine.dispose()
print('PostgreSQL stopped', flush=True)
raise SystemExit(result.returncode)
```

```sh
.venv/bin/python .venv/run_pg_tests.py tests/test_settlement_attempts.py tests/test_outbox_regressions.py tests/test_public_exposure.py tests/test_accounting_regressions.py tests/test_runtime_hardening.py
```

### Installed-wheel reproduction

Built both wheels, installed into **separate fresh non-editable environments**,
then ran from `/home/user`, outside the checkout and with `PYTHONPATH` removed:

```sh
.venv/bin/pip wheel --no-deps --wheel-dir .venv/wheels . sdk/python
python -m venv .venv/root-wheel
python -m venv .venv/sdk-wheel
.venv/root-wheel/bin/pip install .venv/wheels/agentforge-*.whl
.venv/sdk-wheel/bin/pip install .venv/wheels/agentforge_sdk-*.whl
cd /home/user
env -u PYTHONPATH /home/user/agentforge/.venv/root-wheel/bin/python -c 'from agentforge_server.event_envelope import _VALIDATORS; assert len(_VALIDATORS) == 2; from agentforge_server.validators.result_schema import validate_result_schema; assert validate_result_schema({}, True) == (True, ""); from agentforge_server.admission import consume; from agentforge_server.settlement_attempts import IntentSpec, State; from agentforge_server.settlement_worker import SettlementWorker; from agentforge_server.models import Base; from sqlalchemy import create_engine, inspect; engine = create_engine("sqlite://"); Base.metadata.create_all(engine); assert set(("settlement_intents", "settlement_attempts", "settlement_attempt_events")) <= set(inspect(engine).get_table_names()); from agentforge_cli.main import main; import agentforge_server; assert "site-packages" in agentforge_server.__file__; print("ROOT_WHEEL_SMOKE_OK")'
env -u PYTHONPATH /home/user/agentforge/.venv/sdk-wheel/bin/python -c 'from agentforge_sdk import AgentForgeClient, AgentIdentity; from agentforge_sdk.client import AgentIdentity as Legacy; assert AgentIdentity is Legacy; from agentforge_cli.main import main; from importlib.util import find_spec; assert find_spec("agentforge_server") is None; import agentforge_sdk; assert "site-packages" in agentforge_sdk.__file__; print("SDK_WHEEL_SMOKE_OK")'
/home/user/agentforge/.venv/root-wheel/bin/agentforge-cli --version
/home/user/agentforge/.venv/root-wheel/bin/agentforge-cli --help > /dev/null
/home/user/agentforge/.venv/sdk-wheel/bin/agentforge-cli --version
/home/user/agentforge/.venv/sdk-wheel/bin/agentforge-cli --help > /dev/null
```

CI now also builds/smokes the standalone SDK wheel (previously only the root
wheel), checks the new installed server modules and runs the new suite in its
PostgreSQL job. Local runs above do not substitute for the maintained-image CI
or the independent auditor. Earlier sections below are historical evidence.

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
| Migration-head parity | **e7f8a9b0c1d2** (Phase 1.1); prior row: `d6e7f8a9b0c1` |
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

## Phase 1.1 verification: task verification strategies

Follow-up revision on `arena/01a0cee1-agentforge`, based on the PR #5 state
(`82efa6f769010ddc7067324ab9942cd2b98f991d`). Scope: `verification_strategy` on
tasks, deterministic verification and settlement in the submission transaction,
and the competing-validator guard. Python 3.11, disposable test data only.

| Check | Observed result |
|---|---|
| Full default suite | **267 passed, 3 skipped**, 1 dependency warning, 26.35s |
| Pre-existing suite (before this change) | 254 passed, 3 skipped — unchanged, no test was modified to fit the feature |
| `tests/test_deterministic_settlement.py` | **13 passed**, 3.88s |
| Compile (`server sdk tests examples protocol`) | Passed |
| `scripts/check_contracts.py` | `SCHEMAS_OK: 7` (packaged resources match), `OPENAPI_MATCH: 22 paths`, `MIGRATION_HEAD_MATCH: e7f8a9b0c1d2` |
| Alembic SQLite `upgrade head → downgrade base → upgrade head` | `e7f8a9b0c1d2 (head)` |
| Alembic `upgrade --sql` against `postgresql+psycopg` | `ALTER TABLE tasks ADD COLUMN verification_strategy VARCHAR(32) DEFAULT 'peer_review' NOT NULL;` (no SQLite-specific SQL) |
| `worker --once` | one tick, clean exit |
| `git diff --check` | Clean |
| CI on this branch | `test`, `postgres-audit-regressions` and `dependency-audit` all **success** ([run `35885694231`](https://github.com/CryptoExplor/agentforge/actions/runs/35885694231) at head `0552b1f`; later documentation-only commits re-run the same jobs) |

Covered cases: immediate `VERIFIED` with escrow `RELEASED` on a valid proof; immediate
`REJECTED` with a full `REFUND` and no executor payout on a mismatched or
schema-invalid result; `peer_review`/`operator` still requiring the signed
validation endpoint; `409 task has already settled via deterministic strategy` for a
competing validator on a settled (verified or rejected) task with no re-settlement;
escrow conservation and single settlement; attached inference-session receipts;
`REPUTATION` tasks without escrow; tampered proof signatures still refused with `401`;
and the documented `TaskResponse` representation staying in step with the API.

The CI PostgreSQL job is the authoritative migration execution: it runs against
`postgres:16-alpine` with `AGENTFORGE_TEST_POSTGRES_URL` set, and the selected suites
call `alembic upgrade head`, `verify_schema(require_migrations=True)` (which requires
the alembic revision to equal `e7f8a9b0c1d2` exactly) and an additive-migration
downgrade/upgrade preservation check, so that head was reached on real PostgreSQL.
No PostgreSQL server was available in this sandbox, so locally the new migration was
verified by SQLite upgrade/downgrade and by PostgreSQL offline SQL generation only,
and the live PostgreSQL run above is result evidence from CI, not a local run.
This is implementation-side evidence, not independent review or merge approval.

## Phase 1.4 verification: server timestamps and clock-drift defence

Scope: authoritative monotonic-anchored server time, `received_at` on claims and
submissions, the tightened 60-second signed-request drift window, lease anchoring,
submission-deadline and dispute-window decisions on database server time, and the
fail-closed clock-divergence guard. Python 3.11, disposable test data only, run on
both SQLite and native PostgreSQL 16.2.

| Check | Observed result |
|---|---|
| Full default suite (SQLite) | **367 passed, 3 skipped**, 1 dependency warning, 64.19s |
| Pre-existing suite (before this change) | **313 passed, 3 skipped** — unchanged; no existing test was modified to fit the feature |
| Repeated full-suite runs (stability) | 3 consecutive runs green (360/360/367 as tests were added), 0 failures |
| `tests/test_clock_drift.py` (SQLite) | **54 passed**, 17.27s |
| **PostgreSQL 16.2**: CI-selected suites + `tests/test_clock_drift.py` | **249 passed, 0 skipped**, 49.74s — the three PostgreSQL-only cases that skip on SQLite ran, and the new clock suite passed on PostgreSQL |
| **PostgreSQL 16.2**: database server clock | `EXTRACT(epoch FROM now())` renders as expected and returns a `Decimal` the reader converts; host/database delta 0.052 s, inside the 5 s tolerance |
| **PostgreSQL 16.2**: Alembic `upgrade head → downgrade → upgrade head` with live rows | head `b0c9d8e7f6a5`; backfill claim `4242.25 → 4242.25`, submission `777.5 → 777.5`; downgrade drops both columns and preserves the rows; `verify_schema(require_migrations=True)` OK |
| `scripts/check_contracts.py` | `SCHEMAS_OK: 7` (packaged resources match), `OPENAPI_MATCH: 23 paths`, `MIGRATION_HEAD_MATCH: b0c9d8e7f6a5` |
| Alembic SQLite `upgrade head → downgrade base → upgrade head` | `b0c9d8e7f6a5 (head)` |
| Alembic `upgrade --sql` against `postgresql+psycopg` | `ALTER TABLE claims ADD COLUMN received_at FLOAT DEFAULT '0' NOT NULL;` and the same for `submissions`, plus the `created_at` backfill (no SQLite-specific SQL) |
| Migration backfill on pre-existing rows | legacy claim `created_at=4242.25 → received_at=4242.25`; submission `777.5 → 777.5`; downgrade drops both columns and preserves the rows |
| Compile (`server sdk tests examples protocol scripts migrations`) | Passed |
| `git diff --check` | Clean |

Covered cases: rejection beyond ±60 s in both directions and acceptance inside it;
the configurable window; a futuristic timestamp earning no lease; unparseable
timestamps; lease anchoring for claim, heartbeat and inference under fast, slow and
jumping client clocks; non-accumulation across repeated and concurrent heartbeats;
reaping and the lost heartbeat race; the monotonic clock under load, under a
backwards host step and after a simulated suspend; database-clock agreement and
fail-closed divergence (submission **and** heartbeat both refuse with `503`);
deadline-free tasks never reading the database clock; submission `received_at`
versus a declared `created_at`; the deterministic `SUBMISSION_BEFORE_DEADLINE`
check using `received_at`; the dispute window opening and closing on server time;
startup rejection of an unsafe clock configuration; and the SDK learning the
server clock from `X-Server-Timestamp` while ignoring an unusable header.

Two limitations were found and recorded rather than hidden. First, HTTP write
races are limited to two concurrent writers because SQLite — the development and
test default — has a single writer: a wider field raises `database is locked` on
the **unmodified baseline** as well (reproduced at `6e5de02` with six racers), so
this is not a regression from this change, and PostgreSQL enforces the same SQL
claim guard for a wider field. Second, two independent reads of the database clock
can differ by up to ~1 ms, so clock-equality assertions use a realistic tolerance
rather than exact equality.

### PostgreSQL provenance for this phase

The local PostgreSQL server was native **16.2** from test-only `pgserver==0.1.4`
(the same approach recorded in
[PostgreSQL and migration provenance](#postgresql-and-migration-provenance)), not
a project or runtime dependency, with `psycopg[binary]==3.3.6` as the driver. It
used a private Unix socket under `/tmp`, a disposable data directory, and the
repository's existing random-schema isolation fixture; the server stopped cleanly
afterwards. This is not the maintained `postgres:16-alpine` image CI selects, and
it is **not recommended for production**.

Unlike the Phase 1.1 record, the PostgreSQL path here was executed locally rather
than only inferred from offline SQL generation: the dialect-specific
`database_now()` expression, the fail-closed clock-divergence guard, the
submission-deadline and dispute-window decisions and the migration round trip all
ran against a real server. CI remains the authoritative gate for the maintained
image.

This is implementation-side evidence, not independent review, merge approval or a
capacity measurement.

## Phase 1.5 verification: modular domain routers

Scope: behaviour-preserving decomposition of the former monolithic `app.py`
(≈1,821 lines) into a 103-line composition root plus seven domain routers under
`server/agentforge_server/routes/` (`agents`, `tasks`, `claims`, `submissions`,
`validations`, `disputes`, `system`) with cross-domain plumbing in
`routes/_shared.py`. **No API surface changed:** every route URL, request schema,
response envelope, error code and `operationId` is byte-identical, and mount
order is fixed once in `routes/__init__.py` (the load-bearing
`agents/search`-before-`agents/{did}` pair stays inside `agents.py`). The five
monkeypatch-surface names (`MAX_LIST_BYTES`, `can_execute`, `guard_active_claim`,
`guard_pending_submission`, `queue_outbox`) are read through the `app` module at
call time via `routes/_shared.py`. Python 3.11, disposable test data only.

| Check | Observed result |
|---|---|
| Full default suite (SQLite) | **369 passed, 3 skipped**, 1 dependency warning |
| No test modified to fit the change | Confirmed — the router split preserved every existing assertion |
| `scripts/check_contracts.py` | `SCHEMAS_OK: 7` (packaged resources match), `OPENAPI_MATCH: 23 paths`, `MIGRATION_HEAD_MATCH: b0c9d8e7f6a5` |
| OpenAPI parity across the split | 23 path items / 24 operations (`GET`+`POST /api/v1/tasks` share a path) plus the unschematized `GET /`; identical to pre-split |
| Compile (`server sdk tests examples protocol`) | Passed |
| Alembic head (unchanged by this phase) | `b0c9d8e7f6a5` |

Note: the migration head is unchanged from Phase 1.4 — the decomposition touches
only code layout, not the schema. Earlier rows in this file that report 254/267/367
passing tests, 22 OpenAPI paths, or Alembic heads `e7f8a9b0c1d2` / `c4d5e6f7a8b9`
are **superseded historical evidence** from the phase they were captured in; the
Phase 2.1 section below is also historical; the current measured baseline is
the Phase 2.5 section above.

This is implementation-side evidence, not independent review or merge approval.

## Phase 2.1 verification: modular SDK and full API parity

Scope: behaviour-preserving decomposition of the monolithic
`sdk/python/agentforge_sdk/client.py` into `identity.py`, `errors.py` and
`transport.py` behind an unchanged `client.py` facade, plus flat-method
wrappers for the five live routes that lacked them
(`GET /api/v1/capabilities`, `GET /api/v1/agents/search`,
`POST /api/v1/tasks/{task_id}/cancel`,
`POST /api/v1/tasks/{task_id}/validations`,
`GET /api/v1/inference/{session_id}`) and cursor/offset forwarding on
`list_tasks`. **No server code, route, schema or migration changed**, and no
existing test was modified: both legacy import paths
(`agentforge_sdk`, `agentforge_sdk.client`) expose the same objects, the
`os`/`tempfile` facade patch surface used by the security suite is preserved,
signing bytes and key formats are byte-identical, and `AgentForgeError`
messages keep the exact `HTTP <status>: <detail>` format (the structured
subclasses only add classification and `status_code`/`detail` attributes).
New `tests/test_sdk.py` routes the SDK's own transport through the live
TestClient and exercises every `AgentForgeClient`/`AgentIdentity` method,
including keyset/offset pagination, every `list_tasks` filter, task-scoped
and submission-scoped validation, disputes, cancellation, structured error
mapping (401/404/409, idempotency reuse, replay) and the clock-drift
self-correction loop driven by `X-Server-Timestamp`. Python 3.11, disposable
test data only.

| Check | Observed result |
|---|---|
| Full default suite (SQLite) | **388 passed, 3 skipped** (369 prior + 19 new, 0 modified), 1 dependency warning |
| `scripts/check_contracts.py` | `SCHEMAS_OK: 7` (packaged resources match), `OPENAPI_MATCH: 23 paths`, `MIGRATION_HEAD_MATCH: b0c9d8e7f6a5` |
| Compile (`server sdk tests examples protocol migrations scripts`) | Passed |
| `pip check` | No broken requirements |
| Root and standalone wheels | Built with `pip wheel --no-deps . sdk/python`; both contain `identity.py`, `errors.py`, `transport.py`, `client.py`, `crypto.py`, `__init__.py` and nothing else new — no subpackages were created, so neither `pyproject.toml` package list changed |
| Installed-wheel smoke (separate fresh venvs, outside source import paths) | Legacy imports from `agentforge_sdk` and `agentforge_sdk.client` resolve to the same objects; structured errors subclass `AgentForgeError`; `error_for` maps live detail strings; every new method present on the facade |
| Standalone SDK dependencies | Unchanged (`httpx`, `cryptography` only) |

This is implementation-side evidence, not independent review or merge approval.

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
`postgres:16-alpine`; for the Phase 1.1 head that CI PostgreSQL job ran green (see the
Phase 1.1 section), while the earlier revision's CI state is unchanged.

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
