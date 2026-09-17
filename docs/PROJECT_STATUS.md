# AgentForge project status

**Snapshot date:** 2026-09-17 (Asia/Calcutta)
**Release shape:** pre-testnet MVP, audit-fix baseline with the settlement provider boundary merged
**Repository purpose:** a GitHub-ready handoff for future pull requests and small, reviewable commits

## Executive summary

AgentForge is a protocol-oriented Agent Work Exchange reference implementation. It coordinates signed agent identities, task discovery, claim leases, mock inference, signed proof bundles, deterministic validation, disputes, role-specific reputation, an authenticated audit stream, a retryable coordination outbox, and a local mock ledger.

The audit-fix implementation is complete for the frozen MVP scope. The code is intentionally conservative and provider-agnostic. It does **not** claim to be an official FLOP client, a TCLK settlement implementation, an airdrop calculator, or a hosted arbitrary-agent execution service.

## What is in this baseline

- FastAPI API under `/api/v1`.
- Ed25519 `did:key` registration and signed requests.
- Persistent idempotency records with replay and conflict behavior.
- Expiring claims, direct expiry checks, a reaper, and a database active-claim backstop.
- Public/private task authorization with fail-closed private payload reads.
- Mock inference receipts with separate requested, measured, paid, and verified compute fields.
- Submission proof hashes, evidence checks, declarative result-schema checks, and inference receipt integrity checks.
- Server-derived provenance trust and independence/anti-circularity checks.
- Immutable validation decisions and idempotent dispute/settlement paths.
- Explicit mock `FULL_RELEASE`, `PARTIAL_RELEASE`, `REFUND`, and `SLASH` transitions.
- An isolated `SettlementProvider` boundary (`settlement.py`,
  `adapters/mock_settlement.py`) with a server-derived `MOCK`/`TEST_CREDIT`
  allow-list, a primary escrow asset derived from the first funded component,
  and rejection of unsupported assets and providers.
- Decimal-string ledger accounting, append-only ledger/reputation/audit events, and outbox delivery leases.
- Alembic initial schema and development-only `create_all`/faucet behavior.
- Python SDK, protocol JSON Schemas, signing documentation, OpenAPI, Docker configurations, and CI.

## What is deliberately not in this baseline

- No real FLOP contract, fee, receipt, eligibility, airdrop, referral, or farming logic.
- No TCLK state-machine or cryptography copied into AgentForge.
- No custom HTLC/PTLC or invented external settlement API.
- No official FLOP participation claim for mock/local inference or mock credits.
- No arbitrary external-agent code execution inside the API process.
- No promise that a task is `ECONOMIC_ELIGIBLE` or `EXTERNAL_NETWORK_VERIFIED` because a client requested that status.
- No settlement provider other than the local mock. The boundary exists and rejects any other provider name at call time, but there is still no external rail, deal-reference model, or TCLK adapter to configure.

## Verification result

The following checks were re-run on `main` after PR #1 and PR #2 were merged:

| Check | Result |
|---|---|
| `python -m pytest -q` | **24 passed**, 2 Starlette/httpx deprecation warnings |
| `python -m compileall -q server sdk tests examples` | **Passed** |
| JSON Schema meta-validation for `protocol/v1/*.schema.json` | **Passed**, 5 of 5 |
| Generated FastAPI OpenAPI compared with `protocol/v1/openapi.json` | **Match**, 22 paths |
| Alembic SQLite `upgrade head -> downgrade base -> upgrade head` | **Passed**, revision `3293de03bb66` |
| `git diff --check` | **Clean** |
| GitHub Actions CI on `main` | **Success** (run `35153608459`) |
| PostgreSQL integration run | **Not run in the sandbox**; no Docker, Podman, or `psql` executable was available |

See [`AUDIT_VERIFICATION.md`](AUDIT_VERIFICATION.md) for requirement-by-requirement traceability and exact test names.

## Where the repository stands today

The archive import, PR #1 (`refactor: isolate mock settlement provider`, merge
`4521722`), and PR #2 (`feat: add server-derived asset and mode guardrails`,
merge `ecd9300`) are all merged into `main`. The provider-boundary refactor and
the asset/mode guardrails that this document originally listed as the next
implementation phase are therefore complete; see
[`AUDIT_FEEDBACK_LOG.md`](AUDIT_FEEDBACK_LOG.md) for the audit entry that
records the merged content, the zero-reward primary-asset defect, and its fix.

Read these files in order before starting new work:

1. [`GITHUB_HANDOFF.md`](GITHUB_HANDOFF.md)
2. [`AUDIT_VERIFICATION.md`](AUDIT_VERIFICATION.md)
3. [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md)
4. [`PR_PLAN.md`](PR_PLAN.md)
5. [`REPOSITORY_MAP.md`](REPOSITORY_MAP.md)

Each new PR should stay at the size of PR #1 or PR #2: one cohesive change,
focused regression tests, no invented FLOP or TCLK assumptions. The planned
next candidates are the durable outbox/gossip worker and multi-validator
consensus with dispute escalation; both need explicit scope approval first.

## Known verification limitation

The sandbox did not provide a PostgreSQL server/client, so the PostgreSQL dialect and production-like compose path still need to be exercised in GitHub Actions or a developer environment. SQLite migration round-trip and the SQLAlchemy model metadata were verified locally; that is not a substitute for PostgreSQL integration coverage.
