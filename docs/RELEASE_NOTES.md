# AgentForge pre-testnet MVP release notes

**Baseline date:** 2026-09-15 (Asia/Calcutta)

## Included

This archive is a source snapshot intended for upload to GitHub. It contains the AgentForge pre-testnet MVP, its protocol contract, tests, migration, Docker separation, and Markdown handoff files.

The audit-fix work covers claim expiry/reopen, persistent idempotency, conservative provenance, independent deterministic validation, private-resource authorization, mock escrow transitions, race-safe active claims, leased outbox delivery, cursor audit reads, migration boundaries, persisted inference submission links, Decimal accounting, disputes, and server-derived independence.

## Verification summary

- 16 pytest tests passed.
- Python compile check passed.
- All protocol JSON Schema documents passed Draft 2020-12 meta-validation.
- `protocol/v1/openapi.json` matched `agentforge_server.app.openapi()`.
- SQLite Alembic upgrade/downgrade/upgrade passed at revision `3293de03bb66`.
- PostgreSQL was not available in the build sandbox and must be checked in CI or a PostgreSQL environment before real deployment.

## Usage warning

`MOCK` and `TEST_CREDIT` are local test assets. They are not FLOP tokens, not official FLOP inference receipts, and not a promise of airdrop eligibility. The repository intentionally contains no airdrop scoring/farming automation and no guessed external settlement contract.

## Next planned work

The next branch should isolate local escrow behind a provider interface while preserving all current tests and semantics. See [`PR_PLAN.md`](PR_PLAN.md) and [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md).
