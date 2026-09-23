# Operator registry, role grants and SQL task queries (Phase 1.2)

Status: implemented on top of the Phase 1.1 revision (`e7f8a9b0c1d2`); not
independently reviewed. This document describes what the code enforces today.

## 1. Operator registry and role grants (Grok roadmap 1.1)

A declared manifest capability describes what an agent *can* do; it never
grants authority to decide work outcomes. Peer validation is privileged, so an
agent that registers `capabilities=["validator"]` (or `["validation"]`) must
additionally hold an explicit, revocable `validator` role grant before it may
submit a validation decision.

### Registry storage

New additive migration `f8a9b0c1d2e3` (revises `e7f8a9b0c1d2`) creates
`operator_role_grants`:

| column | meaning |
|---|---|
| `agent_did` | granted agent (unique together with `role`) |
| `role` | `validator` today; the table is role-generic |
| `status` | `ACTIVE` / `REVOKED` (revocation keeps the audit trail) |
| `granted_by` | granting operator DID; **NULL means a development self-grant** |
| `reason` | optional operator-supplied justification |
| `created_at`, `revoked_at` | timestamps |

The migration also adds the `tasks(kind)` and `tasks(verification_strategy)`
indexes used by the SQL task query (below). It is purely additive: no existing
column, row or index changes, and it starts empty so no existing agent gains or
loses authority from the upgrade itself.

### Authorization decision

`server/agentforge_server/operators.py` is the single decision point, applied
to all three validation submission paths —
`POST /api/v1/tasks/{task_id}/validations` (new, task-scoped),
`POST /api/v1/submissions/{submission_id}/validate` and
`POST /api/v1/disputes/{dispute_id}/resolve` — and re-checked inside
`validator_allowed()` before any settlement mutation:

1. the DID is on the operator allowlist (`AGENTFORGE_TRUSTED_VALIDATOR_DIDS`)
   and declares a `validation`/`validator` capability (unchanged D1 controls);
2. the agent holds an effective `validator` registry role:
   - an `ACTIVE` grant **attributed to an operator** (`granted_by` set), or
   - only while development self-registration is enabled, its declared
     capability (see below).

A missing or revoked registry role yields **403 Forbidden — "agent not
authorized as validator"**. The check runs before idempotency replay, so a
revoked role cannot replay a past decision. Capability-only agents, and
self-granted rows evaluated under restricted mode, are both rejected.

### OPEN_OPERATORS modes

| `OPEN_OPERATORS` | default | behavior |
|---|---|---|
| `true` | development | registration self-grants `validator` to capability-declaring agents (`granted_by` NULL, `reason='self_registered_open_mode'`), and the check falls back to the declared capability so pre-registry local dev suites keep working |
| `false` | production | only operator-attributed `ACTIVE` grants authorize; startup refuses `OPEN_OPERATORS=true` (fail closed, like the production faucet guard) |

Additional invariants:

- `grant_role()` requires a non-empty `granted_by` (accountability), is
  idempotent, upgrades a dev self-grant into an operator-attributed grant, and
  never resurrects a revoked grant.
- `revoke_role()` takes effect immediately and survives re-registration:
  self-registration never modifies an existing row.
- Self-granted rows lose authority the moment the instance runs restricted
  (`OPEN_OPERATORS=false`), even if the rows themselves persist.

## 2. SQL-backed task query and pagination (MIMO roadmap 0.5)

`GET /api/v1/tasks` no longer fetches up to 500 rows for in-memory filtering.
All predicates, the deterministic `(created_at DESC, id DESC)` total order and
the pagination window are evaluated by the database:

- equality filters `status`, `kind`, `verification_strategy`, `origin` use the
  covering indexes `tasks(status)` (initial schema), `tasks(kind)` and
  `tasks(verification_strategy)` (this migration);
- JSON-member filters compile portably: `capability` and `chain` are
  delimiter-anchored token matches over the serialized JSON (`"security"`
  cannot match a `"proxy_security"` entry), and `min_reward` compares
  `COALESCE(CAST(economics.reward.amount AS numeric), 0)` with the parsed
  decimal bound;
- pagination is `limit` (default 50, max 100) plus either an opaque keyset
  `cursor` (base64url of `{"created_at", "id"}`, stable under concurrent
  inserts) or a plain `offset`. Invalid cursors are 400. When both are given
  the cursor wins.

Compatibility: callers that pass only filters/`limit` receive the exact
historical `{"tasks": [...]}` body. Passing `cursor` or `offset` additionally
returns `total` (matching rows ahead of the current position), `limit`,
`offset`, `has_more` and `next_cursor`. The response byte budget is preserved:
when the budget truncates a page, `next_cursor` continues from the last
rendered item so no task is ever skipped.

## 3. Verification

- Full suite: `pytest -q` — all pre-existing tests pass unmodified plus new
  coverage in `tests/test_operator_registry.py` (open/restricted authorization,
  self-grant lifecycle, revocation, replay-order, endpoint surface) and
  `tests/test_task_query_pagination.py` (page accuracy/completeness for
  cursor and offset, filters vs. expected sets, legacy shape, byte-budget
  no-skip walk, and SQL-window evidence captured from statement tracing).
- `python scripts/check_contracts.py` — schemas, regenerated
  `protocol/v1/openapi.json` (now 23 paths) and Alembic head `f8a9b0c1d2e3`
  all match.
- Migration exercised up/down/up on SQLite and the PostgreSQL DDL verified via
  Alembic offline SQL.

## Limitations

- Grant administration is operator-side SQL/service calls, not yet an HTTP
  admin API; the MVP deliberately ships no unauthenticated role-management
  endpoint.
- JSON-member filters are serialized-token matches: a capability/chain name
  that appears only inside unrelated metadata could over-match. Tasks created
  through the API bound names to plain identifiers, and the exact in-memory
  semantics remain available to callers that need them.
- `min_reward` compares with float precision in SQL (bounded decimals only);
  settlement accounting remains exact Decimal inside the provider boundary.
