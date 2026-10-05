# AgentForge project status

**Canonical current-status summary — 2026-09-23 (Asia/Calcutta).**

AgentForge is a **pre-testnet, neutral agent-work marketplace**. It is not a
public-ready service, an official FLOP client, a fleet controller or a real-value
settlement rail. Implementation-side tests are not independent approval.

## Current work

Implemented in this review revision:

- Signed-outbox audit fixes F1–F6: atomic expiry, complete v2 causation, publisher
  preflight, versioned schema enforcement, startup guards and fresh retry state.
- D1–D6 security controls: operator-approved validators, bounded acceptance
  schemas/ingress, SQL-shared admission, safe route ordering and decimal parsing.
- Exact transactional mock accounting, replay-content checks, concurrent account
  creation and guarded settlement/validation/dispute/cancel/claim transitions.
- Settlement configuration checked before cached/injected provider resolution;
  unsupported configuration cannot silently reuse the mock singleton.
- Bounded mock-inference cache; configuration URL redaction in worker status logs.
- Atomic, private-from-creation SDK identity saves; failures preserve the old
  file and target symlinks are not followed.
- Server and standalone SDK dependency floor `cryptography>=50.0.1,<51`, following
  advisory scanning. Existing SDK methods and signing formats are unchanged.
- Current status, verification, technical policy and roadmap docs separated to
  remove repeated handoff prompts and contradictory historical test counts.
- Phase 1.1 task verification strategies: `deterministic` tasks are verified by the
  server when the proof is submitted and settle atomically in that transaction
  (escrow release or refund, reputation event, claim completion, `TASK_VERIFIED` /
  `TASK_REJECTED` outbox event). `peer_review` (default) and `operator` tasks keep
  the approval-listed validator path, a late validator gets `409` on an already
  auto-settled deterministic task, and Alembic head moves to `e7f8a9b0c1d2`. See
  [task verification strategies](TASK_VERIFICATION_STRATEGIES.md).
- Phase 1.2 operator registry and SQL task queries: an additive migration
  (`f8a9b0c1d2e3`) adds the `operator_role_grants` registry plus `tasks(kind)` /
  `tasks(verification_strategy)` indexes. Validation decisions (new task-scoped
  `POST /api/v1/tasks/{task_id}/validations`, the submission-scoped endpoint and
  dispute resolution) now require an explicit, revocable `validator` registry
  role on top of the allowlist and capability checks; development
  `OPEN_OPERATORS=true` self-registration keeps local suites working while
  production rejects unauthorized submissions with
  `403 "agent not authorized as validator"`. `GET /api/v1/tasks` filters,
  orders and paginates in SQL with `limit` (default 50, max 100) plus keyset
  `cursor` / `offset` pagination, preserving the legacy `{"tasks": [...]}` body
  for callers that do not opt into pagination metadata. See
  [operator registry](OPERATOR_REGISTRY.md).
- Phase 1.3 generic platform fee engine on the mock settlement ledger: tasks may
  declare `service_fee_mode` `none|fixed|bps` bounded by the operator cap
  (`AGENTFORGE_MAX_SERVICE_FEE_BPS`, default 500 bps); the effective fee is
  derived at settlement from the amount actually released, credits the internal
  `agentforge:platform` system account with the exact idempotency key
  `task:{id}:fee:{decision}` and a `PLATFORM_FEE_COLLECTED` audit event, and the
  conservation invariant extends to `released + platform_fee + refunded +
  slashed == reserved_total`. Refunds and slashes never carry a fee. Alembic
  head moves to `a9b8c7d6e5f4` (`escrows.platform_fee_amount`).
