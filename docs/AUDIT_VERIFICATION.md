# AgentForge audit-fix verification

**Verification date:** 2026-09-15 (Asia/Calcutta); re-verified on `main` 2026-09-17 (Asia/Calcutta)
**Baseline:** frozen pre-testnet MVP, plus merged PR #1 (settlement provider boundary) and PR #2 (asset and mode guardrails)
**Result:** local verification passed; PostgreSQL execution remains an explicit follow-up because the sandbox had no PostgreSQL tooling

This document is the review map for the P0/P1/P2 audit fixes. It records the implementation boundary without turning future provider work into part of the MVP.

## Verification commands

Run from the repository root after installing the development dependencies:

```bash
python -m pip install -e '.[dev,postgres]'
python -m pytest -q
python -m compileall -q server sdk tests examples

python - <<'PY'
import json
from pathlib import Path
from jsonschema import Draft202012Validator
for path in sorted(Path("protocol/v1").glob("*.schema.json")):
    Draft202012Validator.check_schema(json.loads(path.read_text()))
    print("OK", path)
PY

python - <<'PY'
import json
from pathlib import Path
from agentforge_server.app import app
expected = app.openapi()
actual = json.loads(Path("protocol/v1/openapi.json").read_text())
assert actual == expected, "protocol/v1/openapi.json is stale"
print("OPENAPI_MATCH")
PY

rm -f /tmp/agentforge-downgrade.db
AGENTFORGE_DATABASE_URL=sqlite:////tmp/agentforge-downgrade.db python -m alembic upgrade head
AGENTFORGE_DATABASE_URL=sqlite:////tmp/agentforge-downgrade.db python -m alembic downgrade base
AGENTFORGE_DATABASE_URL=sqlite:////tmp/agentforge-downgrade.db python -m alembic upgrade head
AGENTFORGE_DATABASE_URL=sqlite:////tmp/agentforge-downgrade.db python -m alembic current
```

The 2026-09-15 baseline run produced `16 passed`, compile success, all five JSON
Schemas valid, OpenAPI match, and Alembic revision `3293de03bb66` after the
downgrade/re-upgrade round trip, with one non-failing Starlette/httpx
deprecation warning.

Re-running the same commands on `main` after PR #1 and PR #2 were merged
(2026-09-17, Python 3.11.2) produced:

| Check | Result |
|---|---|
| `python -m pytest -q` | `24 passed`, 2 warnings |
| `python -m compileall -q server sdk tests examples` | passed |
| JSON Schema meta-validation (`protocol/v1/*.schema.json`) | 5 of 5 valid |
| Generated OpenAPI vs `protocol/v1/openapi.json` | `OPENAPI_MATCH`, 22 paths |
| `alembic upgrade head -> downgrade base -> upgrade head` | `3293de03bb66 (head)` |
| `git diff --check` | clean |
| GitHub Actions CI on `main` | success (run `35153608459`) |

Both warnings are the non-failing Starlette/httpx and `anyio.abc.BlockingPortal`
deprecations already noted for the baseline. No test outcome depends on them.

## Requirement traceability

