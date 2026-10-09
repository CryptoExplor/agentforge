# AgentForge project status

**Canonical current-status summary — 2026-10-09 (Asia/Calcutta).**

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
- Phase 2.2 protected-staging readiness: `deploy/docker-compose.staging.yml`
  composes PostgreSQL 16, a one-shot Alembic migration gate, the API and the
  outbox worker with health/dependency ordering, loopback-only host binding and
  an internal database network. Its environment template fails closed with
  enrollment, mock faucet, operator self-grants and publishing disabled;
  systemd/backup templates and `DEPLOYMENT_STAGING_RUNBOOK.md` cover health,
  clock skew, backup/restore and rollback. `scripts/simulate_marketplace.py`
  drives three SDK identities through funding, claim, proof, deterministic or
  authorized peer validation, fee/payout accounting and reputation over real
  HTTP; a Uvicorn TCP integration regression covers the default path. No chain
  skeleton was added: `EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md` records the
  pinned-interface, no-locks-across-network-I/O and durable reconciliation gate.
- Phase 2.3 autonomous agent and validator daemons: `scripts/agent_worker.py`
  is a long-running executor (jittered discovery with exponential backoff,
  capability-gated claiming, a background lease heartbeat at half the
  server-declared window, pluggable `--handler capability=module:callable`
  work, signed proof submission with a pinned `submission_id`, and
  SIGINT/SIGTERM shutdown that stops claiming and then drains in-flight work).
  A bounded declined map records permanent claim refusals (403 capability or
  independence, 404 gone) so the loop stops re-attempting a claim it can never
  win: a live run against uvicorn made 51 claim attempts for one
  independence-refused task in a few seconds before this, and 1 after.
  `scripts/validator_worker.py` is the peer-review counterpart: it discovers
  pending proofs, re-derives the acceptance criteria independently
  (result-hash reproduction, committed `expected_result_hash`, required
  outputs/evidence, acceptance schema) and submits signed decisions, voting
  `REJECTED` only under an explicit `--reject` because moving escrow is not the
  same as holding an opinion. Each thread holds its own SDK client, since
  transport clock-calibration state is per-connection. One additive read,
  `GET /api/v1/tasks/{task_id}/submissions`, closes the discovery gap that made
  third-party peer review impossible: a decision signature must cover the
  pending submission's ID and proof hash, but `GET /api/v1/events` is scoped to
  the caller's own audit rows, so only the poster and executor could previously
  learn either. It is identifier-and-commitment only, carries no new authority
  (the same `authorize_task_read` gate as the single-submission read) and adds
  an operation to an existing path, so the contract stays at 23 paths with no
  migration. `docs/TESTNET_QUICKSTART.md` is the operator-facing onboarding
  guide and states the scope plainly: mock credits are database rows, there is
  no public testnet, and external settlement remains gated.
  **Verification baseline for this phase: 402 passed, 3 skipped (389 prior,
  13 new, 0 modified); 23 OpenAPI paths (7 packaged schemas); Alembic head
  `b0c9d8e7f6a5` (schema untouched).**
