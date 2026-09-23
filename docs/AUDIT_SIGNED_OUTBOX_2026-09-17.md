# Signed-event outbox audit — 2026-09-17

> **Historical evidence.** Current status and test results live in
> [PROJECT_STATUS.md](PROJECT_STATUS.md) and [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md).
> The checkpoint counts, commit IDs and CI runs below are not current-worktree approval.

## Historical remediation checkpoint

The findings below are the historical audit of `3986dd1`, not the current
verification result. Their original seven probes have moved from `audits/` to
`tests/test_outbox_regressions.py` and now pass in default discovery. The
remediation includes atomic claim expiry/claim-use guards, v2 complete causation
with legacy v1 verification, schema-backed runtime validation, publisher
preflight, fresh retry state, and table/column/migration readiness checks.

An additional submission regression fixes a missing proof commitment: the
outbox now uses `submission.proof_hash` rather than reading a nonexistent hash
from the proof object. No full proof body is published.

Additional tests cover tampering, legacy compatibility, configuration errors,
owner loss, shutdown between deliveries, data-preserving migrations and
heartbeat/submission/reaper interleavings. The remediation checkpoint then had **99
passing tests** (2 dependency deprecation warnings). Contracts and packaging
checks are recorded in `scripts/check_contracts.py` and CI.

