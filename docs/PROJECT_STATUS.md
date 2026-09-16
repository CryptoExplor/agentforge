# AgentForge project status

**Snapshot date:** 2026-09-15 (Asia/Calcutta)
**Release shape:** pre-testnet MVP, audit-fix baseline
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
- No provider-boundary refactor beyond the current mock behavior; that is the next implementation phase and must preserve the tested semantics.

## Verification result

The following checks were run in the final workspace before archive generation:

| Check | Result |
|---|---|
| `python -m pytest -q` | **16 passed**, 1 Starlette/httpx deprecation warning |
| `python -m compileall -q server sdk tests examples` | **Passed** |
| JSON Schema meta-validation for `protocol/v1/*.schema.json` | **Passed** |
| Generated FastAPI OpenAPI compared with `protocol/v1/openapi.json` | **Match** |
| Alembic SQLite `upgrade head -> downgrade base -> upgrade head` | **Passed**, revision `3293de03bb66` |
| PostgreSQL integration run | **Not run in the sandbox**; no Docker, Podman, or `psql` executable was available |

See [`AUDIT_VERIFICATION.md`](AUDIT_VERIFICATION.md) for requirement-by-requirement traceability and exact test names.

## Recommended first GitHub action

Treat the uploaded archive as the baseline/import commit. Do not mix the next provider-boundary refactor into that import. Read these files in order:

1. [`GITHUB_HANDOFF.md`](GITHUB_HANDOFF.md)
2. [`AUDIT_VERIFICATION.md`](AUDIT_VERIFICATION.md)
3. [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md)
4. [`PR_PLAN.md`](PR_PLAN.md)
5. [`REPOSITORY_MAP.md`](REPOSITORY_MAP.md)

The first implementation PR after the baseline should extract the current local escrow behavior behind a `SettlementProvider` and a `MockSettlementProvider` without changing externally observed mock behavior. It should not add FLOP assumptions.

## Known verification limitation

The sandbox did not provide a PostgreSQL server/client, so the PostgreSQL dialect and production-like compose path still need to be exercised in GitHub Actions or a developer environment. SQLite migration round-trip and the SQLAlchemy model metadata were verified locally; that is not a substitute for PostgreSQL integration coverage.
