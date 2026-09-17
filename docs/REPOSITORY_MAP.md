# AgentForge repository map

This is the short map for a GitHub reviewer or coding agent.

| Path | Responsibility |
|---|---|
| `README.md` | Quick start, current MVP boundary, audit-fix decisions |
| `AGENTFORGE_ARCHITECTURE.md` | Long-form product and protocol architecture |
| `ANTIGRAVITY_IMPLEMENTATION_BRIEF.md` | Frozen implementation brief and non-goals |
| `docs/PROJECT_STATUS.md` | Current release snapshot and verification summary |
| `docs/AUDIT_VERIFICATION.md` | Audit requirement traceability and commands |
| `docs/EVENT_OUTBOX.md` | Signed event outbox: attribution, envelope, configuration, worker, non-goals |
| `docs/ARCHITECTURE_DECISIONS.md` | Frozen decisions and deferred provider strategy |
| `docs/GITHUB_HANDOFF.md` | Upload steps, new-chat prompt, branch/commit guidance |
| `docs/PR_PLAN.md` | Small future PR sequence and review checklist |
| `docs/REPOSITORY_MAP.md` | This file |
| `server/agentforge_server/app.py` | FastAPI routes, signing/authentication, idempotency, authorization, expiry, submissions, validation, disputes |
| `server/agentforge_server/services.py` | Reaper/deadlines, provenance/independence, ledger/escrow, reputation, audit, outbox helpers |
| `server/agentforge_server/models.py` | SQLAlchemy tables, unique constraints, active-claim index |
| `server/agentforge_server/schemas.py` | Pydantic request/response validation |
| `server/agentforge_server/validators/deterministic.py` | Independent hash, acceptance, evidence, schema, receipt, and deadline checks |
| `server/agentforge_server/outbox.py` | Leased at-least-once delivery of signed envelopes, retries, dead-letter, metrics |
| `server/agentforge_server/event_envelope.py` | Versioned canonical event envelope: actor, causation, payload hash, publisher signature |
| `server/agentforge_server/publisher.py` | Server event publisher identity and key loading (publisher only, never an identity root) |
| `server/agentforge_server/settlement.py` | `SettlementProvider` protocol, server-derived provider selection, deployment mode |
| `server/agentforge_server/adapters/mock_settlement.py` | Mock escrow transitions, `MOCK`/`TEST_CREDIT` allow-list, primary-asset derivation |
| `server/agentforge_server/providers.py` | Mock inference provider and non-official receipts |
| `server/agentforge_server/provenance.py` | Fail-closed server-registered source adapter boundary |
| `server/agentforge_server/adapters/technocore.py` | Optional coordination adapter: posts signed envelopes to a configured publish path |
| `server/agentforge_server/worker.py` | Reaper + outbox worker entry point with graceful shutdown and `--once` |
| `server/agentforge_server/db.py` | Database configuration, development schema setup, production guards |
| `server/agentforge_server/settings.py` | Environment and feature settings |
| `sdk/python/agentforge_sdk/` | Python identity, signing, and API client |
| `protocol/v1/*.schema.json` | Versioned machine-readable task, proof, agent, escrow, and validation contracts |
| `protocol/v1/signing.md` | Canonical signing rules, including published event envelopes |
| `protocol/v1/event-envelope.schema.json` | Signed event envelope contract (`agentforge-event/1`) |
| `protocol/v1/openapi.json` | Generated FastAPI API contract |
| `migrations/` | Alembic environment and initial schema revision |
| `tests/test_mvp.py` | Original end-to-end mock exchange coverage |
| `tests/test_audit_fixes.py` | P0/P1/P2 audit-fix integration coverage |
| `tests/test_asset_guardrails.py` | Asset allow-list, server-derived provider/mode, and zero-reward escrow coverage |
| `tests/test_signed_event_outbox.py` | Envelope signing, attribution, causation, redaction, transport flags, retries, worker |
| `tests/test_dispute_and_independence.py` | Dispute replay/settlement and server-derived independence coverage |
| `conformance/README.md` | Future external conformance fixture boundary |
| `Dockerfile` | Production-oriented image: explicit migration, no faucet |
| `Dockerfile.dev` | Development image: explicit auto-schema/mock-faucet convenience |
| `docker-compose.yml` | Production-like PostgreSQL composition |
| `docker-compose.dev.yml` | Development-only SQLite/faucet composition |
| `.github/workflows/ci.yml` | GitHub test and compile workflow |
| `.github/pull_request_template.md` | PR review checklist |

## Source-of-truth order

1. Signed server behavior and tests.
2. `protocol/v1/` schemas, signing rules, and generated OpenAPI.
3. `README.md` and the short docs in `docs/`.
4. Long-form architecture and implementation brief.
5. External draft material, which is context only and never an implementation authority.
