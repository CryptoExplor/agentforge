# AgentForge Documentation Audit & Gap Report

**Type:** Read-only audit (no `docs/` files were modified)
**Audit target label:** Post-Phase 1.5 modular router decomposition (`a97d14b`)
**Actual working-tree HEAD inspected:** `619a83a058cb45cefc8dcb068f141dc8c80e7f6b`
 (branch `arena/01a0dcf7-agentforge`; commit `a97d14b` is not present in this
 shallow checkout — the state described below is verified against the code that
 is actually on disk)
**Ground-truth checks run:** `pytest` (**369 passed, 3 skipped**),
`scripts/check_contracts.py` (**SCHEMAS_OK: 7 · OPENAPI_MATCH: 23 paths ·
MIGRATION_HEAD_MATCH: b0c9d8e7f6a5**).

---

## 0. Scope note — file count

The brief says "22 documentation files." The `docs/` tree actually contains **26
files**: 21 top-level Markdown docs, 3 Markdown docs under
`protocol-intelligence/flop/`, and 2 JSON data files
(`protocol-intelligence/flop/PARAMETER_SNAPSHOT.json`,
`audit/dependency-scan-2026-09-18.json`). All 26 are classified below. The "22"
in the brief most plausibly refers to the top-level narrative docs; the exact
figure should be reconciled, but no file was skipped.

---

## 1. File-by-File Classification Matrix

Legend — **B** = `[BUILT & ACCURATE]`, **S** = `[STALE / OUTDATED]`,
**F** = `[SPECULATIVE / FUTURE]`. Most files mix categories; the dominant tag is
listed first.

