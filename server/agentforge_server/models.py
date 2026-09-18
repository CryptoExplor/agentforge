"""SQLAlchemy models for the AgentForge MVP."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"

    did: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    operator_group: Mapped[str | None] = mapped_column(String(160), nullable=True)
    infrastructure_group: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class AgentCapability(Base):
    __tablename__ = "agent_capabilities"
    __table_args__ = (
        UniqueConstraint("agent_did", "name", name="uq_agent_capability"),
        Index("ix_capability_name", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_did: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class RegistrationChallenge(Base):
    __tablename__ = "registration_challenges"
    __table_args__ = (Index("ix_registration_challenges_expiry", "expires_at"),)

    challenge_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class UsedNonce(Base):
    __tablename__ = "used_nonces"
    __table_args__ = (
        UniqueConstraint("did", "nonce", name="uq_request_nonce"),
        Index("ix_used_nonces_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    did: Mapped[str] = mapped_column(String(200), nullable=False)
    nonce: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("did", "key", name="uq_idempotency_did_key"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    did: Mapped[str] = mapped_column(String(200), nullable=False)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="IN_PROGRESS", nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_task_status", "status"),
        Index("ix_task_poster", "poster_did"),
        Index("ix_task_deadline", "deadline"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), default="public", nullable=False)
    origin: Mapped[str] = mapped_column(String(64), nullable=False)
    poster_did: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    required_capabilities: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    chains: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    acceptance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    demand_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    generation_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    anti_circularity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    economics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    deadline: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="OPEN", nullable=False)
    activity_eligibility: Mapped[str] = mapped_column(String(40), default="NOT_ELIGIBLE", nullable=False)
    acceptance_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claim_task", "task_id"),
        Index("ix_claim_executor", "executor_did"),
        Index(
            "uq_active_task_claim",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    executor_did: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    lease_expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    heartbeat_at: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class InferenceSession(Base):
    __tablename__ = "inference_sessions"
    __table_args__ = (Index("ix_inference_task", "task_id"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    submission_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    executor_did: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="CREATED", nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        Index("ix_submission_task", "task_id"),
        Index("ix_submission_executor", "executor_did"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    executor_did: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    inference_session_ids: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    proof: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="SUBMITTED", nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class ValidationDecision(Base):
    __tablename__ = "validation_decisions"
    __table_args__ = (Index("ix_validation_submission", "submission_id"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), nullable=False)
    validator_did: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    policy: Mapped[str] = mapped_column(String(80), nullable=False)
    checks: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    deterministic_checks: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    reason_codes: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    settlement: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), nullable=False)
    opened_by: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    additional_evidence: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="OPEN", nullable=False)
    resolution_decision_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    resolved_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Escrow(Base):
    __tablename__ = "escrows"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    payer_did: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    reward_amount: Mapped[str] = mapped_column(String(80), default="0", nullable=False)
    deposit_amount: Mapped[str] = mapped_column(String(80), default="0", nullable=False)
    inference_budget: Mapped[str] = mapped_column(String(80), default="0", nullable=False)
    reserved_total: Mapped[str] = mapped_column(String(80), default="0", nullable=False)
    released_amount: Mapped[str] = mapped_column(String(80), default="0", nullable=False)
    refunded_amount: Mapped[str] = mapped_column(String(80), default="0", nullable=False)
    slashed_amount: Mapped[str] = mapped_column(String(80), default="0", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="FUNDED", nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = (UniqueConstraint("did", "asset", name="uq_ledger_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    did: Mapped[str] = mapped_column(ForeignKey("agents.did"), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    balance: Mapped[str] = mapped_column(String(80), default="0", nullable=False)


class LedgerEvent(Base):
    __tablename__ = "ledger_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    did: Mapped[str] = mapped_column(String(200), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_delta: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class ReputationEvent(Base):
    __tablename__ = "reputation_events"
    __table_args__ = (Index("ix_reputation_did", "did"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    did: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    delta: Mapped[float] = mapped_column(Float, nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    delivered_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Actor/causation attribution: who requested the transition, and which
    # verified request caused it. Null for server-generated transitions.
    actor_did: Mapped[str | None] = mapped_column(String(200), nullable=True)
    causation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Delivery telemetry for the worker and for outbox_metrics().
    last_attempt_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_did: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class RequestQuota(Base):
    """Shared fixed-window admission counters, not a queue or business ledger."""
    __tablename__ = "request_quotas"
    __table_args__ = (Index("ix_request_quotas_window", "window"),)
    bucket: Mapped[str] = mapped_column(String(80), primary_key=True)
    window: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
