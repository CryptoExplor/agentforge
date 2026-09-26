# AgentForge repository map

This is the short map for a GitHub reviewer or coding agent.

| Path | Responsibility |
|---|---|
| `README.md` | Quick start, current MVP boundary, audit-fix decisions |
| `AGENTFORGE_ARCHITECTURE.md` | Long-form product and protocol architecture |
| `ANTIGRAVITY_IMPLEMENTATION_BRIEF.md` | Frozen implementation brief and non-goals |
| `docs/SECURITY_REMEDIATION.md` | D1–D6 implementation, operator policy, limits, migration and audit handoff |
| `docs/PROJECT_STATUS.md` | Canonical current state, GitHub snapshot and blockers; no duplicate test ledger |
| `docs/DISCOVERY_SCALABILITY_PLAN.md` | 100k+ client design horizon, discovery projection/routing, private metadata policy, replay/backpressure, SDK/versioning and benchmark gates; not implemented |
| `docs/SDK_ARCHITECTURE_PLAN.md` | Current design comparison, SDK resources/compatibility, integration boundaries, file-level execution plan and next-chat handoff; proposed, not implemented |
| `docs/DEPLOYMENT_READINESS_2026-09-17.md` | Dated HTTP/runtime inventory, in-memory probe evidence and OPEN public-exposure findings D1–D6 |
| `docs/AUDIT_VERIFICATION.md` | Canonical current audit scope, results, commands, dependency evidence and limitations |
| `docs/ACCOUNTING_REMEDIATION.md` | Exact money, atomic ledger/escrow/lifecycle controls and compatibility |
| `docs/TASK_VERIFICATION_STRATEGIES.md` | Verification strategies, atomic deterministic auto-settlement and the competing-validator guard |
| `docs/SERVER_TIME_AND_CLOCK_DRIFT.md` | Authoritative server time: monotonic anchoring, `received_at`, drift window, lease invariants, dispute window, settings and limits |
| `docs/ACTIVITY_ENGINE_P0_REVIEW.md` | Missing external-client source boundary and six-item plan review |
| `docs/EVENT_OUTBOX.md` | Signed event outbox: attribution, envelope, configuration, worker, non-goals |
| `docs/ARCHITECTURE_DECISIONS.md` | Frozen decisions and deferred provider strategy |
| `docs/INTEGRATION_BOUNDARIES.md` | Independent marketplace, optional TCLK/Technocore/FLOP integrations, client policy and testnet evidence |
| `docs/protocol-intelligence/flop/CURRENT_STATE.md` | FLOP/TCLK research: retain/defer/exclude decisions, evidence boundaries and pilot gates |
| `docs/protocol-intelligence/flop/SOURCES.md` | Dated source ledger; draft, reported implementation and unverified claims kept separate |
| `docs/protocol-intelligence/flop/PARAMETER_SNAPSHOT.json` | Documentation-only draft values with sources/units; runtime values remain null |
| `docs/protocol-intelligence/flop/CHANGELOG.md` | Protocol-intelligence revision history |
| `docs/GITHUB_HANDOFF.md` | Short review/publication workflow linked to canonical context |
| `docs/PR_PLAN.md` | Small future PR sequence and review checklist |
| `docs/REPOSITORY_MAP.md` | This file |
| `server/agentforge_server/app.py` | Composition root only: tunables, lifespan schema guard, middleware, router mounting |
| `server/agentforge_server/routes/__init__.py` | `DOMAIN_ROUTERS` — the single definition of router mount order (route matching is order-dependent) |
| `server/agentforge_server/routes/_shared.py` | Cross-domain request plumbing: signing/authentication, nonce + idempotency, outbox causation, private-task read authorization, validator operator gate |
| `server/agentforge_server/routes/agents.py` | Registration challenges, manifest registration, agent discovery, balance lookup, capability index |
| `server/agentforge_server/routes/tasks.py` | Task creation and escrow funding, public discovery with keyset/offset pagination, task read and cancellation |
| `server/agentforge_server/routes/claims.py` | Task claiming, lease management and heartbeats |
| `server/agentforge_server/routes/submissions.py` | Inference sessions, signed proof submission, proof retrieval, deterministic auto-settlement |
| `server/agentforge_server/routes/validations.py` | Submission-scoped and task-scoped peer review (`apply_validation` is the single settlement path), reputation read |
| `server/agentforge_server/routes/disputes.py` | Dispute opening (escrow freeze, bounded window) and resolution |
| `server/agentforge_server/routes/system.py` | `/`, `/health` with clock diagnostics, per-agent signed event outbox feed |
| `server/agentforge_server/services.py` | Reaper/deadlines, provenance/independence, ledger/escrow, reputation, audit, outbox helpers |
| `server/agentforge_server/clock.py` | Authoritative monotonic-anchored server clock, request `received_at` stamping, drift evaluation and the database-clock cross-check |
| `server/agentforge_server/money.py` | Bounded exact decimal arithmetic independent of ambient context |
| `server/agentforge_server/models.py` | SQLAlchemy tables, unique constraints, active-claim index |
| `server/agentforge_server/schemas.py` | Pydantic request/response validation |
| `server/agentforge_server/validators/deterministic.py` | Independent hash, acceptance, expected-result-hash, evidence, schema, receipt, and deadline checks; `evaluate_deterministic` entry point for auto-settlement |
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
| `server/agentforge_server/admission.py` | SQL-atomic shared request quotas, enrollment/security configuration validation and safe nonce/challenge cleanup |
| `server/agentforge_server/operators.py` | Operator registry and role grants: explicit, revocable validator authorization for peer validation |
| `server/agentforge_server/middleware.py` | ASGI body/timeout/header/target/concurrency limits, admission and no-store responses |
| `server/agentforge_server/validators/result_schema.py` | Bounded acceptance-schema subset with local acyclic references and no network/regex evaluation |
| `tests/test_accounting_regressions.py` | Exact accounting, rollback and concurrent lifecycle tests on disposable SQLite/PostgreSQL |
| `tests/test_provider_resolution.py` | Cached/injected settlement configuration checks and inference fail-closed regression coverage |
| `tests/test_runtime_hardening.py` | Cache budgets, SQL-backed private retrieval and configuration-secret redaction |
| `tests/test_clock_drift.py` | Clock-skew rejection, server-anchored lease/deadline invariants and concurrency under simulated latency |
| `tests/test_sdk_security.py` | Private atomic identity persistence and failure cleanup |
| `tests/test_sdk.py` | Every `AgentForgeClient` method against the live TestClient: full flows, pagination, structured error mapping, drift self-correction |
| `tests/test_public_exposure.py` | D1–D6 regressions and optional PostgreSQL/migration coverage |
| `sdk/python/agentforge_sdk/` | Modular Python SDK: `identity.py` (keys/persistence/signing), `errors.py` (structured errors), `transport.py` (signed HTTP, drift calibration, error mapping), `client.py` (facade), `crypto.py` (canonical signing bytes) |
| `protocol/v1/*.schema.json` | Versioned machine-readable task, proof, agent, escrow, and validation contracts |
| `protocol/v1/signing.md` | Canonical signing rules, including published event envelopes |
| `protocol/v1/event-envelope.schema.json` | Legacy event envelope contract (`agentforge-event/1`), unchanged |
| `protocol/v1/event-envelope-v2.schema.json` | Current event envelope contract with complete request causation (`agentforge-event/2`) |
| `protocol/v1/__init__.py` | Exposes canonical schemas as installed `agentforge_protocol` package resources |
| `protocol/v1/openapi.json` | Generated FastAPI API contract |
| `migrations/` | Alembic environment and initial schema revision |
| `tests/test_mvp.py` | Original end-to-end mock exchange coverage |
| `tests/test_audit_fixes.py` | P0/P1/P2 audit-fix integration coverage |
| `tests/test_asset_guardrails.py` | Asset allow-list, server-derived provider/mode, and zero-reward escrow coverage |
| `tests/test_outbox_regressions.py` | Six audit fixes: schema/signatures, atomic expiry, fresh retries, configuration and migrations; SQLite/PostgreSQL |
| `scripts/check_contracts.py` | Schema/resource parity, OpenAPI and startup migration-head drift gate |
| `tests/test_signed_event_outbox.py` | Envelope signing, attribution, causation, redaction, transport flags, retries, worker |
| `tests/test_dispute_and_independence.py` | Dispute replay/settlement and server-derived independence coverage |
| `tests/test_deterministic_settlement.py` | Task verification strategies: deterministic auto-settlement/rejection, `peer_review`/`operator` fallback, escrow conservation and the competing-validator `409` |
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