| # | File | Class | Summary |
|---|---|---|---|
| 1 | `REPOSITORY_MAP.md` | **B** | The single most current doc. Already maps `app.py` as "composition root only," lists all seven `routes/*.py` modules and their responsibilities, and correctly places `apply_validation` in `validations.py`. Accurate to HEAD. |
| 2 | `TASK_VERIFICATION_STRATEGIES.md` | **B** (minor S) | Phase 1.1 `deterministic`/`peer_review`/`operator` strategies match code. Cites `validators.evaluate_deterministic` (exists). Endpoints correct. Stale only in that it does not cite the new router files and still anchors head at `e7f8a9b0c1d2` (now `b0c9d8e7f6a5`). |
| 3 | `OPERATOR_REGISTRY.md` | **B** (minor S) | Phase 1.2 registry, `operator_role_grants`, `OPEN_OPERATORS`, three validation paths, "now 23 paths" all match `operators.py` + `models.py` + migration `f8a9b0c1d2e3`. Does not cite router modules; head reference is Phase-1.2-era. |
| 4 | `SERVER_TIME_AND_CLOCK_DRIFT.md` | **B** | Phase 1.4 clock model matches `clock.py`, `models.py` (`claims.received_at`, `submissions.received_at`), migration `b0c9d8e7f6a5`, 60 s drift window. Accurate; would benefit from router-path anchors. |
| 5 | `EVENT_OUTBOX.md` | **B** | Signed outbox/v2 causation matches `outbox.py`, `event_envelope.py`, `publisher.py`, `worker.py`, `adapters/technocore.py`. No stale `app.py` anchors. Accurate. |
| 6 | `ACCOUNTING_REMEDIATION.md` | **B** | Exact-decimal ledger/escrow controls match `money.py`, `services.py`, `adapters/mock_settlement.py`. Accurate. |
| 7 | `ARCHITECTURE_DECISIONS.md` | **B / F** | Frozen decision record; §1–§5c, §5a/§5b/§5c match code. §8 still frames "modular monolith" as a **design addendum** even though Phase 1.5 realized it — should be marked done. §5 step 5 (deal record) and §6 FLOP/TCLK context remain **F**. |
| 8 | `SECURITY_REMEDIATION.md` | **B / S** | D1–D6 controls are implemented and accurate, BUT the "Sources:" line points to `app,admission,middleware,settings,schemas.py`; post-1.5, D1/D5/D6 handlers live in `routes/_shared.py` + `routes/agents.py` + `routes/tasks.py`. Source pointers are stale. |
| 9 | `PROJECT_STATUS.md` | **B / S** | "Current work" list is current through Phase 1.5 (good). But the "Review publication" table, PR SHAs, and CI section are pinned to old branches/heads (`bd6bf59`, PR #5/#6, `e7f8a9b0c1d2`) and read as stale relative to HEAD. Test-count narrative predates 369. |
| 10 | `AUDIT_VERIFICATION.md` | **S / B** | Evidence record with **layered, internally divergent** counts: top table "254 passed / 22 paths / head e7f8a9b0c1d2," Phase 1.4 section "367 passed / 23 paths / b0c9d8e7f6a5." **No Phase 1.5 verification section exists** and current suite is 369. |
| 11 | `RELEASE_NOTES.md` | **S** | Stops at "Unreleased 2026-09-18." States "No new migration beyond the existing D1–D6 quota revision" and "no SDK/marketplace modularization" — contradicted by migrations `e7f8a9b0c1d2`, `f8a9b0c1d2e3`, `a9b8c7d6e5f4`, `b0c9d8e7f6a5` and the Phase 1.1–1.5 features. Missing entries. |
| 12 | `MARKETPLACE_FEE_AND_FLOP_SETTLEMENT_DESIGN.md` | **S / F** | Header says "not implemented"; §1.3 says platform is paid "**Nothing today**"; §6 lists **P0 (fee engine on mock ledger) as a future gate**. But P0 is **BUILT** (Phase 1.3: `service_fee_mode`, `escrows.platform_fee_amount`, migration `a9b8c7d6e5f4`, `AGENTFORGE_MAX_SERVICE_FEE_BPS`). P1–P4 (FLOP/TCLK rails) remain genuinely **F**. |
| 13 | `DEPLOYMENT_READINESS_2026-09-17.md` | **S** (historical) | Dated snapshot at `bd6bf59`. Heavy `app.py:NNN` line anchors (D1/D3/D5/D6, `/`, reaper, route table), "22 documented paths," D1–D6 marked OPEN. All superseded. Labeled historical, but references are broken against current code. |
| 14 | `AUDIT_SIGNED_OUTBOX_2026-09-17.md` | **S** (historical) | Historical audit at `3986dd1`. F1–F6 all remediated. Uses `app.py:169-190`, `app.py:192-193` anchors that no longer exist. Explicitly labeled history; keep but isolate. |
| 15 | `SDK_ARCHITECTURE_PLAN.md` | **F / S** | Design-only at `bd6bf59`. SDK resource wrappers remain **F**. Stale claims: "`app.py` contains substantial SQL/business logic as well as routing" and "Later server extraction: selected `app.py` handlers" (§ tables) — Phase 1.5 already extracted them. `client.py` "roughly 313 lines" is now 353. |
| 16 | `DISCOVERY_SCALABILITY_PLAN.md` | **F / S** | 100k-client discovery/broker design — entirely **F** (no endpoint/SDK method/schema built). Stale `app.py:NNN` anchors in its endpoint inventory (`app.py:620-654`, `656-663`, `1275-1327`, `596-601`). |
| 17 | `ACTIVITY_ENGINE_P0_REVIEW.md` | **F** (accurate) | Review of an **external** client that is correctly documented as absent from the repo (`scripts/activity_engine/` etc. do not exist). Accurate as a boundary/roadmap doc. |
| 18 | `INTEGRATION_BOUNDARIES.md` | **B / F** | Boundary statements match code (mock inference/settlement present; no TCLK adapter, no DealReference). TCLK/FLOP/rail integrations are labeled future — correct. Accurate. |
| 19 | `AUDIT_FEEDBACK_LOG.md` | **S** (historical) | PR #1/#2 log: "24/24 tests," "22 endpoints," "5/5 schemas," Python 3.14, and a "Next PR" plan (outbox, multi-validator) that is now built. Historical; counts stale (now 369 tests, 23 paths, 7 schemas). |
| 20 | `PR_PLAN.md` | **B** | Gate/checklist process doc; still valid. No code claims to drift. |
| 21 | `GITHUB_HANDOFF.md` | **B** | Workflow doc pointing to canonical files; links resolve. Valid. |
| 22 | `protocol-intelligence/flop/CURRENT_STATE.md` | **F** | FLOP/TCLK research; code cross-refs (`providers.py`, `schemas.py`, `settlement.py`, `validators/deterministic.py`) still exist. Correctly future. Minor: says the signed-outbox audit "remains applicable" though F1–F6 are now fixed. |
| 23 | `protocol-intelligence/flop/SOURCES.md` | **F** | Dated external source ledger; documentation-only. Accurate. |
| 24 | `protocol-intelligence/flop/CHANGELOG.md` | **F** | Revision history of the intelligence set. Accurate. |
| 25 | `protocol-intelligence/flop/PARAMETER_SNAPSHOT.json` | **F** | Draft FLOP parameters, `runtime_use_allowed: false`, all `runtime_value: null`. Correctly isolated future data. |
| 26 | `audit/dependency-scan-2026-09-18.json` | **B** (historical) | pip-audit evidence for the `cryptography>=50.0.1` bump. Historical artifact; accurate as dated evidence. |

**Roll-up:** Built & accurate: ~9 · Mixed (accurate core, stale anchors/heads): ~7 ·
Stale/historical: ~5 · Speculative/future: ~6 (with overlaps where files span categories).

---

## 2. Specific Divergence Findings

Concrete contradictions between `docs/` and `server/agentforge_server/`, verified
against code on disk.

### 2.1 Obsolete file paths / function references (monolithic `app.py`)

Phase 1.5 reduced `app.py` to a 103-line composition root and moved every handler
into `routes/`. These docs still point at the old monolith:

| Doc | Stale reference | Ground truth |
|---|---|---|
| `SECURITY_REMEDIATION.md` | "Sources: `server/agentforge_server/{app,…}.py`" for D1/D5/D6 | D1 private read → `routes/_shared.py::authorize_task_read` (L324); D5 search ordering → `routes/agents.py` (search L179 before `{did}` L210); D6 reward filter → `routes/tasks.py` (L279–283, returns **422**) |
| `DEPLOYMENT_READINESS_2026-09-17.md` | `app.py:441–456`, `352–364`, `115–120`, `478,513`, `635`, `122–133`, `196–205`, `376–529`, `531–997`, `1158–1327` | None of these line ranges exist in the current `app.py`; the code is now spread across `routes/*.py` |
| `AUDIT_SIGNED_OUTBOX_2026-09-17.md` | `app.py:169-190`, `app.py:192-193` | Request signing/causation now in `routes/_shared.py`; reaper invocation in `routes/_shared.py`/`services.py` |
| `DISCOVERY_SCALABILITY_PLAN.md` | `app.py:620–654`, `656–663`, `1275–1327`, `596–601` | `GET /tasks` + reaper → `routes/tasks.py`; `GET /events` → `routes/system.py` |
| `SDK_ARCHITECTURE_PLAN.md` | "`app.py` contains substantial SQL/business logic as well as routing"; "Later server extraction: selected `app.py` handlers" | Extraction already done in Phase 1.5; SQL/business logic lives in `routes/*.py` + `services.py` |

### 2.2 Inconsistencies with the new domain routers (`routes/`)

- Only **2 of 26** docs mention `routes/` at all (`REPOSITORY_MAP.md`,
  `PROJECT_STATUS.md`). The seven feature docs that describe endpoint behavior
  (`TASK_VERIFICATION_STRATEGIES`, `OPERATOR_REGISTRY`, `SERVER_TIME_AND_CLOCK_DRIFT`,
  `EVENT_OUTBOX`, `SECURITY_REMEDIATION`, `ACCOUNTING_REMEDIATION`,
  `INTEGRATION_BOUNDARIES`) describe correct behavior but never cite the owning
  router module, so none satisfy the "cite the specific source file" rule.
- Router mount order and the load-bearing `agents/search`-before-`agents/{did}`
  ordering now live in `routes/__init__.py` and `routes/agents.py`. `DEPLOYMENT_READINESS`
  D5 still describes this as a defect at `app.py:478,513`.
- The five monkeypatch-surface names (`MAX_LIST_BYTES`, `can_execute`,
  `guard_active_claim`, `guard_pending_submission`, `queue_outbox`) read through
  `app` at call time via `routes/_shared.py` are documented **only** in
  `PROJECT_STATUS.md`/`REPOSITORY_MAP.md`; no other doc reflects this indirection.

### 2.3 Discrepancies with DB schema (`models.py`) and migration `b0c9d8e7f6a5`

- **Head migration:** `db.py::SCHEMA_REVISION = "b0c9d8e7f6a5"` and
  `check_contracts.py` both confirm head `b0c9d8e7f6a5`. Docs cite mixed heads:
  `TASK_VERIFICATION_STRATEGIES`/`OPERATOR_REGISTRY` → `e7f8a9b0c1d2`/`f8a9b0c1d2e3`;
  `AUDIT_VERIFICATION` top table → `e7f8a9b0c1d2`; `AUDIT_SIGNED_OUTBOX` → `c4d5e6f7a8b9`.
  Full chain on disk: `3293de03bb66 → c4d5e6f7a8b9 → d6e7f8a9b0c1 → e7f8a9b0c1d2 →
  f8a9b0c1d2e3 → a9b8c7d6e5f4 → b0c9d8e7f6a5`.
- **`escrows.platform_fee_amount`** (migration `a9b8c7d6e5f4`) exists in
  `models.py`, but `MARKETPLACE_FEE_AND_FLOP_SETTLEMENT_DESIGN.md` still says the
  fee engine is unbuilt (see 2.5).
- **`claims.received_at` / `submissions.received_at`** (migration `b0c9d8e7f6a5`)
  exist and are correctly described in `SERVER_TIME_AND_CLOCK_DRIFT.md`, but
  `DEPLOYMENT_READINESS`/`AUDIT_SIGNED_OUTBOX` predate them and describe the old
  client-timestamp/lease model.
- **`operator_role_grants`** table (migration `f8a9b0c1d2e3`) is documented in
  `OPERATOR_REGISTRY.md` (accurate) but absent from the older audit/readiness docs.

### 2.4 Open endpoints vs. documented OpenAPI paths (23 routes)

Live app exposes **23 OpenAPI path items / 24 operations** (`GET`+`POST /api/v1/tasks`
share one path) plus the unschematized `GET /`. Verified list:

```
POST /api/v1/agents/register            GET  /api/v1/proofs/{submission_id}
GET  /api/v1/agents/search              GET  /api/v1/register/challenge
GET  /api/v1/agents/{did}               GET  /api/v1/reputation/{did}
GET  /api/v1/agents/{did}/balance       GET  /api/v1/submissions/{submission_id}
GET  /api/v1/capabilities               POST /api/v1/submissions/{submission_id}/disputes
POST /api/v1/claims/{claim_id}/heartbeat POST /api/v1/submissions/{submission_id}/validate
POST /api/v1/disputes/{dispute_id}/resolve POST /api/v1/tasks   GET /api/v1/tasks
GET  /api/v1/events                     GET  /api/v1/tasks/{task_id}
GET  /api/v1/inference/{session_id}     POST /api/v1/tasks/{task_id}/cancel
POST /api/v1/tasks/{task_id}/claim      POST /api/v1/tasks/{task_id}/inference
POST /api/v1/tasks/{task_id}/submissions POST /api/v1/tasks/{task_id}/validations
GET  /health
```

Contradictions:
- `DEPLOYMENT_READINESS` ("22 documented paths"), `AUDIT_SIGNED_OUTBOX` ("22 paths"),
  `AUDIT_FEEDBACK_LOG` ("22 endpoints"), and `AUDIT_VERIFICATION` top table ("22
  paths match") predate the task-scoped `POST /api/v1/tasks/{task_id}/validations`
  (Phase 1.2), which took the count to **23**. `OPERATOR_REGISTRY`,
  `SERVER_TIME_AND_CLOCK_DRIFT`, and the `AUDIT_VERIFICATION` Phase-1.4 section
  correctly say **23**.
- `DEPLOYMENT_READINESS` lists `POST /submissions/{submission_id}/validate` but not
  the newer task-scoped validation route.

### 2.5 "Built but documented as future" (the highest-risk divergence)

`MARKETPLACE_FEE_AND_FLOP_SETTLEMENT_DESIGN.md` is a **design record** that has
been overtaken by implementation for its first phase:

- Header: "**not implemented, not an implementation approval**"; §1.3: platform is
  paid "**Nothing today**"; §6: "**P0 | Fee engine on the mock ledger … | Maintainer
  approval**" as a *future* gate.
- Ground truth (Phase 1.3, verified): `schemas.py` `service_fee_mode:
  Literal["none","fixed","bps"]`; `settings.py` `max_service_fee_bps` (default 500);
  `escrows.platform_fee_amount` column; migration `a9b8c7d6e5f4`;
  `PLATFORM_FEE_COLLECTED` audit event; `agentforge:platform` system account;
  conservation invariant `released + platform_fee + refunded + slashed ==
  reserved_total`.
- **P1–P4** (FLOP B1/B2 rails, TCLK coordination adapter) remain genuinely
  unbuilt and should stay `[PLANNED]`.

### 2.6 Test-suite/CI count drift

- Current suite: **369 passed, 3 skipped**. `AUDIT_VERIFICATION.md` records, in
  separate sections, 254, 267, and 367 — none current, and **no Phase 1.5 entry
  exists** even though the decomposition is in the tree.
- `SCHEMAS_OK: 7` today vs. "5/5 schemas" (`AUDIT_FEEDBACK_LOG`) / "6/6"
  (`AUDIT_SIGNED_OUTBOX`) / "7 match" (`AUDIT_VERIFICATION`).

### 2.7 Test-helper duplication (verified)

There is **no `tests/helpers.py`**. `signed_request` is redefined in **8** test
files and `register` in **7**:

```
signed_request: test_asset_guardrails, test_audit_fixes, test_clock_drift,
                test_deterministic_settlement, test_mvp, test_operator_registry,
                test_signed_event_outbox, test_task_query_pagination
register:       test_asset_guardrails, test_audit_fixes, test_deterministic_settlement,
                test_mvp, test_operator_registry, test_signed_event_outbox,
                test_task_query_pagination
```

The signatures have already diverged (e.g. some `signed_request` take a `key`
kwarg, some don't; `register` manifest arg is variously required/optional), which
is exactly the drift a single source would prevent.

---

## 3. Proposed Update Plan

No `docs/` file is edited in this pass. The plan below is what a follow-up
editing pass would do, under the four strict rules.

### Rule A — Ground Truth Only (only document what is implemented and passing CI)

| File | Planned edit |
|---|---|
| `MARKETPLACE_FEE_AND_FLOP_SETTLEMENT_DESIGN.md` | Split into **Built** vs **Planned**: move P0 (fee engine) to a "Shipped (Phase 1.3)" section, rewrite §1.3 platform row from "Nothing today" to the actual `service_fee_mode`/`platform_fee_amount` behavior, and relabel §6 P0 as done. Keep P1–P4 as `[PLANNED]`. |
| `AUDIT_VERIFICATION.md` | Add a **Phase 1.5** verification section (369 passed / 3 skipped / 23 paths / head `b0c9d8e7f6a5`); mark the 254/267/367 and 22-path lines explicitly as superseded historical rows. |
| `RELEASE_NOTES.md` | Add entries for Phases 1.1–1.5; remove/qualify "no new migration beyond the D1–D6 quota revision" and "no SDK/marketplace modularization." |
| `PROJECT_STATUS.md` | Refresh the publication/PR/CI table to the current branch and HEAD; align the head reference to `b0c9d8e7f6a5`. |
| `AUDIT_FEEDBACK_LOG.md` | Leave the dated 2026-09-17 entry as-is but add a one-line "superseded — see AUDIT_VERIFICATION.md" banner; do not restate old counts as current. |

### Rule B — Code & Commit Anchoring (cite the exact source file + commit/PR)

- Replace every `app.py:NNN` anchor with the owning module:
  - `SECURITY_REMEDIATION.md` "Sources:" → D1 `routes/_shared.py::authorize_task_read`,
    D3 `middleware.py`, D5 `routes/agents.py`, D6 `routes/tasks.py`, plus
    `admission.py`/`validators/*` where unchanged.
  - `DEPLOYMENT_READINESS_2026-09-17.md`, `AUDIT_SIGNED_OUTBOX_2026-09-17.md`,
    `DISCOVERY_SCALABILITY_PLAN.md`, `SDK_ARCHITECTURE_PLAN.md` → convert
    monolithic `app.py:NNN` references into `routes/<domain>.py` (function-level)
    references, or, for the two explicitly historical audit files, prepend a
    banner "line anchors describe the pre-1.5 monolith" rather than rewriting a
    dated artifact.
- Add router-module citations to the feature docs (`TASK_VERIFICATION_STRATEGIES`
  → `routes/submissions.py`/`routes/validations.py`; `OPERATOR_REGISTRY` →
  `routes/validations.py` + `operators.py`; `SERVER_TIME_AND_CLOCK_DRIFT` →
  `clock.py` + `routes/_shared.py`/`routes/claims.py`).
- Normalize all migration-head references to `b0c9d8e7f6a5` (with the full chain),
  and endpoint counts to **23 paths / 24 operations + `GET /`**.
- Anchor each architectural claim to the Phase commit/PR that introduced it
  (Phase 1.1 `e7f8a9b0c1d2`, 1.2 `f8a9b0c1d2e3`, 1.3 `a9b8c7d6e5f4`, 1.4
  `b0c9d8e7f6a5`, 1.5 router decomposition).

### Rule C — Roadmap Isolation (unbuilt designs must read `[PLANNED]`)

- `[PLANNED]`-tag or quarantine into a clearly-marked design section:
  FLOP B1/B2 rails and TCLK coordination (`MARKETPLACE_FEE…` P1–P4,
  `INTEGRATION_BOUNDARIES` future path, `ARCHITECTURE_DECISIONS` §5 step 5/§6,
  all `protocol-intelligence/flop/*`), the 100k discovery broker/subscription
  design (`DISCOVERY_SCALABILITY_PLAN`), SDK resource wrappers/JS/MCP
  (`SDK_ARCHITECTURE_PLAN`), and the external Activity Engine
  (`ACTIVITY_ENGINE_P0_REVIEW`).
- In `ARCHITECTURE_DECISIONS.md` §8, move "modular monolith" from the future
  "design addendum" framing to a "realized in Phase 1.5" note so it is not read
  as still-pending.
- Ensure no `[PLANNED]` item sits in the same list as a shipped feature without a
  visible tag (current `MARKETPLACE_FEE…` §6 mixes shipped P0 with planned P1–P4).

### Rule D — Test Helper Cleanup

- Create a single-source **`tests/helpers.py`** exporting canonical
  `signed_request(...)` and `register(...)` (superset signatures — include the
  optional `key` kwarg and optional `manifest`), plus any shared identity/setup
  helpers.
- Delete the 8 duplicated `signed_request` and 7 duplicated `register`
  definitions and import from `tests/helpers.py` (or promote them into
  `conftest.py` fixtures).
- Run `pytest` to confirm the current **369 passed / 3 skipped** is preserved
  (no behavior change — pure consolidation).
- Document the consolidation in `AUDIT_VERIFICATION.md`'s reproduction section and
  reference `tests/helpers.py` in `REPOSITORY_MAP.md`.
- *(This is a `tests/` change, outside the `docs/` freeze; it is noted here per
  the brief and would be executed in the implementation pass, not this audit.)*

---

## Appendix — Verification commands used

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q                     # 369 passed, 3 skipped
.venv/bin/python scripts/check_contracts.py       # 7 schemas, 23 paths, head b0c9d8e7f6a5
.venv/bin/python -c "from agentforge_server.app import app; app.openapi()"  # path enumeration
grep -rn '@router\.(get|post|put|patch|delete)' server/agentforge_server/routes/
grep -rn 'app\.py' docs/                           # stale monolith anchors
grep -rln 'def signed_request\|def register' tests/  # helper duplication
```