**GitHub Actions verified remediation at `fd165b2`:**
[CI run 35252261324](https://github.com/CryptoExplor/agentforge/actions/runs/35252261324)
passed both `test` (default suite, contracts and installed-wheel resources) and
`postgres-audit-regressions` (the 47-test regression file against PostgreSQL 16,
with an isolated schema per test). Local PostgreSQL installation was blocked by
unreachable package repositories; the PostgreSQL evidence is this CI run, not
an inference from SQLite. Docker Compose itself was not executed.

The separate Vercel deployment check was failing when the PR checks were
inspected; this remediation does not claim to resolve that deployment issue.
No independent audit or merge approval is implied. The local auditing agent
should review the implementation and rerun verification before the human
maintainer decides either PR's disposition.

## Scope and disposition

Audited checkout: `3986dd16d640830930c8a80a1f5336e78c023f6e`, on
`arena/01a0af8f-agentforge`. Scope: signed envelopes, publisher configuration,
outbox delivery, request attribution, worker/reaper integration, schema startup,
migrations, tests, compose configuration, and related handoff documentation.
This is not an exhaustive audit of every marketplace or settlement path.

**Findings: six reproduced code issues (seven failing invariant probes), plus a
documentation/history inconsistency.** Three code findings are high priority.
The passing baseline suite is not evidence that these paths are correct.

No application code was changed. No commit, push, PR action, merge, revert, or
approval was performed. Merge authority remains with the human maintainer.
This report is not a merge approval.

## Verification performed

The system Python initially lacked pytest. Installed the declared development
and PostgreSQL-driver dependencies into ignored `.venv/`, using Python 3.11.2.
No real signing keys, external publication endpoint, or application database was
used; migration and test databases were disposable.

| Check | Observed result |
|---|---|
| `.venv/bin/python -m pytest -q` | **52 passed**, 2 deprecation warnings |
| `.venv/bin/python -m compileall -q server sdk tests examples` | Passed |
| Draft 2020-12 schema meta-validation | **6/6 valid** |
| Generated OpenAPI versus `protocol/v1/openapi.json` | **Exact match**, 22 paths |
| SQLite Alembic `upgrade head -> downgrade base -> upgrade head` | Passed, `c4d5e6f7a8b9` |
| Worker `--once`, migrated disposable SQLite DB, transport disabled | Clean exit |
| `.venv/bin/python -m pytest -q audits/test_signed_outbox_findings.py --tb=short` | **7 failed**, each exposes a finding below |
| `git diff --check` | Passed |

The audit probes assert desired behavior and deliberately fail on this checkout.
They are outside the configured default `testpaths = ["tests"]`, are not marked
xfail, and must be invoked explicitly. Move corrected regression tests into the
normal suite when implementing fixes. They use local test identities, mock
adapters, real SQLite sessions, and no remote network.

PostgreSQL execution and Docker Compose startup were **not tested**: neither
`psql` nor Docker is installed. Installing the psycopg driver is not PostgreSQL
integration coverage. Live GitHub PR/CI status was not queried.

## F1 — High: concurrent reapers duplicate reputation penalties and events

**Locations:** `server/agentforge_server/services.py:124-173`;
`server/agentforge_server/worker.py:44-50`;
`server/agentforge_server/app.py:192-193`.

The reaper selects ACTIVE expired claims, reads each task, then updates ORM
objects and inserts reputation/audit/outbox rows without a conditional ownership
transition or row locking. The worker and API authentication both invoke it.
Two sessions can read the same ACTIVE claim and CLAIMED task before either
commits, and both perform the expiration side effects.

**Reproduction:** `test_a5_two_reapers_penalize_an_expired_claim_only_once` uses
two real sessions synchronized after reading the task. Observed:

```text
(reaped count, timeout penalties, CLAIM_EXPIRED outbox events)
expected: (1, 1, 1)
actual:   (2, 2, 2)
```

The executor receives -0.2 instead of -0.1 for one expiration. The two outbox
rows have distinct event IDs, so receiver deduplication cannot repair this.
This is a latent service-layer race made especially relevant by running the
worker alongside the API; it is not solely a new envelope-code defect.

**Recommended fix:** atomically win the claim/task transition with conditional
updates (or correctly ordered row locks on supported databases), and emit all
side effects only for the winning transition in the same transaction. Recheck
lease and task state under that protection, including races with heartbeat and
submission. Test simultaneous API/worker and worker/worker execution on SQLite
and PostgreSQL.

## F2 — High: actor signature cannot be reconstructed from the envelope alone

**Locations:** `server/agentforge_server/app.py:169-190`;
`server/agentforge_server/crypto.py:105-117`;
`tests/test_signed_event_outbox.py:335-368`.

Request signing includes the exact timestamp header string:

```text
METHOD\nPATH\nBODY_HASH\nTIMESTAMP\nNONCE
```

Causation records all of those components except TIMESTAMP. The event's
`occurred_at` is a different server-generated time, not the original signed
string. Timestamp headers may also contain fractional or other valid numeric
representations, so substituting an event time is not a reliable workaround.

**Reproduction:** `test_a1_causation_preserves_exact_signed_timestamp`: a real
signed task request produces causation with no original request timestamp.
The existing verification test masks this by reading `sent["timestamp"]` from
the test client's retained request, rather than from the envelope.

**Impact:** the advertised independently verifiable actor attribution is
incomplete; a receiver must obtain missing request data or trust the publisher's
assertion that it verified the actor. The publisher signature itself still works.

**Recommended fix:** persist the exact timestamp string, include it in the
causation schema and protocol, and provide a verification test reconstructing
the actor signing bytes solely from envelope fields and the body hash. Require
method/path/timestamp for new request-attributed records. Explicitly distinguish
legacy records whose timestamp cannot be recovered; never invent one.

## F3 — High: publisher configuration failure exhausts delivery retries

**Locations:** `server/agentforge_server/outbox.py:63-92,107-128`;
`server/agentforge_server/publisher.py:95-110`;
`server/agentforge_server/worker.py:63-68`.

Publisher loading is lazy, inside the per-event try block, **after** incrementing
attempts and committing the lease. Missing production keys and invalid seeds
raise configuration errors which are caught as ordinary delivery failures.
The worker does not fail at startup for an enabled but unusable publisher.

**Reproduction:** `test_a2_configuration_error_does_not_dead_letter_events`
enables a mock transport, removes the production signing key, and advances
retry eligibility through ten attempts. Observed: **DEAD, attempts=10, zero
transport calls**. Correcting the key later does not make DEAD rows claimable.

**Recommended fix:** when publication is enabled, validate/load the publisher
before claiming any events, and fail fast on operator configuration errors.
Leave event retry budgets untouched for configuration failures. Disabled
transport should remain usable for reaping without a signing key. Add an
integration test through worker startup as well as through `drain_once`.

## F4 — Medium: runtime envelope validation does not enforce the schema

**Locations:** `server/agentforge_server/event_envelope.py:146-209,232-249`;
`protocol/v1/event-envelope.schema.json`.

`validate_envelope()` is a partial handwritten check, not validation against the
published schema. In particular it does not reject additional server properties,
even though signing bytes omit every server property except publisher/key ID.

**Reproduction A:** `test_a3_unsigned_server_extension_is_rejected` builds a
valid envelope, appends `server.untrusted_extra` without resigning, and observes
`verify_envelope(...) == True`. This does **not** forge the signed core fields;
it incorrectly accepts an unsigned extension in a supposedly validated object.

**Reproduction B:** `test_a4_builder_enforces_published_schema` configures a
161-character publisher ID. `build_envelope` succeeds although the schema caps
it at 160. Strict downstream receivers can reject envelopes this worker emits.

**Recommended fix:** make build/verify use one authoritative, packaged schema
(or equivalent complete checks), enforcing additional-properties, types,
lengths and patterns. Keep the v1 signing definition stable by rejecting unknown
server fields. Add schema/runtime parity tests and negative tests for mutated
server objects. Two audit probes map to this one underlying validation finding.

## F5 — Medium: startup accepts a database missing the new outbox columns

**Locations:** `server/agentforge_server/db.py:33-49`;
`server/agentforge_server/worker.py:34-41`;
`migrations/versions/c4d5e6f7a8b9_outbox_event_attribution.py`.

`verify_schema()` checks table names only. The previous revision already has
all table names, but lacks actor/causation/telemetry columns now required by ORM
queries. The development `create_all()` path does not upgrade existing tables
either. A stale database can pass startup checks and fail on actual use.

**Reproduction:** `test_a6_schema_guard_rejects_pre_outbox_revision` migrates to
`3293de03bb66` only. `verify_schema()` returns successfully instead of rejecting
it. This is an existing guard weakness exposed by the additive migration, not a
failure of the migration's upgrade/downgrade operations.

**Recommended fix:** check the expected migration revision and/or required
columns at startup; return an actionable migration error. Document upgrading
existing development volumes, and coordinate worker readiness with completed
API migrations rather than table presence alone.

## F6 — Medium: stale ORM attempt counts bypass the retry limit

**Locations:** `server/agentforge_server/outbox.py:53-78,107-125`;
`server/agentforge_server/db.py:22,30`.

Candidates are ORM instances retained with `expire_on_commit=False`. After
another worker retries the row, a conditional SQL claim increments the current
DB count, but the identity-map object can retain a stale count. `db.get()` does
not guarantee a fresh read; dead-letter and backoff decisions use that object.

**Reproduction:** `test_a7_retry_budget_uses_fresh_database_attempts` interleaves
two sessions: A reads attempts=8, B makes failed attempt 9, A resumes after B's
backoff and makes failed attempt 10. Observed DB state is **attempts=10,
PENDING**, not DEAD. The test advances a controlled clock instead of waiting.

**Recommended fix:** use fresh claimed-row state (`UPDATE ... RETURNING` with
appropriate ORM refresh, or explicit refresh after claiming), and base retry
budget decisions on the authoritative attempt count. Also check final update
rowcounts before incrementing the returned delivery count if lease ownership
has changed. The latter is a related review observation, not a separate probe.

## D1 — Medium: handoff documentation confuses roadmap phases with actual PRs

**Locations:** `docs/PROJECT_STATUS.md:49-59,66-86`;
`docs/AUDIT_VERIFICATION.md:49-65`; `docs/PR_PLAN.md:13-19`.

The checkout calls the signed outbox “PR #3”, says it is merged into main, and
calls PR #4 the next consensus change. The supplied human handoff identifies
actual GitHub PR #3 as the docs reconciliation and PR #4 as the signed-outbox
change. Documentation also associates outbox verification with a CI run which
`AUDIT_VERIFICATION.md` itself identifies as PR #2's run.

This inconsistency is visible locally. **Current remote merge/CI state was not
verified**, and this audit must not be used to assert it. Separate roadmap
phase numbers from actual GitHub PR numbers, and label verification with its
exact tested commit/branch. Correct history claims only against confirmed facts.

## Remaining coverage and operational gaps

- CI currently runs compilation and default pytest only; schema/OpenAPI drift,
  migration execution, PostgreSQL concurrency and compose readiness are not gates.
- Existing shutdown coverage sets the stop event before the first tick. It does
  not test SIGTERM during slow delivery. A tick can process 50 sequential events;
  the worker checks stop only between ticks. Test container stop grace and
  at-least-once redelivery, and avoid promising that leases are never abandoned.
- No live transport contract or receiver-side key-distribution/trust procedure
  was verified. A caller-supplied trusted public key is required by the verifier;
  key discovery and rotation operations need an explicit operator runbook.
- The string/scalar allowlist is not content-aware secret detection. Current
  task/proof redaction tests pass, but generic allowed fields such as `reason`
  should stay constrained to defined public codes in future producers.
- Legacy outbox rows retain old payloads and lack actor causation after the
  nullable migration. Do not present them as fully attributed new-format events
  or claim this migration scrubs historical stored private data.

## Suggested remediation order

1. Atomic reaping and side effects (F1).
2. Complete causal signing material and schema-enforced verification (F2/F4).
3. Publisher startup validation and fresh retry state (F3/F6).
4. Migration readiness, operational tests, and documentation corrections (F5/D1).

Re-run the explicit audit probes and default suite after fixes. Obtain a
PostgreSQL integration run before making production-concurrency claims. Submit
findings/fixes for human review; no agent should merge or self-approve them.
