"""Disputes: opening a challenge against a pending submission and resolving it.

A dispute is the only way a counterparty can contest a proof after submission,
and it freezes escrow so the funds cannot be released while the contest is
open. Resolution is a validator decision, so it reuses
``routes.validations.apply_validation`` rather than duplicating settlement.

The dispute window is evaluated against database server time: neither the
disputer's clock nor a ``created_at`` declared inside the proof can hold the
window open on a long-expired task.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Claim, Dispute, Escrow, Submission, Task
from ..schemas import DisputeCreate, ValidationCreate
from ..services import (
    add_audit,
    dispute_window_closed,
    now,
    received_at_of,
)
from ._shared import (
    authenticate,
    finish_idempotency,
    http_error,
    idempotency_replay,
    kernel,
    queue_request_outbox,
    require_validator_operator,
    server_deadline_reference,
)
from .validations import apply_validation

router = APIRouter()


@router.post("/api/v1/submissions/{submission_id}/disputes")
async def open_dispute(
    submission_id: str,
    body: DisputeCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    did = await authenticate(request, db)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    submission = db.get(Submission, submission_id)
    if not submission:
        raise http_error(404, "submission not found")
    task = db.get(Task, submission.task_id)
    if not task or did not in {task.poster_did, submission.executor_did}:
        raise http_error(403, "only requester or executor may open this dispute")
    if not kernel.guard_pending_submission(db, submission):
        raise http_error(409, "submission is already terminal")
    # Dispute window on server time (Grok roadmap 1.4). A submission stays
    # disputable while it is pending; once the task deadline has passed only
    # the configured grace period remains, so escrow cannot be frozen
    # indefinitely on a long-expired task. Evaluated against database server
    # time: neither the disputer's clock nor a declared ``created_at`` can
    # hold the window open.
    received = received_at_of(request)
    if dispute_window_closed(db, task, reference=server_deadline_reference(db, task.deadline)):
        raise http_error(409, "dispute window has closed")
    if db.get(Dispute, body.dispute_id):
        raise http_error(409, "dispute ID already exists")
    existing_open_dispute = db.scalar(
        select(Dispute).where(
            Dispute.submission_id == submission.id,
            Dispute.status == "OPEN",
        )
    )
    if existing_open_dispute:
        raise http_error(409, "submission already has an open dispute")
    dispute = Dispute(
        id=body.dispute_id,
        submission_id=submission.id,
        opened_by=did,
        reason=body.reason,
        additional_evidence=body.additional_evidence,
        status="OPEN",
        # Server receipt time: the audit trail for a dispute window is
        # anchored to the server clock, never to the disputer's clock.
        created_at=received,
    )
    db.add(dispute)
    submission.status = "DISPUTED"
    task.status = "DISPUTED"
    claim = db.get(Claim, task.claim_id) if task.claim_id else None
    if claim:
        claim.status = "DISPUTED"
    escrow = db.get(Escrow, task.id)
    if escrow:
        frozen = db.execute(update(Escrow).where(
            Escrow.task_id == task.id, Escrow.status.in_(["FUNDED", "FROZEN"]),
        ).values(status="FROZEN", updated_at=now()).execution_options(synchronize_session=False))
        if frozen.rowcount != 1:
            db.rollback()
            raise http_error(409, "escrow is already terminal")
        db.refresh(escrow)
    add_audit(db, actor_did=did, kind="DISPUTE_OPENED", aggregate_type="submission", aggregate_id=submission.id, payload={"dispute_id": dispute.id})
    queue_request_outbox(request, db, kind="DISPUTE_OPENED", aggregate_id=submission.id, payload={"task_id": task.id, "submission_id": submission.id, "dispute_id": dispute.id})
    response_body = {"dispute_id": dispute.id, "submission_id": submission.id, "status": dispute.status}
    finish_idempotency(request, response_body)
    db.commit()
    return response_body


@router.post("/api/v1/disputes/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: str,
    body: ValidationCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    did = await authenticate(request, db)
    require_validator_operator(db, did)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    dispute = db.get(Dispute, dispute_id)
    if not dispute or dispute.status != "OPEN":
        raise http_error(404, "open dispute not found")
    submission = db.get(Submission, dispute.submission_id)
    task = db.get(Task, submission.task_id) if submission else None
    if not submission or not task:
        raise http_error(404, "dispute submission not found")
    return apply_validation(
        db,
        task=task,
        submission=submission,
        validator_did=did,
        body=body,
        dispute_id=dispute.id,
        request=request,
    )
