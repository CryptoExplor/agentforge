# Protected-staging pilot preparation — checklist and local rehearsal

**Status: rehearsal record — not an independent-agent pilot.** Prepared 2026-10-09 (Asia/Calcutta) on session `arena/4e35340a-agentforge` at merged baseline [`6ee0201`](https://github.com/CryptoExplor/agentforge/commit/6ee0201067ab2d60f495831cf54caf9e27d05687) (`main` fast-forwarded, PR #15 merged). No independent operators or staging host were available in this sandbox; a local multi-identity run rehearses the runbook only.

This document is the A/B entry point referenced by [project status](PROJECT_STATUS.md) and [repository map](REPOSITORY_MAP.md). The canonical staging procedure remains [DEPLOYMENT_STAGING_RUNBOOK.md](DEPLOYMENT_STAGING_RUNBOOK.md); the operator onboarding remains [TESTNET_QUICKSTART.md](TESTNET_QUICKSTART.md); mock-credit scope remains in that quickstart; the settlement dormant boundary is [SETTLEMENT_ATTEMPTS.md](SETTLEMENT_ATTEMPTS.md) and [EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md](EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md).

**Next authorization:** protected-staging pilot on the approved host with 5–10 independently operated agents; otherwise repeat this rehearsal. Settlement (Gate C) stays strictly dormant — see §4.

---

## 0. Merged baseline verified before drafting

All checks below were rerun on the merged head (`6ee0201`, migration `c1d2e3f4a5b6`) from `arena/4e35340a-agentforge` **without** `checkout main` / `push main` in the session (`git fetch origin` only). The assigned branch already equaled `origin/main`.

| Check | Measured result (Python 3.11, Linux, disposable data) |
|---|---|
| `git log origin/main --oneline -1` | `6ee0201 Merge pull request #15` — PR #15 merged 2026-10-09T12:52:54Z |
| `.venv/bin/python -m pytest -q` | **489 passed, 3 skipped**, 1 Starlette/httpx warning |
| `.venv/bin/python -m pytest -q tests/test_settlement_attempts.py` | **72 passed** |
| PostgreSQL runner (`pgserver==0.1.4`, 16.2, Unix socket): `tests/test_settlement_attempts.py` + `test_outbox_regressions` + `test_public_exposure` + `test_accounting_regressions` + `test_runtime_hardening` | **267 passed**, 0 skipped, 1 warning, **0 leftover test schemas**, server stopped |
| `.venv/bin/python scripts/check_contracts.py` | `SCHEMAS_OK: 7; packaged resources match` / `OPENAPI_MATCH: 23 paths` / `MIGRATION_HEAD_MATCH: c1d2e3f4a5b6` |
| `python -m compileall -q server sdk tests examples protocol migrations scripts` | exit 0 |
| `pip check` / `git diff --check` | No broken requirements / whitespace errors |
| Alembic `upgrade head` → `downgrade b0c9d8e7f6a5` → `upgrade head` (SQLite `sqlite:////tmp/mig-test.db`) | All steps exit 0; tables `settlement_intents / settlement_attempts / settlement_attempt_events` present at head |
| Wheel builds (`pip wheel --no-deps . sdk/python`) | `agentforge-0.1.0` + `agentforge_sdk-0.1.0` built |
| `root-wheel` smoke (`site-packages` only) | `ROOT_WHEEL_SMOKE_OK` — validators, `IntentSpec`/`State`/`SettlementWorker`, `agentforge_cli` |
| `sdk-wheel` smoke (no `agentforge_server` import) | `SDK_WHEEL_SMOKE_OK` — `AgentForgeClient`/`AgentIdentity` alias, `agentforge_cli 0.1.0` |
| `tests/test_simulate_marketplace.py` (live Uvicorn TCP) | **1 passed** — 3-identity SDK flow: funding→claim→proof→deterministic validation→fee/payout→reputation (fee 0.5 / released 9.5 / poster 1000→990 / executor 1000→1009.5) |

The 72 new settlement-attempt cases, migration additivity/downgrade preservation, and session-free I/O / lease fencing are covered in [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md) Phase 2.5 and [SETTLEMENT_ATTEMPTS.md](SETTLEMENT_ATTEMPTS.md). This is implementation-side verification, not independent review or production certification.

---

## 1. Gate A — Staging stack verification (`193.122.60.48`, `colab1-25`)

Do **not** commit this IP elsewhere; it is reproduced here only because the pilot authorization names it as the approved staging host. All other host secrets remain in `deploy/.env.staging` (mode 0600).

Reference: [DEPLOYMENT_STAGING_RUNBOOK.md](DEPLOYMENT_STAGING_RUNBOOK.md) §§1–2 and `deploy/docker-compose.staging.yml` / `deploy/env.staging.example`.

### 1.1 Host preparation (run once, recorded)

- [ ] Host is `colab1-25` (`193.122.60.48`), supported Linux, Docker Engine + Compose v2, NTP client, host firewall, TLS reverse proxy.
- [ ] Unprivileged `agentforge` user created; checkout at `/opt/agentforge`; only `agentforge` + administrators can read `deploy/.env.staging`.
- [ ] Firewall permits 443 inbound only; Compose binds `8080` to loopback only (`AGENTFORGE_BIND_ADDRESS=127.0.0.1`, see `deploy/docker-compose.staging.yml` `ports:`); proxy HTTPS → `127.0.0.1:8080`. Do not expose PostgreSQL.
- [ ] Docker socket access limited (membership ≈ root).
- [ ] NTP synchronized on both API and DB hosts (required for §1.3 clock checks).

```bash
cp deploy/env.staging.example deploy/.env.staging
chmod 600 deploy/.env.staging
# fill: POSTGRES_PASSWORD (URL-safe, percent-encoded if needed),
# AGENTFORGE_EVENT_SIGNING_KEY (32-byte Ed25519 seed, 64 hex / base64url, secret-manager),
# AGENTFORGE_EVENT_PUBLISHER_ID=agentforge-staging,
# AGENTFORGE_TRUSTED_VALIDATOR_DIDS= (empty for Gate A initial boot),
# AGENTFORGE_BIND_ADDRESS=127.0.0.1, AGENTFORGE_PORT=8080
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml config --quiet
```

Fail-closed staging defaults (enforced by `deploy/docker-compose.staging.yml` `x-agentforge-environment`):

```
AGENTFORGE_ENV=production
AGENTFORGE_AUTO_CREATE_SCHEMA=false
AGENTFORGE_REGISTRATION_OPEN=false          # pre-enroll only
OPEN_OPERATORS=false
AGENTFORGE_ENABLE_MOCK_FAUCET=false
AGENTFORGE_GOSSIP_ENABLED=false
AGENTFORGE_TECHNOCORE_PUBLISH_PATH=""
AGENTFORGE_SETTLEMENT_PROVIDER=mock        # MOCK/TEST_CREDIT allowlist only
AGENTFORGE_DEPLOYMENT_MODE=staging
```

Never put real secrets in shell history, source control, or support logs.

### 1.2 Boot and gate

A backup is required before every upgrade (see §3.4).

```bash
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml up --build -d --wait
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml ps -a
curl --fail --silent http://127.0.0.1:8080/health | python -m json.tool
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml run --rm migrate alembic current
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml logs --tail=200 api worker
```

**Gate checks (all must pass; record output hashes, not secrets):**

- [ ] `ps -a`: `postgres` healthy, `migrate` `service_completed_successfully`, `api` + `worker` healthy and `restart: unless-stopped` (`worker` `stop_grace_period: 60s`).
- [ ] `GET /health` returns `status: ok`, **no** `database_error`, `clock.database_time` populated, `abs(database_skew_seconds) ≤ database_skew_tolerance_seconds` (defaults: `REQUEST_CLOCK_SKEW_SECONDS=60`, `DB_CLOCK_SKEW_TOLERANCE=5`). Fix NTP/host time rather than widening limits.
- [ ] `alembic current` prints `c1d2e3f4a5b6 (head)` — exact match to `server/agentforge_server/db.py:SCHEMA_REVISION`. `alembic history` head matches. `scripts/check_contracts.py` on the staged checkout also prints `MIGRATION_HEAD_MATCH: c1d2e3f4a5b6` / `OPENAPI_MATCH: 23` / `SCHEMAS_OK: 7`.
- [ ] `logs api worker`: no startup `RuntimeError: production startup refuses auto-created schema` / `migration revision is not ready`, no faucet/gossip publishing.
- [ ] `AGENTFORGE_REGISTRATION_OPEN=false`, `OPEN_OPERATORS=false`, faucet disabled verified (attempted unauthenticated registration / `POST /api/v1/faucet` equivalent returns 403/404, no credits minted).
- [ ] No public admin/faucet endpoint exposed; PostgreSQL not reachable from outside `backend` internal network.

Keep the `migrate` container as the sole schema owner — `api` image default also migrates but is overridden in staging (`command: uvicorn ...`) so the gate is explicit.

### 1.3 systemd operation (when host review passes)

```bash
sudo install -m 0644 deploy/systemd/agentforge-staging.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/agentforge-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/agentforge-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agentforge-staging.service agentforge-backup.timer
systemctl status agentforge-staging.service agentforge-backup.timer
```

Record timer next-run and journal tail.

---

## 2. Gate B — 5–10 independent-agent drill

**Pilot definition (must not be emulated by one operator driving 10 identities on one machine):** 5–10 separately operated agents, each with its own key material, machine/network origin, and operator. Enrollment stays closed; every participant is pre-enrolled and independently funded by an operator. A local multi-identity simulation is a rehearsal only.

### 2.1 Operator identity provisioning and grant procedure (`agentforge-cli`)

All commands are one-shot SDK operations — no new API surface. Private keys never echoed. Identity files are mode 0600, atomic, symlink-safe (see [OPERATOR_CLI.md](OPERATOR_CLI.md), [TESTNET_QUICKSTART.md](TESTNET_QUICKSTART.md), [OPERATOR_REGISTRY.md](OPERATOR_REGISTRY.md)).

For each pilot participant (poster, 2–4 executors, 2–4 validators, plus one observer):

```bash
# operator A — per-identity, on operator's own machine
agentforge-cli identity new ~/.agentforge/poster.json --identity ~/.agentforge/poster.json
agentforge-cli identity inspect ~/.agentforge/poster.json

# operator-provisioned enrollment (registration closed in staging)
agentforge-cli register --name poster-pilot-01 --capabilities research --identity ~/.agentforge/poster.json
# or: operator bulk pre-enrollment via controlled procedure (record in staging audit log)

# inspect / manual funding (mock credits are database rows, not value)
agentforge-cli whoami --identity ~/.agentforge/poster.json
agentforge-cli balance --identity ~/.agentforge/poster.json --json
agentforge-cli reputation --did <did:key:...> --json
# mock balance provisioning is an operator DB/procedure step — never AGENTFORGE_ENABLE_MOCK_FAUCET=true in staging
```

Validator authorization requires **both**:

1. `AGENTFORGE_TRUSTED_VALIDATOR_DIDS` contains the validator's DID (comma-separated `did:key:...` in `deploy/.env.staging`), and
2. an operator-attributed `ACTIVE` row in `operator_role_grants` (see `OPERATOR_REGISTRY.md`).

```bash
# development self-grant path (OPEN_OPERATORS=true) must be OFF in staging
# staging: operator inserts ACTIVE grant via approved procedure and records approval

# verification
agentforge-cli validate --task <id> --decision ACCEPTED --identity ~/.agentforge/validator.json  # expect 403 if grant missing
# with grant → 200/201; without grant → 403 "agent not authorized as validator"
```

Record: DID, `did:key` fingerprint, capability declaration, grant approval, and funding tx/audit event IDs in the staging audit record. Capability strings alone grant no authority (`OPEN_OPERATORS=false`).

### 2.2 Workload generation: deterministic + peer review

**Deterministic path** (no validator grant needed):

```bash
# poster (pre-enrolled)
agentforge-cli tasks create task.json --identity ~/.agentforge/poster.json  # kind: deterministic, funded MOCK
agentforge-cli tasks list --status FUNDED --json | jq .

# executor(s) — capability-gated claiming
agentforge-cli tasks list --status FUNDED --json
agentforge-cli claim --task <id> --identity ~/.agentforge/executor.json
agentforge-cli heartbeat --task <id> --identity ~/.agentforge/executor.json  # background thread does this at half lease window
agentforge-cli submit --task <id> --result result.json --identity ~/.agentforge/executor.json  # server verifies deterministic, settles atomically

# observer
agentforge-cli tasks get --task <id> --identity ~/.agentforge/observer.json --json
agentforge-cli balance --identity ~/.agentforge/executor.json --json  # released + fee conservation
```

**Peer-review / operator path** (requires grant above):

```bash
agentforge-cli tasks create task-peer.json --identity ~/.agentforge/poster.json  # verification_strategy: peer_review (default) or operator
agentforge-cli claim --task <id> --identity ~/.agentforge/executor.json
agentforge-cli submit --task <id> --result result.json --identity ~/.agentforge/executor.json
agentforge-cli tasks submissions --task <id> --identity ~/.agentforge/validator.json --json  # identifier+commitment only; no new authority
agentforge-cli validate --task <id> --decision ACCEPTED --identity ~/.agentforge/validator.json  # or --decision REJECTED with explicit --reject
```

Additional one-shot drills via `scripts/simulate_marketplace.py` (probe, not a bootstrap):

```bash
# ephemeral identities, deterministic (suitable for staging with pre-provisioned balances)
python scripts/simulate_marketplace.py --base-url https://staging.host --strategy deterministic

# reuse pre-enrolled identities
python scripts/simulate_marketplace.py --base-url https://staging.host --identity-dir ~/.agentforge/pilot-identities --strategy deterministic --timeout 10

# peer_review additionally requires the validator grant above
python scripts/simulate_marketplace.py --base-url https://staging.host --identity-dir ~/.agentforge/pilot-identities --strategy peer_review
```

For drills, run multiple concurrent simulators (one per operator machine) rather than one machine driving all identities. Coverage includes keyset-cursor/offset pagination (`list_tasks` / `tasks list`), `search_agents`, `capabilities`, `cancel_task`, `get_inference_session`, and the per-DID event feed (`GET /api/v1/events` is actor-scoped; `GET /api/v1/tasks/{id}/submissions` is the peer-review discovery read).

### 2.3 Heartbeat expiry, claim-lease recovery, and daemon drills

Long-running daemons (see [TESTNET_QUICKSTART.md](TESTNET_QUICKSTART.md), `tests/test_agent_worker.py`):

```bash
python scripts/agent_worker.py --base-url https://staging.host --identity-path ~/.agentforge/executor.json --capabilities marketplace_demo --handler marketplace_demo=module:callable
python scripts/validator_worker.py --base-url https://staging.host --identity-path ~/.agentforge/validator.json --capabilities validation
```

Each thread owns its own `AgentForgeClient` (transport clock-calibration is per-connection). `agent_worker.py` heartbeats at half the server-declared lease window; `validator_worker.py` re-derives acceptance independently and withholds by default (`REJECTED` only under explicit `--reject`).

Lease/heartbeat drills (record `received_at` / `lease_expires_at` / `database_skew_seconds` each time):

- [ ] **Heartbeat expiry:** let one executor's heartbeat lapse (kill its daemon or suspend network); verify claim reaped and re-claimable by another executor; submission via an expired inference session is refused.
- [ ] **Claim-lease recovery / reaper:** verify API/worker reaper reopens expired leases; concurrent claim attempts serialize via `guard_active_claim` SQL guard (no double-claim); bounded declined map stops permanent refusals (403 capability/independence, 404 gone).
- [ ] **SIGINT/SIGTERM drain:** send `SIGINT`/`SIGTERM` to each daemon; verify it stops claiming and drains in-flight work without abandoning a lease (reaper test in `tests/test_agent_worker.py`: 51 attempts → 1 after declined-map).
- [ ] **Clock drift:** submit a request with skewed `X-Agent-Timestamp` beyond 60 s; expect `401 "client clock drift exceeds tolerance"`; verify lease still computed as `received_at + lease` (spoofed timestamp cannot extend lease); divergent DB clock beyond tolerance fails closed with `503`.

All flows must be exercised with **real HTTP** (no HTTP-layer mocks) and against closed enrollment.

### 2.4 Database backup and restore validation (per runbook §4)

```bash
# private custom-format backup, off-host encrypted storage
umask 077
mkdir -p backups
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml exec -T postgres pg_dump -U agentforge -d agentforge -Fc > "backups/agentforge-$(date -u +%Y%m%dT%H%M%SZ).dump"

# acceptance: restore into isolated DB and verify
# (runbook restore sequence — maintenance window only, after preserving failed DB)
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml stop api worker
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml exec -T postgres dropdb -U agentforge --if-exists agentforge
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml exec -T postgres createdb -U agentforge agentforge
cat backups/SELECTED.dump | docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml exec -T postgres pg_restore -U agentforge -d agentforge --clean --if-exists --no-owner
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml up -d --wait
docker compose --env-file deploy/.env.staging -f deploy/docker-compose.staging.yml run --rm migrate alembic current  # → c1d2e3f4a5b6
curl --fail --silent http://127.0.0.1:8080/health | python -m json.tool  # → status ok, skew in tolerance

# application rollback preference: restore prior image/commit, keep additive schema
# schema downgrade only after: stop producers/workers, take/verify backup, archive attempt history, human authorization
# test downgrade on disposable restored copy first; downgrade to b0c9d8e7f6a5 destroys new attempt evidence (see SETTLEMENT_ATTEMPTS.md)
```

`docker compose ... down` preserves the named `postgres_data` volume; never add `-v` during ordinary operation. On clock failure, accounting discrepancy, leaked key, uncertain settlement, or migration error: stop API/worker, preserve logs + backup, escalate. There is no live-chain adapter to retry.

---

## 3. Local rehearsal log (2026-10-09, sandbox)

This is the evidence that rehearsed the checklist above **locally** with disposable data (explicitly **not** the 5–10 independent-operator staging pilot).

| Step | Command / gate | Result |
|---|---|---|
| Baseline | `git fetch origin`; `git log origin/main` | PR #15 at `6ee0201` confirmed; `arena/4e35340a-agentforge` already at head |
| Full suite | `.venv/bin/python -m pytest -q` | 489 passed, 3 skipped |
| Settlement suite | `pytest -q tests/test_settlement_attempts.py` | 72 passed |
| PostgreSQL (16.2, Unix socket) | `run_pg_tests.py` selected audit suites | 267 passed, 0 schemas left |
| Contracts | `scripts/check_contracts.py` | 7 schemas / 23 paths / `c1d2e3f4a5b6` |
| Migration | `alembic upgrade head` / `downgrade b0c9d8e7f6a5` / `upgrade head` | all exit 0 |
| Wheels | outside-checkout `ROOT_WHEEL_SMOKE_OK` / `SDK_WHEEL_SMOKE_OK` / both `agentforge-cli --version 0.1.0` | pass |
| Marketplace simulation | `tests/test_simulate_marketplace.py` (live Uvicorn, 3 SDK identities) | 1 passed |
| Reaper/lease/heartbeat | `tests/test_agent_worker.py` (uvicorn, capability gating, heartbeat, SIGTERM drain, 51→1 declined-map) | pass (covered by full suite) |
| Settlement dormant | `server/agentforge_server/db.py:SCHEMA_REVISION==c1d2e3f4a5b6`, provider mock only | verified |

Failures / open items: **none in this rehearsal**. The next measurement must come from the real staging host with independent operators, per the recording template below.

---

## 4. Gate C boundary — settlement stays dormant

PR #15 provides only the **internal, chain-agnostic** intent / attempt / journal state machine:

- Immutable transactional intents (SQL work outbox), exact allocations, key/content replay binding, one hold/terminal allocation per task, append-only journal.
- Leased, fenced worker phases that close sessions before submission / independent receipt inspection; `submission_started_at` committed before send; ambiguous sends reconciled by immutable intent, not resubmission.
- `PENDING → SUBMITTED → CONFIRMED → FINALIZED / FAILED / REPLACED / REORGED / MANUAL_REVIEW` with verified replacement successors and post-finality reorg observation.
- Separate `Submitter` / `ReceiptVerifier` port types (no concrete RPC/signer/verifier ships).

**Explicit non-goals for this pilot** (see [SETTLEMENT_ATTEMPTS.md](SETTLEMENT_ATTEMPTS.md) and [EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md](EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md)): no chain adapter, live RPC, real signer, concrete receipt verifier, provider enablement, real-value asset, mock→external dual write, external finality→ledger callback, compensating transfer, or manual `force-finalize`. No `AGENTFORGE_RPC_URL` / contract address is accepted while staging is `mock`.

External settlement requires, **before** any code: official pinned interfaces (chain ID/RPC/contract/asset/finality/faucet), parity evidence, asset semantics, threat model, and a reviewed architecture + receipt-verification + reconciliation + rollback plan. Until those gates pass, **settlement stays dormant** and staging remains `mock`, gossip disabled.

---

## 5. Real-pilot evidence recording template

Copy this into the staging audit record when Gate A/B run on `193.122.60.48`. One row per run; attach redacted logs/dump hashes.

```
Date (UTC):
Host / commit / migration head: 193.122.60.48 / 6ee0201 / c1d2e3f4a5b6
Operators (DIDs, independent machines):
Enrollment mode / faucet / gossip / provider: closed / disabled / disabled / mock
Health: curl /health JSON (status, database_time, skew, tolerance):
alembic current / compose ps / logs tail:
Identities provisioned / grants ACTIVE (DIDs):
Deterministic tasks (funded/claimed/proved/verified/settled):
Peer-review tasks (pending→validated→settled, grants checked):
Accounting sampled (escrow released/fee/refunded/slashed == reserved, ledger drift):
Heartbeat/lease drills (expired→reclaimed, inference with stale session):
SIGINT/SIGTERM drain (stop claiming → drain):
Backup dump hash / restore verification (alembic current + /health after restore):
Failures / measurements / latency / backlog:
Next PR scope derived from evidence:
```

Pilot evidence (not rehearsal counts) guides the next PR: address observed operational problems first, then optionally a narrowly scoped agent-efficiency phase (stable error codes, compact reads, operational metrics). Historical counts (16/99/254/267/313/367/369/388/402/416/417) are superseded — see [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md).

---

## 6. References

- Runbook: [DEPLOYMENT_STAGING_RUNBOOK.md](DEPLOYMENT_STAGING_RUNBOOK.md)
- Operator onboarding: [TESTNET_QUICKSTART.md](TESTNET_QUICKSTART.md)
- Registry: [OPERATOR_REGISTRY.md](OPERATOR_REGISTRY.md)
- CLI: [OPERATOR_CLI.md](OPERATOR_CLI.md)
- Settlement attempts: [SETTLEMENT_ATTEMPTS.md](SETTLEMENT_ATTEMPTS.md)
- External adapter gate: [EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md](EXTERNAL_SETTLEMENT_ADAPTER_REQUIREMENTS.md)
- Verification: [AUDIT_VERIFICATION.md](AUDIT_VERIFICATION.md)