- Phase 2.4 unified operator CLI `agentforge-cli`: a new `agentforge_cli`
  package next to the SDK (`sdk/python/agentforge_cli/`) mechanizes the
  one-off operator actions that previously required inline Python — identity
  generation/inspection (atomic, mode-0600 saves; the private key is never
  echoed; loose permissions produce a warning), registration, whoami,
  balance, reputation, capability index and agent search, task
  list/get/create/cancel/submissions with keyset and offset pagination,
  claim, heartbeat, signed proof submission with pinned submission ids,
  submission/proof reads, task-scoped and submission-scoped peer validation,
  dispute open/resolve, the per-DID event feed and `/health`. Every command
  maps to one existing `AgentForgeClient` method or one unsigned public read:
  the CLI adds no API surface, signing format, retry or server behaviour, and
  the server is untouched (23 OpenAPI paths and the Alembic head are
  unchanged). `--json` prints the raw server response as the scripting
  contract; exit codes separate usage failures (2) from live server/transport
  errors (1), whose status and detail are surfaced verbatim. The CLI ships in
  both wheels: the root `agentforge` distribution gains the package and the
  `agentforge-cli` console script, and the standalone `agentforge-sdk` wheel
  picks the package up through its existing `packages.find` plus the same
  entry point — it needs only the SDK's own dependencies. The long-running
  daemons remain `scripts/agent_worker.py` / `scripts/validator_worker.py`
  and are deliberately not wrapped (they are services, not one-shot
  operations); `docs/OPERATOR_CLI.md` is the command reference, and
  `agentforge_cli.main.client_factory` is the documented test seam the new
  suite uses to point the SDK transport at the in-process app.
  **Verification baseline for this phase: 416 passed, 3 skipped (402 prior,
  14 new, 0 modified); 23 OpenAPI paths (7 packaged schemas); Alembic head
  `b0c9d8e7f6a5` (server untouched).**

- Phase 2.5 chain-agnostic settlement-attempt state machine: an additive migration
  creates immutable intent, fenced attempt and append-only transition-journal
  tables. Trusted in-process producers enqueue transactionally; a dormant worker
  closes SQL sessions before invoking separate submission/receipt-verification
  ports. It dispatches at most once, reconciles ambiguous outcomes without
  resubmission, checks exact intent/party/amount/finality commitments, follows
  verified replacements without another send and continues observing finalized
  attempts for reorgs. Database constraints enforce economic-slot and active-
  attempt exclusivity. No concrete rail, signer, verifier, provider enablement,
  deployment or mock-to-external integration ships. Existing mock settlement,
  HTTP/signing contracts, SDK and publishing worker are unchanged. See
  [settlement attempts](SETTLEMENT_ATTEMPTS.md).

**Current measured baseline — Phase 2.5:** full default suite → **489 passed,
3 skipped** (417 pre-change + 72 new, **0 existing tests modified**); the new
suite plus PostgreSQL-selected audit suites → **267 passed, 0 skipped** on
local disposable PostgreSQL 16.2. Both installed wheels passed outside source
import paths. `scripts/check_contracts.py` → `SCHEMAS_OK: 7`,
`OPENAPI_MATCH: 23 paths`, `MIGRATION_HEAD_MATCH: c1d2e3f4a5b6`.
The public contract files are byte-identical to the post-merge main baseline.
These are implementation-side results, not independent approval or deployment
readiness; maintained-image CI is a separate gate.

The Phase 2.4 paragraph's **416** was an earlier review snapshot: a fresh run on
post-merge `38eda8073439b3dd5b31843d111afbc3c7867a38` measured **417 passed,
3 skipped** before this phase. That snapshot is retained as historical evidence,
not used as the current baseline.

**Current GitHub baseline:** live fetch and PR lookup confirmed PR #14 merged
on 2026-10-08 at `38eda8073439b3dd5b31843d111afbc3c7867a38`; `origin/main`
and this session's starting HEAD matched. Implementation remains on
`arena/9ddbb84c-agentforge`, published as [PR #15](https://github.com/CryptoExplor/agentforge/pull/15)
against `main`, open for separate audit and human-only merge. Implementation
commit `855992ef5f5ae1c4cad0114512139958f831cd54` passed all three CI jobs
(`test`, `postgres-audit-regressions`, `dependency-audit`) in
[run 37842401207](https://github.com/CryptoExplor/agentforge/actions/runs/37842401207).
This publication-record follow-up changes documentation only. Independent
review and merge approval remain pending. The PR #4–#6 snapshots below are
historical, not this phase's publication.

See [verification and audit findings](AUDIT_VERIFICATION.md) for exact test counts,
commands, dependency evidence and limitations. Technical controls live in
[security remediation](SECURITY_REMEDIATION.md),
[accounting remediation](ACCOUNTING_REMEDIATION.md) and [event outbox](EVENT_OUTBOX.md).

## Historical review publication

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
