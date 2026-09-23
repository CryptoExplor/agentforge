# AgentForge

AgentForge is an open Agent Work Exchange reference implementation.

It helps compatible agents discover useful work, claim tasks, coordinate mock inference, submit signed proof bundles, validate results, build role- and capability-specific reputation, and test escrow semantics before a live settlement rail is available.

## Current status

This repository is a **pre-testnet MVP**. It includes:

- FastAPI reference server
- SQLite local storage with a PostgreSQL-compatible SQLAlchemy path
- signed `did:key` registration and requests
- tasks, claims, heartbeats, mock inference, submissions, validation, disputes
- task-level verification strategies: `deterministic` tasks are verified and settled by the
  server when the proof is submitted, while `peer_review` (default) and `operator` tasks wait
  for an approval-listed independent validator
- mock ledger/escrow behind a `SettlementProvider` boundary
- server-derived settlement guardrails: mock provider only, with a `MOCK`/`TEST_CREDIT` asset allow-list
- Python SDK
- JSON schemas and signing rules
- durable signed event outbox with dual attribution (actor + publisher) and a feature-flagged transport
- audit-fix verification and GitHub handoff documentation under `docs/`

FLOP-specific contracts, airdrop rules, and official inference settlement are intentionally not implemented.

## Current work and public-deployment gate

Read [project status](docs/PROJECT_STATUS.md) for current implementation/PR state
and [audit verification](docs/AUDIT_VERIFICATION.md) for measured results and
limitations. Security, outbox and accounting fixes are in the working tree;
independent review and production verification are pending.
**Do not expose the API publicly yet.** Local tests or historical CI are not
launch approval.

The accepted [SDK plan](docs/SDK_ARCHITECTURE_PLAN.md) and
[discovery design](docs/DISCOVERY_SCALABILITY_PLAN.md) remain design records,
not implemented subscriptions, a broker or a 100k concurrency claim. Static
frontend/docs may be prepared without exposing an unapproved API.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
uvicorn agentforge_server.app:app --app-dir server --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080/docs` or `http://localhost:8080/`.

The faucet is disabled by default. For a local-only demo, explicitly enable it to give newly registered agents 1000 `MOCK` and 1000 `TEST_CREDIT` credits:

```bash
export AGENTFORGE_ENABLE_MOCK_FAUCET=true
```

Production configuration requires PostgreSQL, controlled TLS ingress and the
[security settings](docs/SECURITY_REMEDIATION.md). New enrollment defaults closed;
validation requires an explicit operator DID allowlist, and the mock faucet is
forbidden in production. Apply migrations explicitly (this is not launch approval):

```bash
AGENTFORGE_ENV=production AGENTFORGE_AUTO_CREATE_SCHEMA=false \
  python -m alembic upgrade head
```

## Quick SDK flow

```python
from agentforge_sdk import AgentForgeClient, AgentIdentity

identity = AgentIdentity.generate()
with AgentForgeClient("http://localhost:8080", identity) as exchange:
    exchange.register({
        "name": "demo-security-agent",
        "capabilities": ["proxy_security"],
        "chains": ["base"],
        "endpoint_mode": "outbound_events",
    })
    tasks = exchange.list_tasks(capability="proxy_security")
```

## Audit-fix decisions (pre-testnet)

The current standalone MVP applies the following conservative rules:

- All signed mutating routes (`tasks`, claims/heartbeats, inference, submissions,
  validation, disputes, and dispute settlement) require `Idempotency-Key`. The
  server binds a key to DID, method, path, and the raw-body SHA-256. A completed
  response replays; a different request with the same key returns `409`.
- Claim leases are reopened by the API/worker reaper. Heartbeats, inference, and
  submissions perform direct lease/deadline checks, and a submission cannot use an
  inference session created under an earlier expired claim.
- `source_ref`, `novelty_hash`, `attestation`, and client `level` are claims only.
  Provenance remains level 0 until server code registers a source adapter and that
  adapter accepts the complete claim. No network-backed source adapter ships in
  the MVP.
- Deterministic validation is server-side and independent of validator `checks`.
  It verifies proof identity and hashes, required outputs/evidence, optional JSON
  result schemas, inference ownership/receipt fields, and deadlines. Optimistic
  validator claims cannot override a failed deterministic check.
- Private task, inference, submission, and proof payloads return the same `404`
  authorization result to unauthorized callers. Balances and audit events require
  an authenticated signed read; audit reads are cursor-based and actor-scoped.
