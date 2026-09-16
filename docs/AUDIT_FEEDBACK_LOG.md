# AgentForge Audit & Verification Log

*Maintained by: Antigravity (Auditor & Live Verifier)*  
*Target Audience: Web Agent (Feature Lead) & Maintainers*

---

## Log Entry: 2026-09-17 — Baseline Verification & Portability Fix

### Scope
Initial live audit and verification of AgentForge MVP test suite across operating systems (Windows & Linux).

### Verification Results
- **Pytest Suite**: 16/16 tests passing (100% green).
  - `test_idempotency_replay_and_conflict_cover_the_mutating_flow`: PASSED
  - `test_claim_expiry_reopens_and_rejects_old_work`: PASSED
  - `test_private_payloads_sessions_proofs_and_balances_require_authentication`: PASSED
  - `test_provenance_claims_stay_level_zero_until_a_registered_adapter`: PASSED
  - `test_deterministic_acceptance_rejects_validator_claims_and_tampered_proofs`: PASSED
  - `test_cursor_audit_pagination_is_authenticated_and_stable`: PASSED
  - `test_active_claim_index_and_outbox_leases_are_database_safe`: PASSED
  - `test_mock_escrow_transitions_conserve_value_and_cannot_double_settle`: PASSED
  - `test_authentication_rejects_missing_idempotency_stale_and_bad_signatures`: PASSED
  - `test_production_does_not_auto_create_sqlite_schema`: PASSED
  - `test_alembic_initial_schema_is_reproducible`: PASSED (after portability patch)
  - `test_dispute_open_and_resolution_are_idempotent`: PASSED
  - `test_server_side_group_independence_blocks_executor_and_validator`: PASSED
  - `test_ancestry_and_reciprocal_history_are_server_derived`: PASSED
  - `test_full_mock_exchange`: PASSED
  - `test_invalid_proof_is_rejected`: PASSED

---

### Defect Identified & Patched

#### Issue: Subprocess Alembic Invocation & Python Path Resolution
- **File**: `migrations/env.py` and `tests/test_audit_fixes.py`
- **Symptom**: Calling `alembic upgrade head` in a subprocess failed on systems where `alembic` is not on the system `$PATH` or where `server/` is not an installed package in site-packages, producing:
  `ModuleNotFoundError: No module named 'agentforge_server'`
- **Root Cause**:
  1. `subprocess.run(["alembic", ...])` assumed an executable named `alembic` exists on system PATH, which is unreliable in virtualenvs, Windows user-install paths, or non-global scripts directories.
  2. `migrations/env.py` imported `from agentforge_server.models import Base` without ensuring `server/` directory was on `sys.path`.
- **Fix Applied**:
  1. In `migrations/env.py`: Added dynamic resolution of repo root and prepended `server/` to `sys.path`.
  2. In `tests/test_audit_fixes.py`: Used `[sys.executable, "-m", "alembic", "upgrade", "head"]` with `PYTHONPATH` populated in `env`.
- **Impact**: Zero breaking changes. 100% cross-platform compatibility for CI, Docker, Linux, Windows, and macOS.

---

### Notes & Guidelines for Web Agent
1. **Model & Schema Changes**: Whenever creating new SQLAlchemy models in `server/agentforge_server/models.py`, generate a new Alembic migration in `migrations/versions/` using:
   `python -m alembic revision -m "<description>"`
2. **Settlement Isolation**: Maintain strict adherence to the `SettlementProvider` abstraction in `server/agentforge_server/providers.py`. Never reference concrete FLOP contracts directly in task or claim routing logic.
3. **Identity Verification**: Keep DID authentication purely Ed25519 (`did:key:z...`).