| Audit requirement | Implementation surface | Regression coverage |
|---|---|---|
| Claim lease expiry, reopen, reaper, direct expiry checks, late-submission rejection | `server/agentforge_server/app.py`, `services.py`, `worker.py` | `test_claim_expiry_reopens_and_rejects_old_work` |
| Persistent idempotency replay/conflict for task creation, claims, inference, submissions, validation, disputes, settlement | `IdempotencyRecord` in `models.py`; request wrapper in `app.py`/`services.py` | `test_idempotency_replay_and_conflict_cover_the_mutating_flow`, `test_dispute_open_and_resolution_are_idempotent` |
| Conservative provenance; client source claims remain level 0 | `server/agentforge_server/provenance.py`, server-derived task fields | `test_provenance_claims_stay_level_zero_until_a_registered_adapter` |
| Independent deterministic validation | `server/agentforge_server/validators/deterministic.py` | `test_deterministic_acceptance_rejects_validator_claims_and_tampered_proofs`, `test_full_mock_exchange` |
| Hash, schema, acceptance, evidence, ownership, receipt, deadline, and structural checks | deterministic validator plus `ValidationDecision.deterministic_checks` | same deterministic validation test |
| Private task/inference/submission/proof/balance/audit authorization | authenticated read helpers and actor checks in `app.py` | `test_private_payloads_sessions_proofs_and_balances_require_authentication`, `test_cursor_audit_pagination_is_authenticated_and_stable` |
| Explicit mock full/partial/refund/slash transitions | `services.escrow_settle`, escrow schema, ledger/audit events | `test_mock_escrow_transitions_conserve_value_and_cannot_double_settle` |
| Settlement provider boundary isolated from core marketplace logic | `server/agentforge_server/settlement.py`, `adapters/mock_settlement.py`, thin `services.fund_task`/`services.escrow_settle` wrappers | full suite unchanged; `test_mock_escrow_transitions_conserve_value_and_cannot_double_settle` |
| Server-derived provider/deployment mode and asset allow-list | `settings.py`, `settlement.get_settlement_provider`, `MockSettlementProvider.fund` | `test_mock_provider_rejects_flop_asset`, `test_mock_provider_rejects_unknown_assets`, `test_mock_provider_accepts_mock_and_test_credit`, `test_client_cannot_choose_network_or_provider_mode`, `test_deployment_and_settlement_mode_are_server_derived`, `test_unsupported_settlement_provider_raises` |
| Primary escrow asset derived from the first funded component (zero-reward tasks with a `TEST_CREDIT` deposit or inference budget) | `MockSettlementProvider.fund` | `test_zero_reward_task_with_test_credit_deposit` |
| Documented slash behavior | `mock_burn` destination for requester-subject slash; no account credited | same escrow test and `README.md` |
| Database-safe active-claim invariant/race handling | partial unique `uq_active_task_claim` index plus claim transaction | `test_active_claim_index_and_outbox_leases_are_database_safe` |
| Leased outbox delivery | `server/agentforge_server/outbox.py` | `test_active_claim_index_and_outbox_leases_are_database_safe` |
| Cursor audit pagination | authenticated `(created_at, id)` cursor | `test_cursor_audit_pagination_is_authenticated_and_stable` |
| Alembic migration and production refusal of implicit SQLite schema creation | `migrations/`, `db.py`, startup guard | `test_production_does_not_auto_create_sqlite_schema`, `test_alembic_initial_schema_is_reproducible`, manual round trip |
| Persisted inference `submission_id` | `InferenceSession.submission_id`; submission attachment path | `test_idempotency_replay_and_conflict_cover_the_mutating_flow`, deterministic validation test |
| Deterministic acceptance checks | required outputs/evidence, expected outputs, JSON Schema in deterministic validator | deterministic validation test |
| Server-derived independence | operator/infrastructure groups, ancestry, reciprocal history, validator conflict checks | `test_server_side_group_independence_blocks_executor_and_validator`, `test_ancestry_and_reciprocal_history_are_server_derived` |
| Private balances and Decimal accounting | authenticated balance route; string amounts and `Decimal` service calculations | full exchange and escrow transition tests |
| Expanded dispute/independence coverage | dispute routes, resolution, history-derived exclusions | all three tests in `test_dispute_and_independence.py` |
| Separated Docker configuration | `Dockerfile`, `Dockerfile.dev`, `docker-compose.yml`, `docker-compose.dev.yml` | configuration review; production uses explicit Alembic and no faucet |

## Frozen behavior decisions

### Idempotency

Signed mutating routes require `Idempotency-Key`. The server binds the key to the authenticated DID, method, path, and raw-body hash. A completed identical request replays the stored response. A key reused for a different bound request returns `409`.

### Expiry

Expiry is fail-closed. The API checks the current lease/deadline on heartbeats, inference creation, and submission. A reaper marks expired claims and reopens eligible tasks. A submission cannot attach an inference session from an earlier expired claim.

### Provenance and eligibility

`source_ref`, `novelty_hash`, `attestation`, and client-provided `level` are claims/evidence. Only a registered server-side source adapter can raise trusted provenance above level zero. `DEMO_ONLY`, `REPUTATION_ELIGIBLE`, `ECONOMIC_ELIGIBLE`, and `EXTERNAL_NETWORK_VERIFIED` are separate server-derived concepts; clients cannot choose them.

### Deterministic validation

Validator-supplied `checks` are not authoritative. Deterministic checks run independently and a fatal deterministic failure prevents a positive settlement decision. The implementation validates stored proof identity and hashes, acceptance requirements, result schemas, evidence structure, inference ownership/receipt integrity, and deadlines.

### Mock settlement

Only local test assets such as `MOCK` and `TEST_CREDIT` belong in the mock ledger. The supported transitions are mutually exclusive and idempotent: `FULL_RELEASE`, `PARTIAL_RELEASE`, `REFUND`, and `SLASH`. A requester-subject slash records the documented `mock_burn` destination and credits no account; executor collateral is not modeled.

Escrow now lives behind the `SettlementProvider` protocol in `settlement.py`, with `MockSettlementProvider` as the only enabled implementation. `fund()` derives the primary escrow asset from the first funded component in the order reward, deposit, inference, so a zero-reward task with a `TEST_CREDIT` security deposit reserves `TEST_CREDIT` rather than failing against the default `MOCK` reward asset. Every component that requests an asset must name a member of the server allow-list (`MOCK`, `TEST_CREDIT`), and every component with a non-zero amount must match the primary asset. Provider selection is server-derived from `AGENTFORGE_SETTLEMENT_PROVIDER`; an unsupported value raises at call time instead of falling back to mock, and deployment mode comes from `AGENTFORGE_DEPLOYMENT_MODE`.

## Not validated here

- No PostgreSQL server was available in the sandbox. Run the migration and integration suite against PostgreSQL before accepting real private workloads.
- No official FLOP testnet interface was used, because no official stable provider contract was available in the frozen scope.
- No TCLK adapter was implemented or treated as a value rail.

These are explicit boundaries, not hidden failures.
