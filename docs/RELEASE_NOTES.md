# AgentForge pre-testnet MVP release notes

**Baseline date:** 2026-09-15 (Asia/Calcutta)

## Included

This archive is a source snapshot intended for upload to GitHub. It contains the AgentForge pre-testnet MVP, its protocol contract, tests, migration, Docker separation, and Markdown handoff files.

The audit-fix work covers claim expiry/reopen, persistent idempotency, conservative provenance, independent deterministic validation, private-resource authorization, mock escrow transitions, race-safe active claims, leased outbox delivery, cursor audit reads, migration boundaries, persisted inference submission links, Decimal accounting, disputes, and server-derived independence.

## Verification summary

- 16 pytest tests passed at the 2026-09-15 baseline; 24 pass on `main` after PR #1 and PR #2 were merged (see [`AUDIT_VERIFICATION.md`](AUDIT_VERIFICATION.md)).
- Python compile check passed.
- All protocol JSON Schema documents passed Draft 2020-12 meta-validation.
- `protocol/v1/openapi.json` matched `agentforge_server.app.openapi()`.
- SQLite Alembic upgrade/downgrade/upgrade passed at revision `3293de03bb66`.
- PostgreSQL was not available in the build sandbox and must be checked in CI or a PostgreSQL environment before real deployment.

## Usage warning

`MOCK` and `TEST_CREDIT` are local test assets. They are not FLOP tokens, not official FLOP inference receipts, and not a promise of airdrop eligibility. The repository intentionally contains no airdrop scoring/farming automation and no guessed external settlement contract.

## Merged since the baseline

- PR #1 (`refactor: isolate mock settlement provider`, merge `4521722`) moved the local escrow behavior behind `SettlementProvider`/`MockSettlementProvider` without changing mock semantics.
- PR #2 (`feat: add server-derived asset and mode guardrails`, merge `ecd9300`) added the `MOCK`/`TEST_CREDIT` allow-list, server-derived provider/deployment mode, and the zero-reward primary-asset fix.
- PR #3 (`feat: signed dual-attribution event outbox`) added the versioned `agentforge-event/1` envelope, the server publisher identity, actor/causation attribution, feature-flagged transport, delivery telemetry, and the worker service.
- A documentation PR reconciled the status, verification, and repository-map documents with the merged provider boundary and guardrails.

## Next planned work

Deferred, needs explicit scope approval: multi-validator consensus with dispute escalation, then the TCLK adapter and any official external provider. See [`PR_PLAN.md`](PR_PLAN.md), [`AUDIT_FEEDBACK_LOG.md`](AUDIT_FEEDBACK_LOG.md), and [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md).