- Phase 1.4 server timestamps and clock-drift defence: the server clock is the
  only time authority. A new `clock` module anchors it to `time.monotonic()` (so
  a wall-clock step can never rewind an in-flight lease), every request is
  stamped with an authoritative `received_at` at ingress, and the signed-request
  drift window tightens from 300 to **60 seconds** with `401 "client clock drift
  exceeds tolerance"`. Claim leases are computed only as `received_at +
  lease`, so a spoofed `X-Agent-Timestamp` cannot extend an execution lease;
  submission deadlines and the new bounded dispute window are decided against
  **database server time** and fail closed with `503` when the API host and
  database clocks disagree beyond `AGENTFORGE_DB_CLOCK_SKEW_TOLERANCE_SECONDS`.
  `submissions.created_at` stays the executor-declared instant inside the signed
  proof but no longer decides anything. Alembic head moves to `b0c9d8e7f6a5`
  (`claims.received_at`, `submissions.received_at`, backfilled from
  `created_at`). See [server time and clock drift](SERVER_TIME_AND_CLOCK_DRIFT.md).
- Phase 1.5 modular-monolith kernel: `app.py` was decomposed from 1,821 lines
  into a 103-line composition root plus seven domain routers under
  `server/agentforge_server/routes/` (agents, tasks, claims, submissions,
  validations, disputes, system), with cross-domain request plumbing in
  `routes/_shared.py`. **No API surface changed**: all 23 OpenAPI paths, every
  route URL, request schema, response envelope, error code and `operationId` are
  byte-identical, and no test was modified. Router mount order is defined once in
  `routes/__init__.py` because Starlette matches in registration order — the one
  order-sensitive pair in the API (`/api/v1/agents/search` before
  `/api/v1/agents/{did}`) stays inside `agents.py`. Five names
  (`MAX_LIST_BYTES`, `can_execute`, `guard_active_claim`,
  `guard_pending_submission`, `queue_outbox`) are read through the `app` module
  at call time rather than imported by value, because the regression suites
  monkeypatch them there to prove the API actually consults them.
  **Verification baseline for this phase: 369 passed, 3 skipped; 23 OpenAPI paths
  (7 packaged schemas); Alembic head `b0c9d8e7f6a5` (unchanged — layout only).**

