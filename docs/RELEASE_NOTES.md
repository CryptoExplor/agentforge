# AgentForge pre-testnet MVP release notes

## Unreleased — roadmap Phases 1.1–1.5

Review-branch changes layered on top of the 2026-09-18 follow-up below; not a
release, merge or deployment announcement. Current verification is in
[PROJECT_STATUS.md](PROJECT_STATUS.md) and [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md):
**369 passed, 3 skipped; 23 OpenAPI paths; 7 schemas; Alembic head `b0c9d8e7f6a5`**.

### Added

- **Phase 1.1 — task verification strategies** (`e7f8a9b0c1d2`): tasks declare
  `verification_strategy` (`deterministic`/`peer_review`/`operator`); deterministic
  tasks are verified and settled atomically in the submission transaction, with a
  `409` competing-validator guard. See [task verification strategies](TASK_VERIFICATION_STRATEGIES.md).
- **Phase 1.2 — operator registry & SQL task queries** (`f8a9b0c1d2e3`): revocable
  `operator_role_grants` gate validation decisions; `GET /api/v1/tasks` filters,
  orders and paginates in SQL. Adds the task-scoped
  `POST /api/v1/tasks/{task_id}/validations` route (OpenAPI paths 22 → **23**).
  See [operator registry](OPERATOR_REGISTRY.md).
- **Phase 1.3 — generic platform-fee engine** (`a9b8c7d6e5f4`): `service_fee_mode`
  `none`/`fixed`/`bps` capped by `AGENTFORGE_MAX_SERVICE_FEE_BPS`, derived at
  settlement, credited to `agentforge:platform`; `escrows.platform_fee_amount`
  with the invariant `released + platform_fee + refunded + slashed == reserved_total`.
  Mock assets only. See [marketplace fee design](MARKETPLACE_FEE_AND_FLOP_SETTLEMENT_DESIGN.md).
- **Phase 1.4 — authoritative server time & clock-drift defence** (`b0c9d8e7f6a5`):
  monotonic-anchored server clock, per-request `received_at`
  (`claims.received_at`, `submissions.received_at`), 60 s signed-request drift
  window, database-clock cross-check. See [server time and clock drift](SERVER_TIME_AND_CLOCK_DRIFT.md).

### Changed

- **Phase 1.5 — modular-monolith kernel:** `app.py` decomposed from ≈1,821 lines
  into a 103-line composition root plus seven domain routers under
  `server/agentforge_server/routes/`. **No API surface changed** — all 23 OpenAPI
  paths, route URLs, schemas, envelopes, error codes and `operationId`s are
  byte-identical and no test was modified.
- **Test helpers consolidated** into `tests/helpers.py`: the previously duplicated
  `signed_request` (8 copies) and `register` (7 copies) are now single-sourced;
  the full suite passes unchanged.

## Unreleased — security/accounting follow-up (2026-09-18)

These are review-branch changes, not a release, merge or deployment announcement.
Current publication state and verification results are maintained in
[PROJECT_STATUS.md](PROJECT_STATUS.md) and
[AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md).

### Fixed

- Provider-resolution follow-up: unsupported/invalid settlement configuration now
  fails even after the mock singleton is cached or a test provider is injected.
  Existing `mock`/`local` aliases are unchanged.

- Atomic mock-ledger balance changes and account creation; replay checks compare
  all immutable accounting fields, rather than trusting a reused key.
- Exact bounded money arithmetic across funding, partial refunds, slash and
  conservation; ambient Decimal precision can no longer silently erase debits.
- Competing settlement, validation, dispute, cancel/claim and cross-task claim-limit
  races guarded by transactional SQL writes.
- Bounded mock-inference retention; HTTP retrieval remains authorized and SQL-backed.
- Configuration URL secrets removed from transport/worker status output.
- SDK identity files created privately and replaced atomically, without following
  target symlinks or silently ignoring permission/write failures.
- Dependency advisory remediation in both distributions: `cryptography>=50.0.1,<51`.
- Duplicate status/verification/new-chat context consolidated into canonical docs;
  dated evidence remains explicitly historical.

### Preserved and verified

- Outbox F1–F6 fixes: atomic expiry, full v2 causation, strict envelope schemas,
  publisher preflight, fresh attempts and schema-startup guards.
- D1–D6 controls, including root-only schema dialect declarations, operator grants,
  ingress limits, shared quotas, closed production enrollment and no production faucet.
- Existing public API methods, SDK flat imports/signing formats and envelope
  compatibility. No new migration beyond the existing D1–D6 quota revision.

### Compatibility notes

Money that cannot fit the existing 80-character storage representation fails
closed; partial-settlement floats are rejected. Direct mock-provider cache lookups
may raise `KeyError` after eviction; persisted HTTP sessions remain available.
Transport status exposes only a configured marker instead of a URL.
Identity saves require a trusted directory; failure no longer silently succeeds.
See [accounting policy](ACCOUNTING_REMEDIATION.md) for transaction/retry rules.

## Historical foundation

The baseline/provider work established the neutral marketplace, mock settlement
boundary and server-derived asset/mode guardrails. Actual PR #4 added the signed
outbox foundation; subsequent remediation emits v2 for complete causation and
retains honest v1 compatibility. Dated audit records preserve the original findings
and checkpoint evidence; they are not the current test or merge status.

## Non-goals and gates

MOCK/TEST_CREDIT are not real assets or official FLOP receipts. No Activity Engine,
vLLM adapter, live sensor, real settlement provider, broker, MCP, SDK modularization
or deployment was added. Independent review and human-controlled merge remain
mandatory. Later work follows [PR_PLAN.md](PR_PLAN.md), not an implicit roadmap
execution or a promise of reward eligibility.
