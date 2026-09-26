"""Peer review: validator decisions, submitted per submission or per task, plus
the reputation read that those decisions produce.

``apply_validation`` is the single settlement path for every validated outcome
in the marketplace. Both endpoints here and ``routes.disputes.resolve_dispute``
funnel into it, so signature verification, independence checks, the
deterministic cross-check, escrow settlement, reputation and the outbox event
stay identical no matter which door a decision arrives through.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import operators as operator_registry
from ..crypto import canonical_json, verify_signature
from ..db import get_db
from ..models import Agent, Claim, Dispute, Submission, Task, ValidationDecision
from ..schemas import ValidationCreate
from ..services import (
    AccountingConflict,
    add_audit,
    add_reputation,
    escrow_settle,
    independence_failures,
    now,
    reputation_for,
    required_capability_names,
    verification_strategy_of,
)
from ..settings import settings
from ..validators import validate_submission as deterministic_validate
from ._shared import (
    authenticate,
    finish_idempotency,
    http_error,
    idempotency_replay,
    kernel,
    queue_request_outbox,
    require_validator_operator,
)

router = APIRouter()


def validator_allowed(db: Session, validator_did: str, task: Task, submission: Submission) -> bool:
    if validator_did not in settings.trusted_validator_dids or validator_did in {task.poster_did, submission.executor_did}:
        return False
    # Operator registry (Grok 1.1): allowlist + declared capability AND an
    # effective validator registry role (explicit grant, or the development
    # OPEN_OPERATORS self-registration fallback).
    if not operator_registry.validator_authorized(db, validator_did):
        return False
    return not independence_failures(
        db,
        task=task,
        participant_did=validator_did,
        role="validator",
    )


def decision_core(
    submission_id: str,
    validator_did: str,
    body: ValidationCreate,
    evidence_hash: str,
) -> dict[str, Any]:
    return {
        "decision_id": body.decision_id,
        "submission_id": submission_id,
        "validator_did": validator_did,
        "decision": body.decision,
        "policy": body.policy,
        "checks": body.checks,
        "reason_codes": body.reason_codes,
        "settlement": body.settlement,
        "evidence_hash": evidence_hash,
    }


def apply_validation(
    db: Session,
    *,
    task: Task,
    submission: Submission,
    validator_did: str,
    body: ValidationCreate,
    dispute_id: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    if not validator_allowed(db, validator_did, task, submission):
        raise http_error(403, "validator is not approved, independent or validation-capable")
    if verification_strategy_of(task) == "deterministic" and submission.status in {
        "VERIFIED",
        "REJECTED",
    }:
        # The task already settled when the proof was submitted; a late or
        # competing validator cannot re-decide (or re-settle) it.
        raise http_error(409, "task has already settled via deterministic strategy")
    # ``kernel.guard_pending_submission`` is a call-time lookup: the accounting
    # regression suite patches it on the app module to prove two concurrent
    # validators cannot both settle the same submission.
    if not kernel.guard_pending_submission(db, submission):
        raise http_error(409, "submission is not awaiting validation")
    if db.get(ValidationDecision, body.decision_id):
        raise http_error(409, "validation decision ID already exists")
    if db.scalar(
        select(ValidationDecision).where(
            ValidationDecision.submission_id == submission.id
        )
    ):
        raise http_error(409, "submission already has a validation decision")
    evidence_hash = body.evidence_hash or submission.proof_hash
    core = decision_core(submission.id, validator_did, body, evidence_hash)
    if not verify_signature(
        validator_did,
        canonical_json(core).encode("utf-8"),
        body.signature,
    ):
        raise http_error(401, "invalid validation decision signature")

    deterministic = deterministic_validate(db, task, submission)
    if body.decision in {"VERIFIED", "PARTIAL"} and deterministic.fatal:
        raise http_error(422, "deterministic validation failed")

    try:
        escrow_settle(
            db,
            task=task,
            executor_did=submission.executor_did,
            decision=body.decision,
            settlement=body.settlement,
        )
    except ValueError as exc:
        db.rollback()
        raise http_error(409 if isinstance(exc, AccountingConflict) else 400, str(exc))

    decision = ValidationDecision(
        id=body.decision_id,
        submission_id=submission.id,
        validator_did=validator_did,
        decision=body.decision,
        policy=body.policy,
        checks=body.checks,
        deterministic_checks=deterministic.checks,
        reason_codes=body.reason_codes,
        settlement=body.settlement,
        evidence_hash=evidence_hash,
        signature=body.signature,
        created_at=now(),
    )
    db.add(decision)
    submission.status = body.decision
    task.status = body.decision
    claim = db.get(Claim, task.claim_id) if task.claim_id else None
    if claim:
        claim.status = "COMPLETED"
        claim.updated_at = now()
    primary_capability = next(iter(required_capability_names(task)), None)
    executor_delta = {"VERIFIED": 1.0, "PARTIAL": 0.25, "REJECTED": -1.0, "SLASHED": -5.0}[body.decision]
    add_reputation(
        db,
        did=submission.executor_did,
        role="executor",
        kind=f"task_{body.decision.lower()}",
        delta=executor_delta,
        capability=primary_capability,
        reference_type="submission",
        reference_id=submission.id,
    )
    requester_delta = -5.0 if (
        body.decision == "SLASHED"
        and (body.settlement or {}).get("slash_subject") == "requester"
    ) else 0.1
    add_reputation(
        db,
        did=task.poster_did,
        role="requester",
        kind="task_validated",
        delta=requester_delta,
        reference_type="task",
        reference_id=task.id,
    )
    add_reputation(
        db,
        did=validator_did,
        role="validator",
        kind="validation_performed",
        delta=0.1,
        capability="validation",
        reference_type="submission",
        reference_id=submission.id,
    )
    add_audit(db, actor_did=validator_did, kind="VALIDATION_RECORDED", aggregate_type="submission", aggregate_id=submission.id, payload={"decision": body.decision, "decision_id": body.decision_id})
    queue_request_outbox(request, db, kind="VALIDATION_RECORDED", aggregate_id=submission.id, payload={"task_id": task.id, "submission_id": submission.id, "decision": body.decision, "decision_id": body.decision_id, "evidence_hash": evidence_hash})
    # The ordinary validation endpoint also accepts disputed submissions.
    # Close the associated open dispute in the same winning transaction.
    db.execute(update(Dispute).where(
        Dispute.submission_id == submission.id, Dispute.status == "OPEN",
    ).values(status="RESOLVED", resolution_decision_id=decision.id, resolved_at=now()))
    response_body = {
        "decision_id": decision.id,
        "submission_id": decision.submission_id,
        "decision": decision.decision,
        "validator_did": decision.validator_did,
        "policy": decision.policy,
        "evidence_hash": decision.evidence_hash,
        "settlement": decision.settlement,
        "checks": decision.checks,
        "deterministic_checks": decision.deterministic_checks,
        "reason_codes": decision.reason_codes,
        "signature": decision.signature,
    }
    if request is not None:
        finish_idempotency(request, response_body)
    db.commit()
    return response_body


@router.post("/api/v1/submissions/{submission_id}/validate")
async def validate_submission(
    submission_id: str,
    body: ValidationCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    did = await authenticate(request, db)
    require_validator_operator(db, did)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    submission = db.get(Submission, submission_id)
    if not submission:
        raise http_error(404, "submission not found")
    task = db.get(Task, submission.task_id)
    if not task:
        raise http_error(404, "task not found")
    if verification_strategy_of(task) == "deterministic" and submission.status in {
        "VERIFIED",
        "REJECTED",
    }:
        # Deterministic tasks are decided by the server at submission time,
        # so no independent validator decision is needed or accepted.
        raise http_error(409, "task has already settled via deterministic strategy")
    if submission.status not in {"SUBMITTED", "DISPUTED"}:
        raise http_error(409, "submission is already terminal")
    return apply_validation(
        db,
        task=task,
        submission=submission,
        validator_did=did,
        body=body,
        request=request,
    )


@router.post("/api/v1/tasks/{task_id}/validations")
async def create_task_validation(
    task_id: str,
    body: ValidationCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Submit a signed peer-validation decision for a task's pending proof.

    Task-scoped peer validation: the server resolves the task's submission
    that is awaiting validation (``SUBMITTED``, or ``DISPUTED`` so a
    validation also closes the dispute). The operator authorization gate is
    identical to the submission-scoped endpoint: operator allowlist entry,
    declared validation capability, and an effective ``validator`` registry
    role (explicit grant, or development ``OPEN_OPERATORS``
    self-registration). Everything else — signature verification,
    independence checks, deterministic cross-check, atomic settlement — is
    the shared ``apply_validation`` path.
    """
    did = await authenticate(request, db)
    require_validator_operator(db, did)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    task = db.get(Task, task_id)
    if not task:
        raise http_error(404, "task not found")
    submission = db.scalar(
        select(Submission)
        .where(
            Submission.task_id == task.id,
            Submission.status.in_(["SUBMITTED", "DISPUTED"]),
        )
        .order_by(Submission.created_at.desc(), Submission.id.desc())
        .limit(1)
    )
    if not submission:
        raise http_error(409, "task has no submission awaiting validation")
    return apply_validation(
        db,
        task=task,
        submission=submission,
        validator_did=did,
        body=body,
        request=request,
    )


@router.get("/api/v1/reputation/{did}")
def get_reputation(did: str, db: Session = Depends(get_db)):
    if not db.get(Agent, did):
        raise http_error(404, "agent not found")
    return reputation_for(db, did)