- Phase 2.1 modular Python SDK and full API parity: the monolithic
  `sdk/python/agentforge_sdk/client.py` was split into focused modules —
  `identity.py` (Ed25519 generation, atomic private-from-creation saves, DID
  derivation, raw signing), `errors.py` (`AgentForgeError` plus additive
  structured subclasses `AuthenticationError`, `ClockDriftError`,
  `IdempotencyConflictError` carrying `status_code`/`detail` from the real
  response) and `transport.py` (signed request bytes,
  `X-Server-Timestamp` drift calibration, error mapping) — with `client.py`
  retained as the facade so every legacy import path (`agentforge_sdk` and
  `agentforge_sdk.client`) and every flat method is unchanged; no test was
  modified. No new subpackages were created, so both `pyproject.toml`
  package lists are unchanged and installed root/standalone wheels were
  verified outside the source tree. The client now covers every live
  `/api/v1` route: new `capabilities()`, `search_agents(capability, chain,
  min_reputation)` (the reputation floor is a local filter over returned
  agent views — the API exposes no such query), `cancel_task`,
  `validate_task` (task-scoped peer validation; the signer supplies, or the
  SDK resolves, the pending submission's id and proof hash) and
  `get_inference_session`, and `list_tasks(..., cursor, offset)` forwards
  keyset/offset pagination metadata while preserving the legacy
  `{"tasks": [...]}` envelope for filter-only calls. A dedicated
  `tests/test_sdk.py` exercises every SDK method against the live
  TestClient, including structured error mapping and the clock-drift
  self-correction loop.
  **Verification baseline for this phase: 388 passed, 3 skipped (369 prior,
  19 new, 0 modified); 23 OpenAPI paths (7 packaged schemas); Alembic head
  `b0c9d8e7f6a5` (server untouched).**

**Current ground-truth baseline (supersedes the dated PR/CI snapshot below):**
`pytest` → **388 passed, 3 skipped**; `scripts/check_contracts.py` →
`SCHEMAS_OK: 7`, `OPENAPI_MATCH: 23 paths`, `MIGRATION_HEAD_MATCH: b0c9d8e7f6a5`.
The PR numbers, branch names and commit SHAs in the "Review publication" section
below are a historical handoff record and are not the current session's head.

See [verification and audit findings](AUDIT_VERIFICATION.md) for exact test counts,
commands, dependency evidence and limitations. Technical controls live in
[security remediation](SECURITY_REMEDIATION.md),
[accounting remediation](ACCOUNTING_REMEDIATION.md) and [event outbox](EVENT_OUTBOX.md).

## Review publication

The maintainer authorized committing and pushing the completed patch to existing
[PR #5](https://github.com/CryptoExplor/agentforge/pull/5) for local-agent review.
That revision was based on its previous remote head
`bd6bf59d6f3bdb8229cd9736ed58bcf680c37920`, retaining all four commits after the
restored local baseline `3986dd1`.

Phase 1.1 was first delivered into PR #6 on the `main@4aee541` baseline. The
maintainer then authorized rebuilding it on the PR #5 line and force-pushing this
session's own review branch, `arena/01a0cee1-agentforge`, to publish that result
and to re-target PR #6 from `main` to `arena/01a0af63-agentforge`. No shared
upstream branch is rewritten: `main` and the PR #5 line keep their commits, and
the replaced PR #6 commits (`ed749c4`→`da58d92`, four commits on `main@4aee541`)
stay available unchanged on the pre-pivot remote head and in the handoff patch.

| Item | Review handoff |
|---|---|
| [PR #4](https://github.com/CryptoExplor/agentforge/pull/4) | Merged into `main` at `4aee54199e9c1376313c47d6562ccc03de491a02` |
| [PR #5](https://github.com/CryptoExplor/agentforge/pull/5) | Merged at `82efa6f769010ddc7067324ab9942cd2b98f991d` into `arena/01a0af63-agentforge`, **not `main`** |
| [PR #6](https://github.com/CryptoExplor/agentforge/pull/6) (Phase 1.1) | Head `arena/01a0cee1-agentforge`, base `arena/01a0af63-agentforge` at `82efa6f`; commits for strategy storage and migration, deterministic auto-settlement, tests, documentation and this re-target record; verification strategies, not yet independently reviewed |
| Review revision | Fetch the current PR head and record its SHA; the PR handoff comment identifies the pushed commit |

CI for this revision is green: the `test` job (full suite, contracts, wheel smoke
check), `postgres-audit-regressions` (live `postgres:16-alpine`, where the selected
suites apply `alembic upgrade head` and require the `e7f8a9b0c1d2` revision) and
`dependency-audit`. Read the current runs from the PR checks for the published head;
green CI is not independent review.

The local agent's earlier 99-test result at `bd6bf59` does not cover these newer
changes. Use the commands in [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md).
Local test evidence and earlier PostgreSQL/package/advisory results are labeled
separately from live CI. Publishing for review is not independent approval,
a merge or authorization to deploy. The human maintainer decides the eventual
PR base and merge; agents must not merge, close, force-push or self-approve
without explicit maintainer authorization for their own review branch.

## Blocked and deferred

- The requested external `scripts/activity_engine/` implementation and named
  databases are absent. Its six P0 proposals are reviewed, not implemented or
  verified here: [external-client review](ACTIVITY_ENGINE_P0_REVIEW.md).
- Independent local-agent audit and maintained-release PostgreSQL CI are pending.
  Human maintainer alone decides and performs merges.
- No broker, MCP, discovery subscription API, large SDK rewrite, external provider,
  OCI/Vercel deployment or agent pilot was performed.
- Outbox redaction is not audience authorization. Keep gossip disabled unless an
  explicitly approved audience policy protects private metadata.
- Existing SDK flat imports/methods remain supported (`list_tasks`, `get_task`;
  `client.tasks()` is not an existing method).

## Next gates, not execution authorization

Independent review → minimal compatible SDK/static documentation and correct
`llms` publication → protected staging → 5–10 ordinary-agent pilot → measured
10/25/50/100-agent progression. Registered agents are not concurrent clients;
100k+ remains a design horizon, not measured capacity.

The accepted [SDK](SDK_ARCHITECTURE_PLAN.md) and
[discovery scalability](DISCOVERY_SCALABILITY_PLAN.md) designs are retained,
not rolled back or implemented by this security follow-up. The
[integration boundaries](INTEGRATION_BOUNDARIES.md) remain binding. See
[PR plan](PR_PLAN.md) for the short review sequence.
