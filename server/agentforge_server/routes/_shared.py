"""Cross-domain HTTP plumbing shared by every domain router.

Everything here is request-scoped machinery rather than business logic:
authentication and nonce/idempotency handling, the signed-event causation
record, the read-authorization rule for non-public tasks, and the operator
gate for peer validation. Domain-specific view builders live with the domain
router that owns them (``tasks.task_view``, ``submissions.submission_view``,
``agents.agent_view``).

``kernel`` resolves to the :mod:`agentforge_server.app` module. A small number
of names the handlers depend on are read through it *at call time* instead of
being imported by value:

    MAX_LIST_BYTES, can_execute, guard_active_claim,
    guard_pending_submission, queue_outbox

The regression suites monkeypatch ``agentforge_server.app.<name>`` to prove the
API actually consults those guards and tunables (response-size budgeting, the
atomic claim guard, the pending-submission guard, the outbox write). A by-value
import would copy the original object into a router namespace and silently
detach the patch -- and for these names it does not even work: ``app`` imports
this package while it is still executing, so ``from ..app import X`` raises
``ImportError`` against a partially initialised module. Every other import in
the routers package is a normal, direct import.

``kernel`` is a proxy rather than the module object for the same reason. Binding
``from .. import app as kernel`` here would make importing this package require
``app`` to be importable, and ``app`` requires this package -- so whichever of
the two is imported first decides whether the other can load. Importing
``agentforge_server.routes`` on its own would fail. The proxy has no import-time
dependency on ``app`` at all; it looks the module up per attribute access, by
which point it is fully populated.
"""

from __future__ import annotations

import math
import sys
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from starlette.concurrency import run_in_threadpool

from .. import clock
from .. import operators as operator_registry
from ..admission import AdmissionDenied, consume_authenticated
from ..clock import ClockUnavailable
from ..crypto import request_signing_bytes, sha256_bytes, verify_signature
from ..models import Agent, Claim, IdempotencyRecord, OutboxEvent, Task, UsedNonce
from ..services import (
    begin_idempotency,
    capability_names,
    complete_idempotency,
    now,
    reap_expired_claims,
    received_at_of,
)
from ..settings import settings


