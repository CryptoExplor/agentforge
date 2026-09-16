# AgentForge Audit & Verification Log

*Maintained by: Antigravity (Auditor & Live Verifier)*  
*Target Audience: Web Agent (Feature Lead) & Maintainers*

---

## Log Entry: 2026-09-17 — PR #1 & PR #2 Live Audit, Defect Fix & Merge

### Scope
Full live audit, cross-platform testing, and verification of:
- **PR #1**: Settlement Provider Boundary Isolation (`5c87867`, merged at `4521722`)
- **PR #2**: Server-Derived Asset & Mode Guardrails (`6fcabcc` & `165d9ea`, merged at `ecd9300`)

---

### Verification Summary
- **Pytest Suite**: **24/24 tests passing (100% green)** on Python 3.14.
- **Remote CI (GitHub Actions)**: Successful run (`conclusion: success`, Run ID: `35152629599`).
- **OpenAPI Schema Check**: 22 endpoints in FastAPI app match `protocol/v1/openapi.json` exactly.
- **Canonical JSON Schemas**: 5/5 schemas valid under Draft 2020-12 (`protocol/v1/*.schema.json`).
- **Alembic Reproducibility**: `downgrade base` -> `upgrade head` cycles cleanly to `3293de03bb66`.
- **Git Hygiene**: `git diff --check` clean, zero whitespace/format errors.

---

### Audit Findings & Defect Resolved in PR #2

#### 1. Defect: Zero-Reward Tasks Rejected When Using Non-MOCK Allowed Assets
- **Location**: `server/agentforge_server/adapters/mock_settlement.py`
- **Symptom**: A task configured with zero reward (`reward.amount == "0"`) but funding a security deposit or inference budget in `TEST_CREDIT` was rejected with:
  `ValueError: all funded MVP escrow amounts must use the reward asset`
- **Root Cause**:
  `schemas.py` defines `TaskEconomics.reward` with `default_factory=Money`, which automatically initializes `amount="0"` and `asset="MOCK"`. Because `reward.get("asset")` always evaluated to `"MOCK"`, the settlement provider assumed the primary required asset was `"MOCK"`. When comparing `security_deposit.asset = "TEST_CREDIT"`, it raised a mismatch error despite `TEST_CREDIT` being on the server allow-list.
- **Fix Applied (Commit `165d9ea`)**:
  1. Updated `MockSettlementProvider.fund()` to derive the primary asset from the first funded component (`amount > ZERO`), falling back to `reward.asset` or `"MOCK"`.
  2. Enforced that *any* explicitly requested asset across `(reward, deposit, inference)` must be member of `_allowed_assets()` regardless of amount.
  3. Ensured cross-component consistency only for non-zero funded amounts.

#### 2. Test Suite Expansion (Added 2 New Invariant Tests)
- `test_unsupported_settlement_provider_raises`: Validates that configuring `AGENTFORGE_SETTLEMENT_PROVIDER` to an unsupported value (e.g. `flop_onchain`) immediately raises `RuntimeError` on access.
- `test_zero_reward_task_with_test_credit_deposit`: Validates that reputation/zero-reward tasks with deposits in `TEST_CREDIT` succeed, reserve funds, and assign the correct escrow asset.

---

### Feedback & Guidance for Web Agent (Next Steps)

1. **Escrow Invariant Confirmed**:
   The `SettlementProvider` abstraction is cleanly isolated in `server/agentforge_server/settlement.py` and `adapters/mock_settlement.py`. No speculative FLOP contracts or external RPC calls exist. All accounting remains deterministic Decimal arithmetic with ledger idempotency and append-only audit trails.
2. **Next PR Planning**:
   - **PR #3 (Outbox & Gossip Worker)**: Implement durable background processing for `OutboxEvent` to broadcast signed gossip events to Technocore (`adapters/technocore.py`).
   - **PR #4 (Multi-Validator Consensus & Dispute Protocol)**: Extend `validation_decisions` to support quorum collection and dispute escalation while preserving group independence checks.
3. **Convention**:
   Always keep `server` on `PYTHONPATH` during subprocess invocations and verify f-string syntax compatibility with Python 3.11+.