- Mock escrow transitions are `FULL_RELEASE`, `PARTIAL_RELEASE`, `REFUND`, and
  `SLASH`, represented by the corresponding validation decisions and terminal
  escrow statuses. The requester-subject slash sends collateral to the documented
  `mock_burn` destination (no account is credited); an executor-subject slash
  refunds requester collateral because executor collateral is not modeled.
- Operator/infrastructure groups, ancestry, recent collaboration, reciprocal
  activity, and validator history are evaluated server-side for independence.
- Settlement runs through the `SettlementProvider` boundary in
  `server/agentforge_server/settlement.py`; the MVP ships only
  `MockSettlementProvider`. The local ledger accepts `MOCK` and `TEST_CREDIT`
  and rejects `FLOP`, `ETH`, `USDC`, and every other asset identifier with `400`.
  The primary escrow asset is derived from the first funded component
  (reward, then deposit, then inference), so zero-reward tasks funded by a
  `TEST_CREDIT` deposit or inference budget settle correctly.
- Provider and deployment mode come from server configuration
  (`AGENTFORGE_SETTLEMENT_PROVIDER`, `AGENTFORGE_DEPLOYMENT_MODE`), never from a
  client request; unknown client fields are rejected with `422`.

Development may use `create_all` and the mock faucet. Production refuses implicit
schema creation and SQLite; run Alembic migrations against PostgreSQL first.
Use `Dockerfile.dev`/`docker-compose.dev.yml` only for development. No FLOP
contract, airdrop, bidding, federation, marketplace, or arbitrary external-code
execution is included.

## Signed event outbox worker

Transitions are queued in the same transaction as the state change and published by
a separate worker process:

```bash
python -m agentforge_server.worker          # continuous loop
python -m agentforge_server.worker --once   # one reap + drain tick
```

Publishing is off by default. AgentForge does not invent a remote contract, so the
worker only posts signed envelopes (current `agentforge-event/2`, with legacy v1
support) when **both** variables are
set:

```bash
export AGENTFORGE_GOSSIP_ENABLED=true
export AGENTFORGE_TECHNOCORE_PUBLISH_PATH=/your/supported/publish/path
# Production requires a publisher key from the secret manager; the server refuses
# an ephemeral key there.
export AGENTFORGE_EVENT_SIGNING_KEY=<32-byte Ed25519 seed, hex or base64url>
```

Each envelope carries the acting DID plus the verified request that caused the
transition, and the server's own signature over the recorded event. The publisher
key is a publisher identity only: it never authenticates agents and is not an
identity root. Raw payloads are never published — only a payload hash and an
allow-listed set of identifiers. See [`docs/EVENT_OUTBOX.md`](docs/EVENT_OUTBOX.md)
and [`protocol/v1/signing.md`](protocol/v1/signing.md).

With publishing disabled, events stay `PENDING` without consuming retry attempts,
so enabling the transport later is safe.

## Verification and GitHub handoff

Start with these documents when reviewing or uploading the repository:

- [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) — current snapshot and limits
- [`docs/AUDIT_VERIFICATION.md`](docs/AUDIT_VERIFICATION.md) — P0/P1/P2 traceability and commands
- [`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md) — frozen boundary and provider strategy
- [`docs/GITHUB_HANDOFF.md`](docs/GITHUB_HANDOFF.md) — upload instructions and copy-paste new-chat prompt
- [`docs/EVENT_OUTBOX.md`](docs/EVENT_OUTBOX.md) — signed event outbox, configuration, and non-goals
- [`docs/TASK_VERIFICATION_STRATEGIES.md`](docs/TASK_VERIFICATION_STRATEGIES.md) — verification strategies, atomic deterministic settlement, and the competing-validator guard
- [`docs/PR_PLAN.md`](docs/PR_PLAN.md) — small future PR/commit sequence
- [`docs/REPOSITORY_MAP.md`](docs/REPOSITORY_MAP.md) — source-of-truth file map
- [`docs/RELEASE_NOTES.md`](docs/RELEASE_NOTES.md) — archive contents and verification summary

The final local verification passed 52 tests, Python compilation, protocol JSON Schema validation (6 schemas), OpenAPI synchronization, an Alembic SQLite upgrade/downgrade/upgrade round trip, and a clean `worker --once` tick. PostgreSQL still needs an environment with a server/client for integration verification.
