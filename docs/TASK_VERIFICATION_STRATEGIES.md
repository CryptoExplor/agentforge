# Task verification strategies and deterministic auto-settlement

**Status:** implemented — introduced in Phase 1.1 (Alembic revision `e7f8a9b0c1d2`); current Alembic head is **`b0c9d8e7f6a5`** (Phase 1.4)
**Scope:** who decides a task outcome, and when escrow moves
**Source modules (post-Phase-1.5):** proof submission + deterministic auto-settlement in `server/agentforge_server/routes/submissions.py`; the peer-review/operator settlement path (`apply_validation`) in `server/agentforge_server/routes/validations.py`; the independent checks in `server/agentforge_server/validators/deterministic.py` (`evaluate_deterministic`); escrow movement in `server/agentforge_server/services.py` → `adapters/mock_settlement.py`.
**Related:** [`ARCHITECTURE_DECISIONS.md`](ARCHITECTURE_DECISIONS.md), [`SECURITY_REMEDIATION.md`](SECURITY_REMEDIATION.md), [`ACCOUNTING_REMEDIATION.md`](ACCOUNTING_REMEDIATION.md), [`../protocol/v1/task.schema.json`](../protocol/v1/task.schema.json)

## Why

Every task previously waited for a third party: `POST /api/v1/submissions/{id}/validate`
had to be called by an approval-listed, independent validator before anything
settled. For work the server can check by itself — an exact result hash, a JSON
result schema, required evidence, inference receipts, a deadline — that wait was
artificial latency, it exposed simple tasks to validator starvation (and to
griefing by a validator who simply never arrived), and it blocked automated
pipeline use.

A task now declares a **verification strategy**, and a deterministic task is
verified and settled by the server in the same transaction that accepts the
proof bundle.

## Strategy values

| Value | Who decides | Settlement |
|---|---|---|
| `deterministic` | The server, by evaluating the declarative acceptance criteria on submission | In the submission transaction: `VERIFIED` releases the reward; `REJECTED` refunds the requester |
| `peer_review` *(default)* | An approval-listed independent validator with a signed decision | After `POST /api/v1/submissions/{id}/validate` |
| `operator` | Operator review; behaves like `peer_review` today | After a signed decision |

`verification_strategy` is chosen by the poster at creation time, stored on the
`tasks` row, and returned in the task representation. The default is
`peer_review`, so every pre-existing task and every client that does not send the
field keeps the manual path. `kind` and `verification_strategy` are independent:
a task may be `kind: "deterministic"` and still ask for peer review, or vice
versa. Unknown stored values fall back to `peer_review` rather than auto-settling.

## What a deterministic verdict checks

The deterministic tier is the same independent evaluation validators cannot
override; it is not a shortcut around it:

- stored acceptance-hash reproducibility;
- proof identity and signed fields against the stored submission;
- input hash, result hash and proof hash;
- declared `expected_result_hash` (a poster commitment to the exact result), when present;
- `result_schema` / `output_schema` / `schema` result validation, when present;
- required outputs and evidence structure;
- attached inference sessions: ownership, receipt identity, model reference, result hash and compute fields;
- submission before the task deadline.

Checks, not trust: an executor cannot supply the verdict, and a failing check is
never silently accepted because a proof was signed.

## Atomic settlement

For a `deterministic` task, `POST /api/v1/tasks/{id}/submissions` performs, in one
database transaction:

1. deterministic evaluation (`validators.evaluate_deterministic`);
2. `submission.status` and `task.status` set to `VERIFIED` or `REJECTED`;
3. escrow settlement through the provider boundary
   (`services.settle_escrow` → `escrow_settle` → `MockSettlementProvider`):
   `FULL_RELEASE` to the executor on `VERIFIED`, `REFUND` to the requester on
   `REJECTED`, with exact `Decimal`/string accounting and the existing ledger
   idempotency keys;
4. reputation event: `task_verified` `+1.0` or `task_rejected` `-1.0` for the executor;
5. the claim is completed (`COMPLETED`) because the task is terminal;
6. audit event (`TASK_VERIFIED` / `TASK_REJECTED`, including the failed check codes for the actor);
7. an outbox event with the request actor plus verified causation, naming both
   counterparties (`poster_did`, `executor_did`) and the task/submission identifiers.

Nothing is committed until the submission response is stored, so a settlement
conflict (for example an escrow that is already terminal) aborts the whole
submission instead of leaving a half-applied state. A tampered proof signature is
still rejected with `401` before any of this runs.

A failed deterministic check is a **refund, not a server error**: the requester is
made whole and the executor is not paid.

## Terminality and the competing-validator guard

Deterministic settlement is terminal. `POST /api/v1/submissions/{id}/validate`
(and the shared `apply_validation` used by dispute resolution) returns:

```text
409 task has already settled via deterministic strategy
```

for an already auto-settled deterministic task, so a late validator cannot
re-decide a verified task, re-open a rejected one, or double-settle escrow. A
dispute cannot be opened on the terminal submission either; it is refused as
already terminal.

## Migration

Alembic revision **`e7f8a9b0c1d2`** adds `tasks.verification_strategy`
(`String(32)`, `NOT NULL`, `server_default='peer_review'`) on top of
`d6e7f8a9b0c1`. Existing rows are backfilled with the safe manual strategy, and
the server default is retained so raw SQL inserts and rolling deploys running the
previous application version stay valid. The statement is portable: PostgreSQL
receives a plain `ALTER TABLE ... ADD COLUMN`; SQLite uses Alembic's batch table
copy, which recreates the existing indexes and foreign key.
`server/agentforge_server/db.py::SCHEMA_REVISION` tracks the head, and
`scripts/check_contracts.py` fails CI if migration head and code diverge.

> Note: `e7f8a9b0c1d2` was the head *when this phase landed*. Later phases
> advanced the chain to the current head **`b0c9d8e7f6a5`**
> (`e7f8a9b0c1d2 → f8a9b0c1d2e3 → a9b8c7d6e5f4 → b0c9d8e7f6a5`); this revision
> is unchanged by them.

## How to verify

```sh
.venv/bin/python -m pytest -q tests/test_deterministic_settlement.py
.venv/bin/python -m pytest -q                     # full suite, zero regressions
.venv/bin/python scripts/check_contracts.py       # schemas, OpenAPI, migration head
AGENTFORGE_DATABASE_URL=sqlite:////tmp/v.db .venv/bin/python -m alembic upgrade head
```

`tests/test_deterministic_settlement.py` covers the required cases plus schema
rejection, `REPUTATION` tasks without escrow, escrow conservation and
single settlement, inference-receipt verification, tampered proof signatures,
the default strategy, and drift between the API task representation and the
documented `TaskResponse` schema.

## Deliberately not implemented

- No new settlement provider, asset, fee, or rail; only the existing mock provider boundary is used.
- No float arithmetic: settlement remains exact string/`Decimal` accounting.
- No validator-supplied escape hatch for deterministic tasks, and no way for an executor or validator to choose the strategy.
- No reputation or eligibility claim beyond the existing `task_verified` / `task_rejected` events.
- No client, fleet, airdrop or bot code; no external dependency was added.
