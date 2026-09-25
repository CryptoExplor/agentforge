"""Task lifecycle on the requester side: creation and escrow funding, public
discovery with keyset/offset pagination, single-task read and cancellation.

Discovery is the only endpoint in the API that renders an unbounded number of
rows, so it is the one place that enforces a response-byte budget. The budget
is read through ``kernel.MAX_LIST_BYTES`` rather than imported by value so the
public-exposure regression suite can patch it on the app module.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Float, String, and_, func, or_, select, update
from sqlalchemy.orm import Session

from ..crypto import canonical_json, sha256_json
from ..db import get_db
from ..models import Escrow, Task
from ..money import fee_at_bps
from ..schemas import TaskCreate
from ..services import (
    AccountingConflict,
    add_audit,
    build_anti_circularity,
    derive_activity_eligibility,
    escrow_settle,
    fund_task,
    new_id,
    now,
    reap_expired_claims,
    received_at_of,
    task_outbox_payload,
    task_payload,
    verified_provenance_level,
)
from ..settings import settings
from ..validators.result_schema import UnsafeSchema, check_result_schema
from ._shared import (
    authenticate,
    authorize_task_read,
    finish_idempotency,
    http_error,
    idempotency_replay,
    kernel,
    queue_request_outbox,
)

router = APIRouter()


def escrow_view(db: Session, task_id: str) -> dict[str, Any] | None:
    escrow = db.get(Escrow, task_id)
    if not escrow:
        return None
    transitions = {
        "RELEASED": "FULL_RELEASE",
        "PARTIAL": "PARTIAL_RELEASE",
        "REFUNDED": "REFUND",
        "SLASHED": "SLASH",
    }
    return {
        "task_id": escrow.task_id,
        "status": escrow.status,
        "transition": transitions.get(escrow.status),
        "asset": escrow.asset,
        "reward_amount": escrow.reward_amount,
        "deposit_amount": escrow.deposit_amount,
        "inference_budget": escrow.inference_budget,
        "reserved_total": escrow.reserved_total,
        "released_amount": escrow.released_amount,
        "refunded_amount": escrow.refunded_amount,
        "slashed_amount": escrow.slashed_amount,
        "platform_fee_amount": escrow.platform_fee_amount,
    }


def task_view(db: Session, task: Task) -> dict[str, Any]:
    result = task_payload(task)
    result["escrow"] = escrow_view(db, task.id)
    return result


def validate_task_inputs(body: TaskCreate) -> None:
    for key in ("result_schema", "output_schema", "schema"):
        if key in body.acceptance:
            try:
                check_result_schema(body.acceptance[key])
            except (UnsafeSchema, RecursionError):
                raise http_error(422, "unsupported, invalid or overly complex acceptance schema")
    if not body.acceptance:
        raise http_error(400, "acceptance criteria are required")
    if body.generation_policy.synthetic_demo and body.demand_provenance.type != "synthetic_demo":
        raise http_error(400, "synthetic_demo policy requires synthetic_demo provenance")
    if body.demand_provenance.type == "synthetic_demo" and not body.generation_policy.synthetic_demo:
        raise http_error(400, "synthetic_demo provenance must be explicitly labelled")
    if body.economics.mode == "REPUTATION" and body.economics.reward.amount != "0":
        raise http_error(400, "REPUTATION tasks cannot have a monetary reward")
    # Generic marketplace service fee (mock ledger only). The operator cap
    # bounds every declared fee at creation; the effective fee is derived
    # later from the amount actually released, and refunds/slashes never
    # carry a fee.
    fee_mode = body.economics.service_fee_mode
    fee_bps = body.economics.service_fee_bps
    fee_fixed = body.economics.agentforge_service_fee.amount
    if fee_mode == "none":
        if fee_bps != 0 or fee_fixed != "0":
            raise http_error(400, "service fee mode 'none' cannot declare a fee value")
    else:
        if body.economics.mode == "REPUTATION":
            raise http_error(400, "REPUTATION tasks cannot carry a platform service fee")
        if fee_mode == "fixed":
            if fee_bps != 0:
                raise http_error(400, "fixed service fee mode cannot declare bps")
            max_fixed = fee_at_bps(Decimal(body.economics.reward.amount), settings.max_service_fee_bps)
            if Decimal(fee_fixed) > max_fixed:
                raise http_error(
                    400,
                    "declared service fee exceeds the operator cap "
                    f"({settings.max_service_fee_bps} bps of the reward)",
                )
        else:
            if fee_fixed != "0":
                raise http_error(400, "bps service fee mode cannot declare a fixed amount")
            if fee_bps > settings.max_service_fee_bps:
                raise http_error(
                    400,
                    f"declared service fee {fee_bps} bps exceeds the operator cap "
                    f"({settings.max_service_fee_bps} bps)",
                )


def decode_task_cursor(cursor: str) -> tuple[float, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        created = float(decoded["created_at"])
        task_id = str(decoded["id"])
        if not math.isfinite(created):
            raise ValueError("cursor timestamp must be finite")
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise http_error(400, "invalid task cursor") from exc
    return created, task_id


@router.post("/api/v1/tasks")
async def create_task(body: TaskCreate, request: Request, db: Session = Depends(get_db)):
    did = await authenticate(request, db)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    validate_task_inputs(body)

    task_id = new_id("T")
    # Server receipt time, not a clock read somewhere later in the handler:
    # the task's own timestamps are anchored the same way its claims are.
    created = received_at_of(request)
    required = list(dict.fromkeys(body.required_capabilities))
    provenance = body.demand_provenance.model_dump(mode="json")
    provenance["reported_level"] = provenance.get("level", 0)
    provenance["verified_level"] = verified_provenance_level(provenance)
    provenance["level"] = provenance["verified_level"]
    generation_policy = body.generation_policy.model_dump(mode="json")
    economics = body.economics.model_dump(mode="json")
    try:
        anti_circularity = build_anti_circularity(
            db,
            creator_did=did,
            parent_task_id=body.parent_task_id,
            max_depth=3,
        )
    except ValueError as exc:
        db.rollback()
        raise http_error(400, str(exc))
    anti_circularity["parent_observation_hash"] = provenance.get("novelty_hash")
    task_create_payload = body.model_dump(mode="json", by_alias=True)
    task_create_payload["required_capabilities"] = required
    acceptance_hash = sha256_json(body.acceptance)
    task_hash = sha256_json(
        {"poster_did": did, "task_id": task_id, "task": task_create_payload}
    )
    task = Task(
        id=task_id,
        version=1,
        kind=body.kind,
        visibility=body.visibility,
        origin=body.origin,
        poster_did=did,
        required_capabilities=required,
        chains=body.chains,
        input_data=body.input_data,
        acceptance=body.acceptance,
        demand_provenance=provenance,
        generation_policy=generation_policy,
        anti_circularity=anti_circularity,
        economics=economics,
        deadline=body.deadline,
        status="OPEN",
        verification_strategy=body.verification_strategy,
        activity_eligibility=derive_activity_eligibility(
            demand_provenance=provenance,
            generation_policy=generation_policy,
            economics=economics,
        ),
        acceptance_hash=acceptance_hash,
        task_hash=task_hash,
        created_at=created,
        updated_at=created,
    )
    db.add(task)
    try:
        db.flush()
        fund_task(db, task)
        queue_request_outbox(
            request,
            db,
            kind="TASK_CREATED",
            aggregate_id=task.id,
            payload=task_outbox_payload(task),
        )
        add_audit(
            db,
            actor_did=did,
            kind="TASK_CREATED",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={"status": task.status, "eligibility": task.activity_eligibility},
        )
        db.flush()
        response_body = task_view(db, task)
        finish_idempotency(request, response_body)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise http_error(409 if isinstance(exc, AccountingConflict) else 400, str(exc))
    return response_body


@router.get("/api/v1/tasks")
def list_tasks(
    status: str | None = None,
    kind: str | None = None,
    verification_strategy: str | None = None,
    capability: str | None = None,
    chain: str | None = None,
    origin: str | None = None,
    min_reward: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int | None = Query(default=None, ge=0, le=1_000_000),
    cursor: str | None = Query(default=None, max_length=500),
    db: Session = Depends(get_db),
):
    """SQL-backed public task discovery (MIMO 0.5).

    Every filter, the deterministic ``(created_at DESC, id DESC)`` ordering
    and the pagination window are evaluated by the database: equality
    filters use the ``tasks(kind)``, ``tasks(status)`` and
    ``tasks(verification_strategy)`` indexes, while the JSON-member filters
    (capability, chain, min_reward) compile to portable per-dialect SQL
    expressions. Nothing fetches unbounded rows for in-memory filtering.

    Pagination is cursor-based: pass ``limit`` (default 50, max 100) plus
    either an opaque ``cursor`` (keyset seek — preferred, stable under
    concurrent inserts) or a plain ``offset``. Legacy callers that pass
    only filters/limit keep the exact historical ``{"tasks": [...]}``
    response; passing ``cursor`` or ``offset`` additionally returns
    ``total``, ``limit``, ``offset``, ``has_more`` and ``next_cursor``.
    When ``cursor`` and ``offset`` are both supplied, the cursor wins.
    """
    reap_expired_claims(db)

    minimum: float | None = None
    if min_reward is not None:
        try:
            parsed = Decimal(min_reward)
        except InvalidOperation:
            raise http_error(422, "min_reward must be a finite nonnegative decimal")
        if not parsed.is_finite() or parsed < 0 or abs(parsed.adjusted()) > 80:
            raise http_error(422, "min_reward must be a bounded finite nonnegative decimal")
        minimum = float(parsed)

    conditions = [Task.visibility == "public"]
    if status:
        conditions.append(Task.status == status)
    if kind:
        conditions.append(Task.kind == kind)
    if verification_strategy:
        conditions.append(Task.verification_strategy == verification_strategy)
    if origin:
        conditions.append(Task.origin == origin)
    if capability:
        # JSON-member match: the token includes the JSON string delimiters,
        # so "security" cannot match a "proxy_security" entry.
        conditions.append(
            Task.required_capabilities.cast(String).contains(json.dumps(capability), autoescape=True)
        )
    if chain:
        conditions.append(Task.chains.cast(String).contains(json.dumps(chain), autoescape=True))
    if minimum is not None:
        reward_amount = Task.economics["reward"]["amount"].as_string()
        conditions.append(func.coalesce(func.cast(reward_amount, Float), 0.0) >= minimum)

    paged = cursor is not None or offset is not None
    if cursor is not None:
        cursor_created, cursor_id = decode_task_cursor(cursor)
        conditions.append(or_(
            Task.created_at < cursor_created,
            and_(Task.created_at == cursor_created, Task.id < cursor_id),
        ))
    sql_offset = 0 if cursor is not None else (offset or 0)

    # The deterministic total order (created_at DESC, id DESC) makes both
    # the keyset seek and offset pagination stable and repeatable.
    statement = select(Task).where(*conditions).order_by(Task.created_at.desc(), Task.id.desc())
    rows = db.scalars(
        statement.limit(limit + 1 if paged else limit).offset(sql_offset)
    ).all()

    has_more = paged and len(rows) > limit
    rows = rows[:limit]

    tasks: list[dict[str, Any]] = []
    response_bytes = 0
    byte_truncated = False
    for task in rows:
        item = task_view(db, task)
        size = len(canonical_json(item).encode())
        if response_bytes + size > kernel.MAX_LIST_BYTES:
            # Preserve the no-skip guarantee: the cursor continues from the
            # last rendered item instead of silently dropping tasks.
            byte_truncated = True
            break
        tasks.append(item)
        response_bytes += size

    if not paged:
        return {"tasks": tasks}

    total = int(db.scalar(select(func.count()).select_from(Task).where(*conditions)) or 0)
    has_more = has_more or byte_truncated
    next_cursor = None
    if has_more and tasks:
        last = tasks[-1]
        raw_cursor = json.dumps(
            {"created_at": last["created_at"], "id": last["id"]},
            separators=(",", ":"),
        ).encode()
        next_cursor = base64.urlsafe_b64encode(raw_cursor).decode().rstrip("=")
    return {
        "tasks": tasks,
        "total": total,
        "limit": limit,
        "offset": sql_offset,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


@router.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    reap_expired_claims(db)
    task = db.get(Task, task_id)
    if not task:
        raise http_error(404, "task not found")
    await authorize_task_read(request, db, task)
    return task_view(db, task)


@router.post("/api/v1/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request, db: Session = Depends(get_db)):
    did = await authenticate(request, db)
    replay = idempotency_replay(request)
    if replay is not None:
        return replay
    task = db.get(Task, task_id)
    if not task:
        raise http_error(404, "task not found")
    if task.poster_did != did:
        raise http_error(403, "only the requester may cancel a task")
    cancelled = db.execute(update(Task).where(
        Task.id == task.id, Task.status.in_(["OPEN", "FUNDED"]),
    ).values(status="CANCELLED", updated_at=now(), state_version=Task.state_version + 1)
        .execution_options(synchronize_session=False))
    if cancelled.rowcount != 1:
        raise http_error(409, "claimed or submitted tasks cannot be cancelled directly")
    db.refresh(task)
    try:
        escrow_settle(
            db,
            task=task,
            executor_did=did,
            decision="REJECTED",
            settlement={},
        )
    except ValueError as exc:
        db.rollback()
        raise http_error(409 if isinstance(exc, AccountingConflict) else 400, str(exc))
    task.status = "CANCELLED"
    task.updated_at = now()
    add_audit(db, actor_did=did, kind="TASK_CANCELLED", aggregate_type="task", aggregate_id=task.id, payload={})
    queue_request_outbox(request, db, kind="TASK_CANCELLED", aggregate_id=task.id, payload={"task_id": task.id})
    response_body = task_view(db, task)
    finish_idempotency(request, response_body)
    db.commit()
    return response_body
