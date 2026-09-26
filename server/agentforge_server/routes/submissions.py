"""Execution and proof: inference sessions, signed proof submission, proof
retrieval, and deterministic auto-settlement.

Two distinct verification paths meet here. A ``deterministic`` task is decided
by the server inside the submission transaction -- no validator is involved and
the escrow transition, reputation event and outbox event commit atomically with
the submission. Any other strategy leaves the submission pending peer review,
which ``routes.validations`` handles.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..crypto import canonical_json, sha256_json, verify_signature
from ..db import get_db
from ..models import Claim, InferenceSession, Submission, Task
from ..providers import ProviderUnavailable, get_provider
from ..schemas import InferenceRequestCreate, SubmissionCreate
from ..services import (
    AccountingConflict,
    add_audit,
    add_reputation,
    deadline_passed,
    lease_expiry,
    mark_task_deadline_expired,
    now,
    received_at_of,
    required_capability_names,
    settle_escrow,
    verification_strategy_of,
)
from ..validators import evaluate_deterministic
from ._shared import (
    authenticate,
    authorize_task_read,
    discard_idempotency,
    finish_idempotency,
    http_error,
    idempotency_replay,
    kernel,
    queue_request_outbox,
    server_deadline_reference,
)

router = APIRouter()


def submission_view(submission: Submission) -> dict[str, Any]:
    return {
        "submission_id": submission.id,
        "task_id": submission.task_id,
        "executor_did": submission.executor_did,
        "result": submission.result,
        "evidence": submission.evidence,
        "inference_session_ids": submission.inference_session_ids,
        "proof": submission.proof,
        "result_hash": submission.result_hash,
        "proof_hash": submission.proof_hash,
        "status": submission.status,
        "created_at": submission.created_at,
    }


def deterministic_auto_settlement(
    db: Session,
    *,
    task: Task,
    submission: Submission,
    claim: Claim,
    request: Request,
) -> dict[str, Any]:
    """Verify and settle a deterministic submission in the caller's transaction.

    Nothing here commits. The deterministic checks, the submission/task/claim
    transitions, the escrow transition, the reputation event, and the outbox
    event are all applied to the same session, so they either become durable
    together or not at all.

    A failing check is a decision, not an error: the proof is rejected and the
    requester is refunded. Escrow and ledger arithmetic stays inside the
    settlement provider boundary with exact Decimal/string accounting, and a
    settlement conflict aborts the whole submission instead of leaving a
    half-applied state.
    """
    evaluation = evaluate_deterministic(db, task, submission)
    decision = "REJECTED" if evaluation.fatal else "VERIFIED"
    failed_checks = [item["code"] for item in evaluation.checks if item["result"] == "FAIL"]

    submission.status = decision
    task.status = decision
    task.updated_at = now()

    try:
        settle_escrow(
            db,
            task=task,
            executor_did=submission.executor_did,
            decision=decision,
        )
    except ValueError as exc:
        db.rollback()
        raise http_error(409 if isinstance(exc, AccountingConflict) else 400, str(exc))

    claim.status = "COMPLETED"
    claim.updated_at = now()
    add_reputation(
        db,
        did=submission.executor_did,
        role="executor",
        kind=f"task_{decision.lower()}",
        delta=1.0 if decision == "VERIFIED" else -1.0,
        capability=next(iter(required_capability_names(task)), None),
        reference_type="submission",
        reference_id=submission.id,
    )
    add_audit(
        db,
        actor_did=submission.executor_did,
        kind=f"TASK_{decision}",
        aggregate_type="task",
        aggregate_id=task.id,
        payload={
            "task_id": task.id,
            "submission_id": submission.id,
            "verification_strategy": "deterministic",
            "deterministic_checks": evaluation.checks,
            "failed_checks": failed_checks,
        },
    )
    queue_request_outbox(
        request,
        db,
        kind=f"TASK_{decision}",
        aggregate_id=task.id,
        payload={
            "task_id": task.id,
            "submission_id": submission.id,
            # Dual attribution: the settled exchange names both counterparties,
            # while actor/causation attribute the request that caused it.
            "poster_did": task.poster_did,
            "executor_did": submission.executor_did,
            "status": decision,
            "decision": decision,
            "verification_strategy": "deterministic",
        },
    )
    return {
        "strategy": "deterministic",
        "decision": decision,
        "deterministic_checks": evaluation.checks,
        "failed_checks": failed_checks,
    }


@router.post("/api/v1/tasks/{task_id}/inference")
async def create_inference(
    task_id: str,
    body: InferenceRequestCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    did = await authenticate(request, db)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    task = db.get(Task, task_id)
    if not task or task.claim_id is None:
        raise http_error(404, "claimed task not found")
    claim = db.get(Claim, task.claim_id)
    if not claim or claim.executor_did != did or claim.status != "ACTIVE":
        raise http_error(403, "agent does not own an active claim")
    if not kernel.guard_active_claim(db, claim):
        raise http_error(409, "claim is no longer active")
    db.refresh(task)
    if deadline_passed(db, task.deadline, reference=server_deadline_reference(db, task.deadline)):
        discard_idempotency(request, db)
        mark_task_deadline_expired(db, task, claim)
        raise http_error(409, "task deadline has passed")
    try:
        provider = get_provider(body.provider)
        session_data = await provider.create(body)
    except ProviderUnavailable as exc:
        raise http_error(503, str(exc))

    # Server receipt time, taken before the provider call: the lease is never
    # extended by however long an inference round-trip happened to take.
    timestamp = received_at_of(request)
    session = InferenceSession(
        id=session_data.id,
        task_id=task.id,
        executor_did=did,
        provider=session_data.provider,
        request=body.model_dump(mode="json"),
        status=session_data.status,
        result=session_data.result,
        receipt=session_data.receipt,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(session)
    task.status = "EXECUTING"
    claim.heartbeat_at = timestamp
    claim.lease_expires_at = lease_expiry(timestamp, kernel.CLAIM_LEASE_SECONDS)
    claim.received_at = timestamp
    claim.updated_at = timestamp
    add_audit(db, actor_did=did, kind="INFERENCE_CREATED", aggregate_type="inference", aggregate_id=session.id, payload={"task_id": task.id, "provider": session.provider})
    response_body = {
        "session_id": session.id,
        "task_id": task.id,
        "submission_id": session.submission_id,
        "status": session.status,
        "result": session.result,
        "receipt": session.receipt,
    }
    finish_idempotency(request, response_body)
    db.commit()
    return response_body


@router.get("/api/v1/inference/{session_id}")
async def get_inference(session_id: str, request: Request, db: Session = Depends(get_db)):
    session = db.get(InferenceSession, session_id)
    if not session:
        raise http_error(404, "inference session not found")
    task = db.get(Task, session.task_id)
    if not task:
        raise http_error(404, "task not found")
    await authorize_task_read(request, db, task)
    return {
        "session_id": session.id,
        "task_id": session.task_id,
        "submission_id": session.submission_id,
        "provider": session.provider,
        "status": session.status,
        "result": session.result,
        "receipt": session.receipt,
    }


@router.post("/api/v1/tasks/{task_id}/submissions")
async def submit_task(
    task_id: str,
    body: SubmissionCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    did = await authenticate(request, db)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    task = db.get(Task, task_id)
    if not task or not task.claim_id:
        raise http_error(404, "claimed task not found")
    claim = db.get(Claim, task.claim_id)
    if not claim or claim.executor_did != did:
        raise http_error(403, "agent does not own this task")
    if claim.status != "ACTIVE":
        raise http_error(409, "claim is not submit-capable")
    if not kernel.guard_active_claim(db, claim):
        raise http_error(409, "claim is no longer active")
    db.refresh(task)
    # The submission deadline is decided by the server, against database
    # server time: never by the executor's clock and never by the
    # ``created_at`` it declares inside the proof.
    received = received_at_of(request)
    if deadline_passed(db, task.deadline, reference=server_deadline_reference(db, task.deadline)):
        discard_idempotency(request, db)
        mark_task_deadline_expired(db, task, claim)
        raise http_error(409, "task deadline has passed")
    if db.get(Submission, body.submission_id):
        raise http_error(409, "submission ID already exists")

    for session_id in body.inference_session_ids:
        session = db.get(InferenceSession, session_id)
        if not session or session.task_id != task.id or session.executor_did != did:
            raise http_error(400, f"invalid inference session: {session_id}")

    # ``created_at`` stays the executor-declared instant inside the signed
    # proof (changing it would break every existing proof signature), but it
    # no longer decides anything: ``received_at`` is the server's own receipt
    # time and is what the deadline checks and the deterministic validator
    # compare against.
    created_at = body.created_at or received
    if task.deadline and created_at > task.deadline:
        raise http_error(409, "submission is after the task deadline")
    result_hash = sha256_json(body.result)
    proof_core = {
        "task_id": task.id,
        "submission_id": body.submission_id,
        "executor_did": did,
        "input_hash": sha256_json(task.input_data),
        "result_hash": result_hash,
        "evidence": body.evidence,
        "inference_session_ids": body.inference_session_ids,
        "created_at": created_at,
    }
    if not verify_signature(
        did,
        canonical_json(proof_core).encode("utf-8"),
        body.proof_signature,
    ):
        raise http_error(401, "invalid proof signature")

    proof = {**proof_core, "signature": body.proof_signature}
    submission = Submission(
        id=body.submission_id,
        task_id=task.id,
        executor_did=did,
        result=body.result,
        evidence=body.evidence,
        inference_session_ids=body.inference_session_ids,
        proof=proof,
        result_hash=result_hash,
        proof_hash=sha256_json(proof),
        status="SUBMITTED",
        received_at=received,
        created_at=created_at,
        updated_at=received,
    )
    db.add(submission)
    for session_id in body.inference_session_ids:
        session = db.get(InferenceSession, session_id)
        if session and session.submission_id not in {None, submission.id}:
            db.rollback()
            raise http_error(409, "inference session is already attached to another submission")
        if session:
            session.submission_id = submission.id
    task.status = "SUBMITTED"
    claim.status = "SUBMITTED"
    task.updated_at = now()
    add_audit(db, actor_did=did, kind="TASK_SUBMITTED", aggregate_type="submission", aggregate_id=submission.id, payload={"task_id": task.id, "proof_hash": submission.proof_hash})
    queue_request_outbox(request, db, kind="PROOF_SUBMITTED", aggregate_id=submission.id, payload={"task_id": task.id, "submission_id": submission.id, "proof_hash": submission.proof_hash})
    if verification_strategy_of(task) == "deterministic":
        # No validator is involved: the proof is evaluated and the task is
        # settled inside this same transaction (one atomic commit below).
        verification = deterministic_auto_settlement(
            db,
            task=task,
            submission=submission,
            claim=claim,
            request=request,
        )
        # Render the view after settlement so the client sees the verdict.
        response_body = submission_view(submission)
        response_body["verification_strategy"] = "deterministic"
        response_body["verification"] = verification
    else:
        response_body = submission_view(submission)
    finish_idempotency(request, response_body)
    db.commit()
    return response_body


@router.get("/api/v1/submissions/{submission_id}")
async def get_submission(submission_id: str, request: Request, db: Session = Depends(get_db)):
    submission = db.get(Submission, submission_id)
    if not submission:
        raise http_error(404, "submission not found")
    task = db.get(Task, submission.task_id)
    if not task:
        raise http_error(404, "task not found")
    await authorize_task_read(request, db, task)
    return submission_view(submission)


@router.get("/api/v1/proofs/{submission_id}")
async def get_proof(submission_id: str, request: Request, db: Session = Depends(get_db)):
    submission = db.get(Submission, submission_id)
    if not submission:
        raise http_error(404, "submission not found")
    task = db.get(Task, submission.task_id)
    if not task:
        raise http_error(404, "task not found")
    await authorize_task_read(request, db, task)
    return {
        "submission_id": submission.id,
        "proof": submission.proof,
        "proof_hash": submission.proof_hash,
        "status": submission.status,
    }
