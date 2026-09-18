"""AgentForge pre-testnet MVP HTTP API."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import secrets
from decimal import Decimal, InvalidOperation
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from starlette.concurrency import run_in_threadpool

from .admission import AdmissionDenied, consume_authenticated, validate_security_configuration
from .middleware import MAX_BODY_BYTES, RequestSecurityMiddleware
from .validators.result_schema import UnsafeSchema, check_result_schema
from . import db as database
from .crypto import (
    canonical_json,
    registration_signing_bytes,
    request_signing_bytes,
    sha256_bytes,
    sha256_json,
    verify_signature,
)
from .db import get_db, init_db, verify_schema
from .models import (
    Agent,
    AgentCapability,
    AuditEvent,
    Claim,
    Dispute,
    Escrow,
    IdempotencyRecord,
    InferenceSession,
    LedgerAccount,
    OutboxEvent,
    RegistrationChallenge,
    Submission,
    Task,
    UsedNonce,
    ValidationDecision,
)
from .providers import ProviderUnavailable, get_provider
from .schemas import (
    AgentManifest,
    DisputeCreate,
    InferenceRequestCreate,
    RegistrationRequest,
    SubmissionCreate,
    TaskCreate,
    ValidationCreate,
)
from .services import (
    add_audit,
    add_reputation,
    AccountingConflict,
    begin_idempotency,
    guard_pending_submission,
    build_anti_circularity,
    can_execute,
    capability_names,
    complete_idempotency,
    derive_activity_eligibility,
    independence_failures,
    escrow_settle,
    fund_task,
    guard_active_claim,
    mark_task_deadline_expired,
    new_id,
    now,
    queue_outbox,
    reap_expired_claims,
    reputation_for,
    required_capability_names,
    task_outbox_payload,
    task_payload,
    verified_provenance_level,
)
from .settings import settings
from .validators import validate_submission as deterministic_validate


CLAIM_LEASE_SECONDS = 15 * 60
MAX_LIST_BYTES = 4_000_000


def http_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.environment == "production":
        if database.DATABASE_URL.startswith("sqlite"):
            raise RuntimeError("SQLite schema is not allowed in production")
        verify_schema(require_migrations=True)
    elif settings.auto_create_schema:
        init_db()
    else:
        verify_schema()
    validate_security_configuration()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="AgentForge Agent Work Exchange",
        version="0.1.0",
        description="Open protocol-oriented agent tasks, proofs, validation, reputation, and mock escrow.",
        lifespan=lifespan,
    )

    app.add_middleware(RequestSecurityMiddleware)

    @app.get("/", include_in_schema=False)
    def root():
        web_path = Path(__file__).resolve().parents[2] / "web" / "index.html"
        if web_path.exists():
            return FileResponse(web_path)
        return {
            "name": settings.server_name,
            "version": "0.1.0",
            "protocol": "/api/v1",
            "docs": "/docs",
            "openapi": "/openapi.json",
        }

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "agentforge", "version": "0.1.0"}

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

        try:
            timestamp_value = float(timestamp)
        except (TypeError, ValueError):
            raise http_error(401, "invalid request timestamp")
        if not math.isfinite(timestamp_value):
            raise http_error(401, "invalid request timestamp")
        if abs(now() - timestamp_value) > settings.request_clock_skew_seconds:
            raise http_error(401, "request timestamp outside allowed clock skew")

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
        return queue_outbox(
            db,
            kind=kind,
            aggregate_id=aggregate_id,
            payload=payload,
            actor_did=getattr(request.state, "actor_did", None),
            causation=getattr(request.state, "causation", None),
        )

    def agent_view(db: Session, agent: Agent) -> dict[str, Any]:
        capabilities = db.scalars(
            select(AgentCapability).where(AgentCapability.agent_did == agent.did)
        ).all()
        manifest = agent.manifest or {}
        return {
            "did": agent.did,
            "name": agent.name,
            "status": agent.status,
            "capabilities": [
                {"name": row.name, "level": row.level, "metadata": row.metadata_json}
                for row in capabilities
            ],
            "chains": manifest.get("chains", []),
            "endpoint_mode": manifest.get("endpoint_mode", "outbound_events"),
            "created_at": agent.created_at,
            "reputation": reputation_for(db, agent.did),
        }

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
        }

    def task_view(db: Session, task: Task) -> dict[str, Any]:
        result = task_payload(task)
        result["escrow"] = escrow_view(db, task.id)
        return result

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

    @app.get("/api/v1/register/challenge")
    def registration_challenge(db: Session = Depends(get_db)):
        challenge_id = new_id("CH")
        nonce = secrets.token_urlsafe(24)
        created = now()
        record = RegistrationChallenge(
            challenge_id=challenge_id,
            nonce=nonce,
            created_at=created,
            expires_at=created + settings.challenge_ttl_seconds,
            used=False,
        )
        db.add(record)
        db.commit()
        return {
            "challenge_id": challenge_id,
            "nonce": nonce,
            "expires_at": record.expires_at,
        }

    @app.post("/api/v1/agents/register")
    def register_agent(body: RegistrationRequest, db: Session = Depends(get_db)):
        challenge = db.get(RegistrationChallenge, body.challenge_id)
        if not challenge or challenge.used or challenge.expires_at < now():
            raise http_error(400, "challenge is missing, expired, or already used")
        if challenge.nonce != body.nonce:
            raise http_error(400, "challenge nonce mismatch")

        # Sign only fields explicitly supplied by the registrant. Server-side
        # defaults are stored in the normalized manifest but are not silently
        # added to the object the agent signed.
        signed_manifest = body.manifest.model_dump(mode="json", exclude_unset=True)
        manifest = body.manifest.model_dump(mode="json")
        message = registration_signing_bytes(
            body.challenge_id,
            body.nonce,
            body.did,
            signed_manifest,
        )
        if not verify_signature(body.did, message, body.signature):
            raise http_error(401, "invalid registration signature")

        timestamp = now()
        agent = db.get(Agent, body.did)
        is_new = agent is None
        if is_new and not settings.registration_open:
            raise http_error(403, "new agent registration is closed")
        # Single-use challenge is a compare-and-set, including concurrent registration.
        consumed = db.execute(update(RegistrationChallenge).where(
            RegistrationChallenge.challenge_id == body.challenge_id,
            RegistrationChallenge.used.is_(False),
            RegistrationChallenge.expires_at >= now(),
        ).values(used=True).execution_options(synchronize_session=False))
        if consumed.rowcount != 1:
            raise http_error(409, "registration challenge already consumed or expired")
        if is_new:
            agent = Agent(
                did=body.did,
                name=body.manifest.name,
                manifest=manifest,
                status="active",
                operator_group=body.manifest.operator_group,
                infrastructure_group=body.manifest.infrastructure_group,
                created_at=timestamp,
                updated_at=timestamp,
            )
            db.add(agent)
            db.flush()
        else:
            agent.name = body.manifest.name
            agent.manifest = manifest
            agent.operator_group = body.manifest.operator_group
            agent.infrastructure_group = body.manifest.infrastructure_group
            agent.updated_at = timestamp

        db.execute(delete(AgentCapability).where(AgentCapability.agent_did == body.did))
        for item in body.manifest.capabilities:
            if isinstance(item, str):
                name, level, metadata = item, 1, {}
            else:
                item_data = item.model_dump(mode="json")
                name = item_data["name"]
                level = item_data.get("level", 1)
                metadata = item_data.get("metadata", {})
            db.add(
                AgentCapability(
                    agent_did=body.did,
                    name=name,
                    level=level,
                    metadata_json=metadata,
                )
            )

        if is_new and settings.enable_mock_faucet:
            from .services import ensure_account

            ensure_account(db, body.did, "MOCK", "1000")
            # Also fund TEST_CREDIT for local test asset coverage
            ensure_account(db, body.did, "TEST_CREDIT", "1000")

        challenge.used = True
        add_audit(
            db,
            actor_did=body.did,
            kind="AGENT_REGISTERED",
            aggregate_type="agent",
            aggregate_id=body.did,
            payload={"new": is_new},
        )
        db.commit()
        return agent_view(db, agent)

    @app.get("/api/v1/agents/search")
    def search_agents(
        capability: str | None = None,
        chain: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        db: Session = Depends(get_db),
    ):
        statement = select(Agent).where(Agent.status == "active")
        if capability:
            statement = statement.where(Agent.did.in_(select(AgentCapability.agent_did).where(AgentCapability.name == capability)))
        agents = db.scalars(statement.order_by(Agent.did).limit(500).execution_options(yield_per=1))
        result = []
        response_bytes = 0
        for agent in agents:
            caps = capability_names(db, agent.did)
            chains = set((agent.manifest or {}).get("chains", []))
            if capability and capability not in caps:
                continue
            if chain and chain not in chains:
                continue
            item = agent_view(db, agent)
            size = len(canonical_json(item).encode())
            if response_bytes + size > MAX_LIST_BYTES:
                break
            result.append(item)
            response_bytes += size
            if len(result) >= limit:
                break
        return {"agents": result}

    @app.get("/api/v1/agents/{did}")
    def get_agent(did: str, db: Session = Depends(get_db)):
        agent = db.get(Agent, did)
        if not agent:
            raise http_error(404, "agent not found")
        return agent_view(db, agent)

    @app.get("/api/v1/agents/{did}/balance")
    async def get_balance(did: str, request: Request, asset: str = "MOCK", db: Session = Depends(get_db)):
        if not db.get(Agent, did):
            raise http_error(404, "agent not found")
        caller = await authenticate(request, db, require_idempotency=False)
        if caller != did:
            raise http_error(403, "balance is only available to the account owner")
        account = db.scalar(
            select(LedgerAccount).where(
                LedgerAccount.did == did,
                LedgerAccount.asset == asset,
            )
        )
        return {"did": did, "asset": asset, "balance": account.balance if account else "0"}

    @app.get("/api/v1/capabilities")
    def list_capabilities(db: Session = Depends(get_db)):
        rows = db.execute(select(AgentCapability.name, func.count()).group_by(
            AgentCapability.name
        ).order_by(AgentCapability.name).limit(100)).all()
        return {"capabilities": [{"name": name, "agent_count": count} for name, count in rows]}

    @app.post("/api/v1/tasks")
    async def create_task(body: TaskCreate, request: Request, db: Session = Depends(get_db)):
        did = await authenticate(request, db)
        replay = idempotency_replay(request)
        if replay is not None:
            return replay
        validate_task_inputs(body)

        task_id = new_id("T")
        created = now()
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

    @app.get("/api/v1/tasks")
    def list_tasks(
        status: str | None = None,
        capability: str | None = None,
        chain: str | None = None,
        origin: str | None = None,
        min_reward: str | None = Query(default=None, max_length=80),
        limit: int = Query(default=50, ge=1, le=100),
        db: Session = Depends(get_db),
    ):
        result = []
        response_bytes = 0
        from .services import dec

        minimum = None
        if min_reward is not None:
            try:
                minimum = Decimal(min_reward)
            except InvalidOperation:
                raise http_error(422, "min_reward must be a finite nonnegative decimal")
            if not minimum.is_finite() or minimum < 0 or abs(minimum.adjusted()) > 80:
                raise http_error(422, "min_reward must be a bounded finite nonnegative decimal")
        reap_expired_claims(db)
        statement = select(Task).where(Task.visibility == "public")
        if status:
            statement = statement.where(Task.status == status)
        if origin:
            statement = statement.where(Task.origin == origin)
        tasks = db.scalars(statement.order_by(Task.created_at.desc()).limit(500).execution_options(yield_per=1))
        for task in tasks:
            if task.visibility != "public":
                continue
            if status and task.status != status:
                continue
            if origin and task.origin != origin:
                continue
            if capability and capability not in required_capability_names(task):
                continue
            if chain and chain not in (task.chains or []):
                continue
            if minimum is not None:
                reward = dec(((task.economics or {}).get("reward") or {}).get("amount", "0"))
                if reward < minimum:
                    continue
            item = task_view(db, task)
            size = len(canonical_json(item).encode())
            if response_bytes + size > MAX_LIST_BYTES:
                break
            result.append(item)
            response_bytes += size
            if len(result) >= limit:
                break
        return {"tasks": result}

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: str, request: Request, db: Session = Depends(get_db)):
        reap_expired_claims(db)
        task = db.get(Task, task_id)
        if not task:
            raise http_error(404, "task not found")
        await authorize_task_read(request, db, task)
        return task_view(db, task)

    @app.post("/api/v1/tasks/{task_id}/cancel")
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

    def active_claim_count(db: Session, did: str) -> int:
        return db.scalar(
            select(func.count(Claim.id)).where(
                Claim.executor_did == did,
                Claim.status == "ACTIVE",
                Claim.lease_expires_at > now(),
            )
        ) or 0

    @app.post("/api/v1/tasks/{task_id}/claim")
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
        if task.deadline and task.deadline <= now():
            discard_idempotency(request, db)
            mark_task_deadline_expired(db, task)
            raise http_error(409, "task deadline has passed")
        if not can_execute(db, task, did):
            raise http_error(403, "agent does not match required capabilities")
        conflicts = independence_failures(db, task=task, participant_did=did, role="executor")
        if conflicts:
            raise http_error(403, "executor is not independent: " + ", ".join(conflicts))
        if active_claim_count(db, did) >= 10:
            raise http_error(429, "active claim limit reached")

        claim_id = new_id("C")
        created = now()
        claim = Claim(
            id=claim_id,
            task_id=task.id,
            executor_did=did,
            attempt=1,
            status="ACTIVE",
            lease_expires_at=created + CLAIM_LEASE_SECONDS,
            heartbeat_at=created,
            created_at=created,
            updated_at=created,
        )
        db.add(claim)
        task.status = "CLAIMED"
        task.claim_id = claim.id
        task.state_version += 1
        task.updated_at = created
        add_audit(db, actor_did=did, kind="TASK_CLAIMED", aggregate_type="task", aggregate_id=task.id, payload={"claim_id": claim.id})
        queue_request_outbox(request, db, kind="TASK_CLAIMED", aggregate_id=task.id, payload={"task_id": task.id, "claim_id": claim.id, "executor_did": did})
        response_body = {
            "claim_id": claim.id,
            "task_id": task.id,
            "executor_did": did,
            "status": claim.status,
            "lease_expires_at": claim.lease_expires_at,
        }
        finish_idempotency(request, response_body)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise http_error(409, "task already has an active claim")
        return response_body

    @app.post("/api/v1/claims/{claim_id}/heartbeat")
    async def heartbeat(claim_id: str, request: Request, db: Session = Depends(get_db)):
        did = await authenticate(request, db)
        replay = idempotency_replay(request)
        if replay is not None:
            return replay
        claim = db.get(Claim, claim_id)
        if not claim or claim.executor_did != did:
            raise http_error(404, "claim not found")
        if not guard_active_claim(db, claim):
            raise http_error(409, "claim is no longer active")
        task = db.get(Task, claim.task_id, populate_existing=True)
        if task and task.deadline and task.deadline <= now():
            discard_idempotency(request, db)
            mark_task_deadline_expired(db, task, claim)
            raise http_error(409, "task deadline has passed")
        claim.heartbeat_at = now()
        claim.lease_expires_at = claim.heartbeat_at + CLAIM_LEASE_SECONDS
        claim.updated_at = claim.heartbeat_at
        response_body = {"claim_id": claim.id, "lease_expires_at": claim.lease_expires_at}
        finish_idempotency(request, response_body)
        db.commit()
        return response_body

    @app.post("/api/v1/tasks/{task_id}/inference")
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
        if not guard_active_claim(db, claim):
            raise http_error(409, "claim is no longer active")
        db.refresh(task)
        if task.deadline and task.deadline <= now():
            discard_idempotency(request, db)
            mark_task_deadline_expired(db, task, claim)
            raise http_error(409, "task deadline has passed")
        try:
            provider = get_provider(body.provider)
            session_data = await provider.create(body)
        except ProviderUnavailable as exc:
            raise http_error(503, str(exc))

        timestamp = now()
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
        claim.lease_expires_at = timestamp + CLAIM_LEASE_SECONDS
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

    @app.get("/api/v1/inference/{session_id}")
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

    @app.post("/api/v1/tasks/{task_id}/submissions")
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
        if not guard_active_claim(db, claim):
            raise http_error(409, "claim is no longer active")
        db.refresh(task)
        if task.deadline and task.deadline <= now():
            discard_idempotency(request, db)
            mark_task_deadline_expired(db, task, claim)
            raise http_error(409, "task deadline has passed")
        if db.get(Submission, body.submission_id):
            raise http_error(409, "submission ID already exists")

        for session_id in body.inference_session_ids:
            session = db.get(InferenceSession, session_id)
            if not session or session.task_id != task.id or session.executor_did != did:
                raise http_error(400, f"invalid inference session: {session_id}")

        created_at = body.created_at or now()
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
            created_at=created_at,
            updated_at=now(),
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
        response_body = submission_view(submission)
        finish_idempotency(request, response_body)
        db.commit()
        return response_body

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

    @app.get("/api/v1/submissions/{submission_id}")
    async def get_submission(submission_id: str, request: Request, db: Session = Depends(get_db)):
        submission = db.get(Submission, submission_id)
        if not submission:
            raise http_error(404, "submission not found")
        task = db.get(Task, submission.task_id)
        if not task:
            raise http_error(404, "task not found")
        await authorize_task_read(request, db, task)
        return submission_view(submission)

    @app.get("/api/v1/proofs/{submission_id}")
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

    def validator_allowed(db: Session, validator_did: str, task: Task, submission: Submission) -> bool:
        if validator_did not in settings.trusted_validator_dids or validator_did in {task.poster_did, submission.executor_did}:
            return False
        caps = capability_names(db, validator_did)
        if not ({"validation", "validator"} & caps):
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
        if not guard_pending_submission(db, submission):
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

    @app.post("/api/v1/submissions/{submission_id}/validate")
    async def validate_submission(
        submission_id: str,
        body: ValidationCreate,
        request: Request,
        db: Session = Depends(get_db),
    ):
        did = await authenticate(request, db)
        if did not in settings.trusted_validator_dids or not {"validation", "validator"}.intersection(capability_names(db, did)):
            raise http_error(403, "validator is not approved or validation-capable")
        replay = idempotency_replay(request)
        if replay is not None:
            return replay
        submission = db.get(Submission, submission_id)
        if not submission:
            raise http_error(404, "submission not found")
        if submission.status not in {"SUBMITTED", "DISPUTED"}:
            raise http_error(409, "submission is already terminal")
        task = db.get(Task, submission.task_id)
        if not task:
            raise http_error(404, "task not found")
        return apply_validation(
            db,
            task=task,
            submission=submission,
            validator_did=did,
            body=body,
            request=request,
        )

    @app.post("/api/v1/submissions/{submission_id}/disputes")
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
        if not guard_pending_submission(db, submission):
            raise http_error(409, "submission is already terminal")
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
            created_at=now(),
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

    @app.post("/api/v1/disputes/{dispute_id}/resolve")
    async def resolve_dispute(
        dispute_id: str,
        body: ValidationCreate,
        request: Request,
        db: Session = Depends(get_db),
    ):
        did = await authenticate(request, db)
        if did not in settings.trusted_validator_dids or not {"validation", "validator"}.intersection(capability_names(db, did)):
            raise http_error(403, "validator is not approved or validation-capable")
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

    @app.get("/api/v1/reputation/{did}")
    def get_reputation(did: str, db: Session = Depends(get_db)):
        if not db.get(Agent, did):
            raise http_error(404, "agent not found")
        return reputation_for(db, did)

    @app.get("/api/v1/events")
    async def events(
        request: Request,
        cursor: str | None = None,
        after_created_at: float = Query(default=0.0, ge=0),
        after_id: str = "",
        limit: int = Query(default=100, ge=1, le=200),
        db: Session = Depends(get_db),
    ):
        did = await authenticate(request, db, require_idempotency=False)
        if cursor:
            try:
                padded = cursor + "=" * (-len(cursor) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
                after_created_at = float(decoded["created_at"])
                after_id = str(decoded["id"])
            except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
                raise http_error(400, "invalid event cursor") from exc
        after_condition = or_(
            AuditEvent.created_at > after_created_at,
            and_(AuditEvent.created_at == after_created_at, AuditEvent.id > after_id),
        )
        selected = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.actor_did == did, after_condition)
            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
            .limit(limit + 1)
        ).all()
        has_more = len(selected) > limit
        selected = selected[:limit]
        next_cursor = None
        if selected:
            raw_cursor = json.dumps(
                {"created_at": selected[-1].created_at, "id": selected[-1].id},
                separators=(",", ":"),
            ).encode()
            next_cursor = base64.urlsafe_b64encode(raw_cursor).decode().rstrip("=")
        return {
            "events": [
                {
                    "id": row.id,
                    "actor_did": row.actor_did,
                    "kind": row.kind,
                    "aggregate_type": row.aggregate_type,
                    "aggregate_id": row.aggregate_id,
                    "payload": row.payload,
                    "created_at": row.created_at,
                }
                for row in selected
            ],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "agentforge_server.app:app",
        host=os.getenv("AGENTFORGE_HOST", "0.0.0.0"),
        port=int(os.getenv("AGENTFORGE_PORT", "8080")),
        reload=False,
    )


if __name__ == "__main__":
    main()
