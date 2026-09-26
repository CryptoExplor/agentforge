"""Agent identity: registration challenges, manifest registration, discovery,
balance lookup and the capability index.

Registration is the one write path that is not authenticated by a signature
over the HTTP request -- an agent has no server-side identity yet -- so it is
authenticated by a single-use, expiring challenge plus a signature over the
exact manifest fields the registrant supplied.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import operators as operator_registry
from ..crypto import canonical_json, registration_signing_bytes, verify_signature
from ..db import get_db
from ..models import Agent, AgentCapability, LedgerAccount, RegistrationChallenge
from ..schemas import RegistrationRequest
from ..services import add_audit, capability_names, new_id, now, reputation_for
from ..settings import settings
from ._shared import authenticate, http_error, kernel

router = APIRouter()


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


@router.get("/api/v1/register/challenge")
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


@router.post("/api/v1/agents/register")
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
    declared_capabilities: set[str] = set()
    for item in body.manifest.capabilities:
        if isinstance(item, str):
            name, level, metadata = item, 1, {}
        else:
            item_data = item.model_dump(mode="json")
            name = item_data["name"]
            level = item_data.get("level", 1)
            metadata = item_data.get("metadata", {})
        declared_capabilities.add(name)
        db.add(
            AgentCapability(
                agent_did=body.did,
                name=name,
                level=level,
                metadata_json=metadata,
            )
        )

    # Operator registry (Grok 1.1): only in development OPEN_OPERATORS mode
    # does a declared validation capability self-grant the registry role, so
    # local dev suites keep working without an explicit operator grant. A
    # savepoint contains a concurrent self-grant race; an operator-revoked
    # grant is never resurrected here.
    try:
        with db.begin_nested():
            operator_registry.ensure_self_grant(db, body.did, declared_capabilities)
    except IntegrityError:
        pass

    if is_new and settings.enable_mock_faucet:
        from ..services import ensure_account

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


@router.get("/api/v1/agents/search")
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
        if response_bytes + size > kernel.MAX_LIST_BYTES:
            break
        result.append(item)
        response_bytes += size
        if len(result) >= limit:
            break
    return {"agents": result}


@router.get("/api/v1/agents/{did}")
def get_agent(did: str, db: Session = Depends(get_db)):
    agent = db.get(Agent, did)
    if not agent:
        raise http_error(404, "agent not found")
    return agent_view(db, agent)


@router.get("/api/v1/agents/{did}/balance")
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


@router.get("/api/v1/capabilities")
def list_capabilities(db: Session = Depends(get_db)):
    rows = db.execute(select(AgentCapability.name, func.count()).group_by(
        AgentCapability.name
    ).order_by(AgentCapability.name).limit(100)).all()
    return {"capabilities": [{"name": name, "agent_count": count} for name, count in rows]}
