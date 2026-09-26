"""Executor-side lease management: claiming a task and keeping the lease alive.

Both handlers are the reason the server clock is authoritative. A lease always
starts at the server's own receipt time for *this* request, so an executor
whose clock runs fast cannot buy a longer execution window and a slow clock
cannot shorten one, and a handler that stalls cannot retroactively gain time.

``can_execute`` and ``guard_active_claim`` are called through ``kernel`` because
the outbox and clock-drift regression suites patch them on the app module to
prove the API really consults the capability check and the atomic claim guard.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Agent, Claim, Task
from ..services import (
    add_audit,
    deadline_passed,
    independence_failures,
    lease_expiry,
    mark_task_deadline_expired,
    new_id,
    now,
    received_at_of,
)
from ._shared import (
    authenticate,
    discard_idempotency,
    finish_idempotency,
    http_error,
    idempotency_replay,
    kernel,
    queue_request_outbox,
    server_deadline_reference,
)

router = APIRouter()


def active_claim_count(db: Session, did: str) -> int:
    return db.scalar(
        select(func.count(Claim.id)).where(
            Claim.executor_did == did,
            Claim.status == "ACTIVE",
            Claim.lease_expires_at > now(),
        )
    ) or 0


@router.post("/api/v1/tasks/{task_id}/claim")
async def claim_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    did = await authenticate(request, db)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    # Serialize this executor's cross-task claim count. Task locks alone
    # cannot enforce the limit when two different tasks are claimed at once.
    active = db.execute(update(Agent).where(
        Agent.did == did, Agent.status == "active",
    ).values(status=Agent.status).execution_options(synchronize_session=False))
    if active.rowcount != 1:
        raise http_error(401, "unknown or inactive agent")
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if not task:
        raise http_error(404, "task not found")
    if task.status not in {"OPEN", "FUNDED"}:
        raise http_error(409, f"task is not claimable in state {task.status}")
    if deadline_passed(db, task.deadline, reference=server_deadline_reference(db, task.deadline)):
        discard_idempotency(request, db)
        mark_task_deadline_expired(db, task)
        raise http_error(409, "task deadline has passed")
    if not kernel.can_execute(db, task, did):
        raise http_error(403, "agent does not match required capabilities")
    conflicts = independence_failures(db, task=task, participant_did=did, role="executor")
    if conflicts:
        raise http_error(403, "executor is not independent: " + ", ".join(conflicts))
    if active_claim_count(db, did) >= 10:
        raise http_error(429, "active claim limit reached")

    claim_id = new_id("C")
    # The lease starts at the server's own receipt time for this request and
    # nowhere else: not the signed X-Agent-Timestamp, not a body field, not a
    # later clock read. An agent whose clock runs fast cannot buy itself a
    # longer execution window, and a slow clock cannot shorten one either.
    received = received_at_of(request)
    claim = Claim(
        id=claim_id,
        task_id=task.id,
        executor_did=did,
        attempt=1,
        status="ACTIVE",
        lease_expires_at=lease_expiry(received, kernel.CLAIM_LEASE_SECONDS),
        heartbeat_at=received,
        received_at=received,
        created_at=received,
        updated_at=received,
    )
    db.add(claim)
    task.status = "CLAIMED"
    task.claim_id = claim.id
    task.state_version += 1
    task.updated_at = received
    add_audit(db, actor_did=did, kind="TASK_CLAIMED", aggregate_type="task", aggregate_id=task.id, payload={"claim_id": claim.id})
    queue_request_outbox(request, db, kind="TASK_CLAIMED", aggregate_id=task.id, payload={"task_id": task.id, "claim_id": claim.id, "executor_did": did})
    response_body = {
        "claim_id": claim.id,
        "task_id": task.id,
        "executor_did": did,
        "status": claim.status,
        "lease_expires_at": claim.lease_expires_at,
        "received_at": claim.received_at,
    }
    finish_idempotency(request, response_body)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise http_error(409, "task already has an active claim")
    return response_body


@router.post("/api/v1/claims/{claim_id}/heartbeat")
async def heartbeat(claim_id: str, request: Request, db: Session = Depends(get_db)):
    did = await authenticate(request, db)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    claim = db.get(Claim, claim_id)
    if not claim or claim.executor_did != did:
        raise http_error(404, "claim not found")
    if not kernel.guard_active_claim(db, claim):
        raise http_error(409, "claim is no longer active")
    task = db.get(Task, claim.task_id, populate_existing=True)
    if task and deadline_passed(db, task.deadline, reference=server_deadline_reference(db, task.deadline)):
        discard_idempotency(request, db)
        mark_task_deadline_expired(db, task, claim)
        raise http_error(409, "task deadline has passed")
    # A heartbeat extends the lease from the server's receipt time for this
    # request only, so repeated heartbeats cannot accumulate extra time and a
    # forged client timestamp cannot move the expiry at all.
    received = received_at_of(request)
    claim.heartbeat_at = received
    claim.lease_expires_at = lease_expiry(received, kernel.CLAIM_LEASE_SECONDS)
    claim.received_at = received
    claim.updated_at = received
    response_body = {
        "claim_id": claim.id,
        "lease_expires_at": claim.lease_expires_at,
        "received_at": claim.received_at,
    }
    finish_idempotency(request, response_body)
    db.commit()
    return response_body