class _Kernel:
    """Attribute proxy onto :mod:`agentforge_server.app`.

    ``kernel.NAME`` reads ``agentforge_server.app.NAME`` at the moment of access,
    which is what lets the regression suites patch those names on the app module
    and have the routers see the patch. See the module docstring for why this is
    a proxy rather than a plain module import.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(sys.modules["agentforge_server.app"], name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<kernel proxy -> agentforge_server.app>"


kernel = _Kernel()


def http_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


def server_deadline_reference(db: Session, deadline: float | None) -> float | None:
    """Authoritative reference instant for a client-declared absolute deadline.

    Returns ``None`` when the task has no deadline (nothing to evaluate, no
    database round trip). Otherwise the value is the *database* server clock,
    cross-checked against the API host clock: if the two cannot be shown to
    agree, the request fails closed with 503 instead of settling a deadline on
    an ambiguous clock. A client clock is never a candidate here.
    """
    if deadline is None:
        return None
    try:
        return clock.deadline_reference(db)
    except ClockUnavailable as exc:
        raise http_error(503, "server clock is not synchronized") from exc


async def authenticate(
    request: Request,
    db: Session,
    *,
    require_idempotency: bool = True,
) -> str:
    did = request.headers.get("X-Agent-DID")
    timestamp = request.headers.get("X-Agent-Timestamp")
    nonce = request.headers.get("X-Agent-Nonce")
    signature = request.headers.get("X-Agent-Signature")
    idempotency_key = request.headers.get("Idempotency-Key")
    if not all([did, timestamp, nonce, signature]):
        raise http_error(401, "signed agent headers are required")
    if len(timestamp or "") > 160:
        raise http_error(401, "request timestamp is too long")
    if len(nonce or "") > 160:
        raise http_error(401, "request nonce is too long")
    if require_idempotency and not idempotency_key:
        raise http_error(400, "Idempotency-Key is required for state-changing requests")

    agent = db.get(Agent, did)
    if not agent or agent.status != "active":
        raise http_error(401, "unknown or inactive agent")

    # Authoritative receipt time, stamped by the ingress middleware from the
    # server's own monotonic clock before this handler ran. It is the
    # reference for the drift window below and the anchor for every lease,
    # expiry and deadline this request may create (Grok roadmap 1.4).
    received_at = received_at_of(request)
    request.state.received_at = received_at

    try:
        timestamp_value = float(timestamp)
    except (TypeError, ValueError):
        raise http_error(401, "invalid request timestamp")
    if not math.isfinite(timestamp_value):
        raise http_error(401, "invalid request timestamp")
    # Strict clock-drift tolerance, measured against the server clock. A
    # stale request cannot be replayed later and a futuristic one cannot
    # pre-date a lease it has not earned. The client timestamp is only ever
    # compared here: it is never stored as, or added to, a server deadline.
    if abs(clock.drift_seconds(timestamp_value, received_at)) > settings.request_clock_skew_seconds:
        raise http_error(401, "client clock drift exceeds tolerance")

    raw_body = await request.body()
    message = request_signing_bytes(
        request.method,
        request.url.path,
        raw_body,
        timestamp,
        nonce,
    )
    if not verify_signature(did, message, signature):
        raise http_error(401, "invalid request signature")

    try:
        await run_in_threadpool(consume_authenticated, did, db)
    except AdmissionDenied as exc:
        raise HTTPException(429, "request quota exceeded", headers={"Retry-After": str(exc.retry_after)})
    except Exception:
        raise http_error(503, "admission unavailable")

    # Record the verified request as causal attribution for any outbox event
    # this request produces. request_id is the single-use request nonce, which
    # the server already persists in used_nonces.
    request.state.actor_did = did
    request.state.causation = {
        "request_id": nonce,
        "request_signature": signature,
        "request_timestamp": timestamp,
        "request_body_hash": "sha256:" + sha256_bytes(raw_body),
        "method": request.method.upper(),
        "path": request.url.path,
    }

    # Reap before authorizing work so expired claims cannot remain usable.
    reap_expired_claims(db)
    used = UsedNonce(did=did, nonce=nonce, created_at=now())
    db.add(used)
    try:
        # Consume the nonce before business logic so failed requests cannot
        # be replayed later if their conditions change.
        db.commit()
    except IntegrityError:
        db.rollback()
        raise http_error(401, "request nonce has already been used")

    if require_idempotency:
        request_hash = sha256_bytes(raw_body)
        try:
            record, replay = begin_idempotency(
                db,
                did=did,
                key=idempotency_key or "",
                method=request.method,
                path=request.url.path,
                request_hash=request_hash,
            )
        except ValueError as exc:
            raise http_error(409, str(exc))
        except IntegrityError:
            # Two workers may reserve the same key concurrently. The
            # database unique constraint is authoritative; after rollback,
            # resolve the winner as a replay or conflict instead of leaking
            # a 500 response.
            db.rollback()
            record = db.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.did == did,
                    IdempotencyRecord.key == (idempotency_key or ""),
                )
            )
            if record is None:
                raise http_error(409, "idempotency reservation raced; retry the request")
            if (
                record.method != request.method.upper()
                or record.path != request.url.path
                or record.request_hash != request_hash
            ):
                raise http_error(409, "Idempotency-Key was reused with a different request")
            if record.status == "COMPLETED":
                replay = record.response_body or {}
            else:
                raise http_error(409, "an identical request is already in progress")
        request.state.idempotency_record = record
        if replay is not None:
            request.state.idempotency_replay = replay
            request.state.idempotency_status = record.response_status or 200
    return did


def idempotency_replay(request: Request) -> JSONResponse | None:
    payload = getattr(request.state, "idempotency_replay", None)
    if payload is None:
        return None
    return JSONResponse(
        content=payload,
        status_code=getattr(request.state, "idempotency_status", 200),
    )


def finish_idempotency(
    request: Request,
    payload: dict[str, Any],
    *,
    status_code: int = 200,
) -> None:
    record = getattr(request.state, "idempotency_record", None)
    if record is not None and getattr(request.state, "idempotency_replay", None) is None:
        complete_idempotency(
            record,
            response_status=status_code,
            response_body=payload,
        )


def discard_idempotency(request: Request, db: Session) -> None:
    """Do not leave an IN_PROGRESS record when an error path commits state."""
    record = getattr(request.state, "idempotency_record", None)
    if record is not None and getattr(request.state, "idempotency_replay", None) is None:
        db.delete(record)


def queue_request_outbox(
    request: Request,
    db: Session,
    *,
    kind: str,
    aggregate_id: str,
    payload: dict[str, Any],
) -> OutboxEvent:
    """Queue an event with the verified request recorded as its causation.

    The outbox event carries the acting DID plus the verified request
    signature, request nonce, and body hash, so a published envelope can show
    both "this DID requested this operation" and "this instance recorded the
    resulting transition".
    """
    # ``kernel.queue_outbox`` is a deliberate call-time lookup: the accounting
    # regression suite patches it on the app module to prove an outbox write
    # failure rolls back the whole task/escrow/ledger transaction.
    return kernel.queue_outbox(
        db,
        kind=kind,
        aggregate_id=aggregate_id,
        payload=payload,
        actor_did=getattr(request.state, "actor_did", None),
        causation=getattr(request.state, "causation", None),
    )


def require_validator_operator(db: Session, did: str) -> None:
    """Operator authorization gate for peer-validation decisions (Grok 1.1).

    Three independent conditions must hold before an agent may submit a
    validation decision:

    1. the DID is on the operator-approved validator allowlist
       (``AGENTFORGE_TRUSTED_VALIDATOR_DIDS``) — existing D1 control;
    2. the agent declares a ``validation``/``validator`` capability;
    3. the agent holds an effective ``validator`` registry role: an
       ``ACTIVE`` row in ``operator_role_grants``, or — only while the
       development ``OPEN_OPERATORS=true`` self-registration mode is
       enabled — its declared capability.

    A capability alone is never authority: in restricted mode a missing or
    revoked registry grant is rejected with 403
    ("agent not authorized as validator"). The check runs before
    idempotency replay, so a revoked role cannot replay a past decision.
    """
    if did not in settings.trusted_validator_dids or not {"validation", "validator"}.intersection(capability_names(db, did)):
        raise http_error(403, "validator is not approved or validation-capable")
    if not operator_registry.validator_role_active(db, did):
        raise http_error(403, "agent not authorized as validator")


async def authorize_task_read(request: Request, db: Session, task: Task) -> str | None:
    if task.visibility == "public":
        return None
    try:
        did = await authenticate(request, db, require_idempotency=False)
    except HTTPException:
        raise http_error(404, "task not found")
    claim = db.get(Claim, task.claim_id) if task.claim_id else None
    if did in {task.poster_did, claim.executor_did if claim else None}:
        return did
    if did in settings.trusted_validator_dids and {"validation", "validator"}.intersection(capability_names(db, did)):
        return did
    raise http_error(404, "task not found")
