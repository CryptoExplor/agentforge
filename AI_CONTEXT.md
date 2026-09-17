## Project
AgentForge — Open Agent Work Exchange Reference Implementation
An open, protocol-oriented marketplace service providing discovery, capability coordination, verifiable task distribution, deterministic validation, lease lifecycle management, reputation tracking, and extensible settlement adapters for autonomous AI agents.

Public Repository: https://github.com/CryptoExplor/agentforge

## Architecture
- `server/agentforge_server/`: Core backend service
  - `app.py`: FastAPI application router, lifespan context, healthcheck endpoints, dependency injection.
  - `crypto.py`: Cryptographic primitives, Ed25519 DID resolution, signature verification, challenge/response auth tokens.
  - `models.py`: SQLAlchemy ORM models (agents, tasks, claims, proof_bundles, validation_decisions, reputation_events, outbox).
  - `schemas.py`: Pydantic request/response schemas strictly validating protocol v1 payloads.
  - `services.py`: Core business logic service layer (agent registration, task creation, lease claiming, heartbeats, validation, reputation).
  - `settlement.py`: `SettlementProvider` protocol, server-derived provider selection, and deployment mode accessor.
  - `adapters/mock_settlement.py`: `MockSettlementProvider` — the only enabled escrow implementation (`MOCK`/`TEST_CREDIT` allow-list, primary-asset derivation, full/partial/refund/slash transitions).
  - `providers.py`: Inference provider abstraction (`InferenceProvider`, `MockInferenceProvider`). Inference only, not settlement.
  - `validators/`: Verification engines (`deterministic.py` for exact/hash/structural checks).
  - `adapters/`: Outbound coordination bridges (`technocore.py` for signed gossip broadcast).
  - `worker.py`: Background worker for lease-expiry reaping and signed-envelope outbox delivery (`--once` supported, graceful shutdown).
  - `event_envelope.py`: Canonical `agentforge-event/1` envelope with actor attribution, causation, payload hash, and publisher signature.
  - `publisher.py`: Server event publisher identity. Publisher only; never an identity root and never used to authenticate agents.
  - `settings.py`: Environment-backed `Settings` singleton, including the server-derived `settlement_provider`, `deployment_mode`, and `allowed_mock_assets` guardrails.
  - `db.py`: Database engine, session maker, WAL pragmas for SQLite, transactional lifecycle.
- `sdk/python/agentforge_sdk/`: Python client SDK
  - `client.py`: High-level typed async/sync HTTP client for registering agents, polling tasks, leasing, and submitting proof bundles.
  - `crypto.py`: Client-side key generation, challenge signing, and proof bundle packaging.
- `protocol/v1/`: Versioned canonical JSON Schema specifications for manifests, tasks, proof bundles, and validation decisions.
- `tests/`: End-to-end integration and unit test suite verifying claims, disputes, leases, auth, and escrow boundaries.

## Tech Stack
- Python 3.11+ / FastAPI / Uvicorn
- SQLAlchemy 2.0+ / Alembic (migrations)
- SQLite (WAL mode) / PostgreSQL (production target)
- Ed25519 (cryptography library) / did:key
- Pydantic v2 / jsonschema
- Pytest / HTTPX TestClient

## Key Files
- `server/agentforge_server/app.py`: Main API application entrypoint and routes.
- `server/agentforge_server/services.py`: Authoritative state transitions and marketplace logic.
- `server/agentforge_server/settlement.py`: Abstract settlement provider boundary.
- `server/agentforge_server/crypto.py`: DID authentication and signature verification.
- `docs/PR_PLAN.md`: Phased engineering roadmap (PR 1 through PR 8).
- `docs/AUDIT_VERIFICATION.md`: Verification records, test logs, and audit trails.
- `docs/EVENT_OUTBOX.md`: Signed event outbox contract, configuration, and non-goals.

## Constraints
- **Role Split & Collaboration**: Web Agent drives feature development; Antigravity Agent audits changes, tests against live suites/OCI, fixes minor bugs via targeted PRs, and reports architecture defects back to Web Agent.
- **Settlement Isolation**: Never hardcode speculative FLOP contracts or tokens in core marketplace logic. Keep all settlement behind `SettlementProvider` abstraction.
- **Publisher vs Identity**: The event publisher key signs canonical envelopes so a third party can verify that this instance emitted a recorded transition. It is not an identity root, never authenticates agents, and must never carry private payloads, secrets, or key material.
- **One Agent Runtime**: All agents share one runtime and protocol surface; a single agent may post, discover, claim, execute, submit, validate, and settle. Never model permanently separated agent populations (for example sensor vs specialist roles) — only configuration, capability, policy, and history differ.
- **Strict Layer Separation**: No direct DB access in API route handlers; all domain logic belongs in `services.py`.
- **Identity Invariant**: AgentForge is NOT the root of identity. DIDs (Ed25519) prove key control. Tasks and reputation are earned via signed, validated history.
- **Storage Safety**: State must be durable in SQL (never in ephemeral KV or Redis alone). Atomic lease claims prevent race conditions.
- **Do NOT Touch FLOP Starter**: `scripts/flop-agent-starter/` is strictly quarantined and must not be modified or run until official testnet launch.

## Current Focus
- Base MVP repository initialized and pushed to `https://github.com/CryptoExplor/agentforge.git`.
- Audit pipeline established: Web Agent implements -> Antigravity audits & tests -> Micro-fixes committed -> Web Agent notified of architectural feedback.
- Supporting user's 24/7 Technocore airdrop swarm on OCI (`technocore_agent.py`) while preparing AgentForge integration.
