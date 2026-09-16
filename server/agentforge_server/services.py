"""Small deterministic services used by the HTTP layer."""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .crypto import canonical_json, sha256_json
from .models import (
    Agent,
    AgentCapability,
    AuditEvent,
    Claim,
    Escrow,
    IdempotencyRecord,
    LedgerAccount,
    LedgerEvent,
    OutboxEvent,
    ReputationEvent,
    Submission,
    Task,
    ValidationDecision,
)


ZERO = Decimal("0")


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def begin_idempotency(
    db: Session,
    *,
    did: str,
    key: str,
    method: str,
    path: str,
    request_hash: str,
) -> tuple[IdempotencyRecord, dict[str, Any] | None]:
    """Reserve an idempotency key or return a previously completed response."""
    if len(key) > 160:
        raise ValueError("Idempotency-Key is too long")
    existing = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.did == did,
            IdempotencyRecord.key == key,
        )
    )
    if existing:
        if (
            existing.method != method.upper()
            or existing.path != path
            or existing.request_hash != request_hash
        ):
            raise ValueError("Idempotency-Key was reused with a different request")
        if existing.status == "COMPLETED":
            return existing, existing.response_body or {}
        raise ValueError("an identical request is already in progress")

    record = IdempotencyRecord(
        id=new_id("IDEM"),
        did=did,
        key=key,
        method=method.upper(),
        path=path,
        request_hash=request_hash,
        status="IN_PROGRESS",
        created_at=now(),
    )
    db.add(record)
    db.flush()
    return record, None


def complete_idempotency(
    record: IdempotencyRecord,
    *,
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    record.status = "COMPLETED"
    record.response_status = response_status
    record.response_body = response_body
    record.completed_at = now()


def mark_task_deadline_expired(db: Session, task: Task, claim: Claim | None = None) -> None:
    """Close a task after its deadline; deadline expiry never reopens it."""
    timestamp = now()
    task.status = "EXPIRED"
    task.updated_at = timestamp
    if claim is not None:
        claim.status = "EXPIRED"
        claim.updated_at = timestamp
    add_audit(
        db,
        actor_did=None,
        kind="TASK_EXPIRED",
        aggregate_type="task",
        aggregate_id=task.id,
        payload={"claim_id": claim.id if claim else None, "reason": "deadline"},
    )
    queue_outbox(
        db,
        kind="TASK_EXPIRED",
        aggregate_id=task.id,
        payload={"task_id": task.id, "reason": "deadline"},
    )
    db.commit()


def reap_expired_claims(db: Session) -> int:
    """Reopen claims whose lease expired; safe to call on every worker tick."""
    expired = db.scalars(
        select(Claim).where(
            Claim.status == "ACTIVE",
            Claim.lease_expires_at <= now(),
        )
    ).all()
    count = 0
    for claim in expired:
        task = db.get(Task, claim.task_id)
        claim.status = "EXPIRED"
        claim.updated_at = now()
        if task and task.claim_id == claim.id and task.status in {"CLAIMED", "EXECUTING"}:
            expired_at_deadline = task.deadline is not None and task.deadline <= now()
            task.status = "EXPIRED" if expired_at_deadline else "OPEN"
            task.claim_id = None
            task.state_version += 1
            task.updated_at = now()
            add_reputation(
                db,
                did=claim.executor_did,
                role="executor",
                kind="claim_timeout",
                delta=-0.1,
                reference_type="claim",
                reference_id=claim.id,
            )
            expiration_kind = "TASK_EXPIRED" if expired_at_deadline else "CLAIM_EXPIRED"
            add_audit(
                db,
                actor_did=None,
                kind=expiration_kind,
                aggregate_type="task",
                aggregate_id=task.id,
                payload={
                    "claim_id": claim.id,
                    "executor_did": claim.executor_did,
                    "reason": "deadline" if expired_at_deadline else "lease",
                },
            )
            queue_outbox(
                db,
                kind=expiration_kind,
                aggregate_id=task.id,
                payload={"task_id": task.id, "claim_id": claim.id},
            )
            count += 1
    if count:
        db.commit()
    return count


def dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal value") from exc
    if not result.is_finite() or result < ZERO:
        raise ValueError("amount must be finite and non-negative")
    return result


def money_string(value: Decimal) -> str:
    return format(value, "f")


def capability_names(db: Session, did: str) -> set[str]:
    rows = db.scalars(
        select(AgentCapability).where(AgentCapability.agent_did == did)
    ).all()
    return {row.name for row in rows}


def required_capability_names(task: Task) -> set[str]:
    names: set[str] = set()
    for item in task.required_capabilities or []:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
    return names


def can_execute(db: Session, task: Task, executor_did: str) -> bool:
    if task.poster_did == executor_did:
        return False
    required = required_capability_names(task)
    if not required:
        return True
    return required.issubset(capability_names(db, executor_did))


def _agent(db: Session, did: str) -> Agent | None:
    return db.get(Agent, did)


def group_conflicts(db: Session, first_did: str, second_did: str) -> list[str]:
    first = _agent(db, first_did)
    second = _agent(db, second_did)
    if not first or not second:
        return []
    conflicts: list[str] = []
    if first.operator_group and first.operator_group == second.operator_group:
        conflicts.append("operator_group")
    if first.infrastructure_group and first.infrastructure_group == second.infrastructure_group:
        conflicts.append("infrastructure_group")
    return conflicts


def recent_collaboration_counts(
    db: Session,
    first_did: str,
    second_did: str,
    *,
    since: float | None = None,
) -> tuple[int, int]:
    """Count recent requester/executor pairings in each direction."""
    cutoff = since if since is not None else now() - 30 * 24 * 60 * 60
    forward = db.scalar(
        select(func.count(Submission.id))
        .join(Task, Task.id == Submission.task_id)
        .where(
            Task.poster_did == first_did,
            Submission.executor_did == second_did,
            Submission.created_at >= cutoff,
        )
    ) or 0
    reverse = db.scalar(
        select(func.count(Submission.id))
        .join(Task, Task.id == Submission.task_id)
        .where(
            Task.poster_did == second_did,
            Submission.executor_did == first_did,
            Submission.created_at >= cutoff,
        )
    ) or 0
    return int(forward), int(reverse)


def independence_failures(
    db: Session,
    *,
    task: Task,
    participant_did: str,
    role: str,
) -> list[str]:
    """Derive anti-circularity conflicts from server-side relationships."""
    failures: list[str] = []
    if participant_did == task.poster_did:
        failures.append("same_did_as_requester")
    if role == "validator":
        # Validator must be independent from both sides of the task.
        failures.extend(f"requester_{item}" for item in group_conflicts(db, participant_did, task.poster_did))
        claim = db.get(Claim, task.claim_id) if task.claim_id else None
        if claim:
            failures.extend(f"executor_{item}" for item in group_conflicts(db, participant_did, claim.executor_did))
        ancestor_posters = set((task.anti_circularity or {}).get("ancestry_posters", []))
        if participant_did in ancestor_posters:
            failures.append("ancestor_participant")
        # A validator that recently assessed either party is treated as conflicted.
        recent_cutoff = now() - 30 * 24 * 60 * 60
        prior = db.scalar(
            select(func.count(ValidationDecision.id))
            .join(Submission, Submission.id == ValidationDecision.submission_id)
            .join(Task, Task.id == Submission.task_id)
            .where(
                ValidationDecision.validator_did == participant_did,
                ValidationDecision.created_at >= recent_cutoff,
                (Task.poster_did == task.poster_did) | (Submission.executor_did == (claim.executor_did if claim else "")),
            )
        ) or 0
        if prior:
            failures.append("recent_validator_conflict")
    else:
        failures.extend(group_conflicts(db, participant_did, task.poster_did))
        ancestor_posters = set((task.anti_circularity or {}).get("ancestry_posters", []))
        if participant_did in ancestor_posters:
            failures.append("ancestor_participant")
        forward, reverse = recent_collaboration_counts(db, task.poster_did, participant_did)
        if forward >= 3 or reverse >= 3:
            failures.append("recent_collaboration_limit")
        if forward > 0 and reverse > 0:
            failures.append("reciprocal_recent_activity")
    return sorted(set(failures))


def verified_provenance_level(demand_provenance: dict[str, Any]) -> int:
    """Return only trust granted by a registered source adapter."""
    from .provenance import verified_provenance_level as verify

    return verify(demand_provenance)


def derive_activity_eligibility(
    *,
    demand_provenance: dict[str, Any],
    generation_policy: dict[str, Any],
    economics: dict[str, Any],
) -> str:
    if generation_policy.get("synthetic_demo") or demand_provenance.get("type") == "synthetic_demo":
        return "DEMO_ONLY"

    level = int(demand_provenance.get("verified_level", verified_provenance_level(demand_provenance)) or 0)
    minimum = int(generation_policy.get("minimum_provenance_level", 1) or 1)
    reward_amount = dec((economics.get("reward") or {}).get("amount", "0"))

    if level < minimum:
        return "NOT_ELIGIBLE"
    if reward_amount > ZERO:
        return "ECONOMIC_ELIGIBLE"
    return "REPUTATION_ELIGIBLE"


def build_anti_circularity(
    db: Session,
    *,
    creator_did: str,
    parent_task_id: str | None,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Derive a small ancestry record; clients cannot self-certify it."""
    ancestry: list[str] = []
    ancestry_posters: list[str] = []
    current_id = parent_task_id
    seen: set[str] = set()
    same_creator_ancestry = False
    depth = 0
    while current_id and depth < max_depth:
        if current_id in seen:
            raise ValueError("task ancestry contains a cycle")
        seen.add(current_id)
        parent = db.get(Task, current_id)
        if not parent:
            raise ValueError("parent_task_id does not reference an existing task")
        ancestry.append(parent.id)
        if parent.poster_did not in ancestry_posters:
            ancestry_posters.append(parent.poster_did)
        if parent.poster_did == creator_did:
            same_creator_ancestry = True
        current_id = (parent.anti_circularity or {}).get("parent_task_id")
        depth += 1

    return {
        "parent_task_id": parent_task_id,
        "ancestry": ancestry,
        "ancestry_posters": ancestry_posters,
        "graph_depth": depth,
        "max_depth": max_depth,
        "cycle_detected": False,
        "same_creator_ancestry": same_creator_ancestry,
    }


def task_payload(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "version": task.version,
        "kind": task.kind,
        "visibility": task.visibility,
        "origin": task.origin,
        "poster_did": task.poster_did,
        "required_capabilities": task.required_capabilities,
        "chains": task.chains,
        "input": task.input_data,
        "acceptance": task.acceptance,
        "demand_provenance": task.demand_provenance,
        "generation_policy": task.generation_policy,
        "anti_circularity": task.anti_circularity,
        "economics": task.economics,
        "deadline": task.deadline,
        "status": task.status,
        "activity_eligibility": task.activity_eligibility,
        "acceptance_hash": task.acceptance_hash,
        "task_hash": task.task_hash,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def add_audit(
    db: Session,
    *,
    actor_did: str | None,
    kind: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        id=new_id("AUD"),
        actor_did=actor_did,
        kind=kind,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
        created_at=now(),
    )
    db.add(event)
    return event


def queue_outbox(
    db: Session,
    *,
    kind: str,
    aggregate_id: str,
    payload: dict[str, Any],
) -> OutboxEvent:
    event = OutboxEvent(
        id=new_id("OUT"),
        kind=kind,
        aggregate_id=aggregate_id,
        payload=payload,
        status="PENDING",
        attempts=0,
        next_attempt_at=now(),
        created_at=now(),
    )
    db.add(event)
    return event


def ensure_account(
    db: Session,
    did: str,
    asset: str = "MOCK",
    initial_balance: str = "0",
) -> LedgerAccount:
    account = db.scalar(
        select(LedgerAccount).where(
            LedgerAccount.did == did,
            LedgerAccount.asset == asset,
        )
    )
    if account:
        return account
    account = LedgerAccount(did=did, asset=asset, balance=money_string(dec(initial_balance)))
    db.add(account)
    db.flush()
    return account


def post_ledger_event(
    db: Session,
    *,
    did: str,
    asset: str,
    delta: Decimal,
    reason: str,
    idempotency_key: str,
    task_id: str | None = None,
) -> LedgerEvent:
    existing = db.scalar(
        select(LedgerEvent).where(LedgerEvent.idempotency_key == idempotency_key)
    )
    if existing:
        return existing

    account = ensure_account(db, did, asset)
    current = dec(account.balance)
    new_balance = current + delta
    if new_balance < ZERO:
        raise ValueError("insufficient mock balance")

    account.balance = money_string(new_balance)
    event = LedgerEvent(
        id=new_id("LED"),
        idempotency_key=idempotency_key,
        did=did,
        asset=asset,
        amount_delta=money_string(delta),
        reason=reason,
        task_id=task_id,
        created_at=now(),
    )
    db.add(event)
    return event


def fund_task(db: Session, task: Task) -> Escrow | None:
    """Delegate to the active settlement provider (mock by default).

    Preserved as a thin wrapper so existing imports from services continue to
    work and the externally observed mock behavior remains unchanged.
    """
    from .settlement import get_settlement_provider

    return get_settlement_provider().fund(db, task)


def escrow_settle(
    db: Session,
    *,
    task: Task,
    executor_did: str,
    decision: str,
    settlement: dict[str, Any] | None = None,
) -> Escrow | None:
    """Delegate settlement to the active provider.

    The mock provider preserves FULL_RELEASE, PARTIAL_RELEASE, REFUND, SLASH,
    Decimal/string accounting, ledger idempotency keys, audit events,
    mock_burn, terminal exclusivity, and conservation.
    """
    from .settlement import get_settlement_provider

    return get_settlement_provider().settle(
        db, task=task, executor_did=executor_did, decision=decision, settlement=settlement
    )


def add_reputation(
    db: Session,
    *,
    did: str,
    role: str,
    kind: str,
    delta: float,
    capability: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
) -> ReputationEvent:
    event = ReputationEvent(
        id=new_id("REP"),
        did=did,
        role=role,
        capability=capability,
        kind=kind,
        delta=delta,
        reference_type=reference_type,
        reference_id=reference_id,
        created_at=now(),
    )
    db.add(event)
    return event


def reputation_for(db: Session, did: str) -> dict[str, Any]:
    rows = db.scalars(select(ReputationEvent).where(ReputationEvent.did == did)).all()
    overall = sum(row.delta for row in rows)
    by_role: dict[str, float] = {}
    by_capability: dict[str, float] = {}
    for row in rows:
        by_role[row.role] = round(by_role.get(row.role, 0.0) + row.delta, 4)
        if row.capability:
            by_capability[row.capability] = round(
                by_capability.get(row.capability, 0.0) + row.delta,
                4,
            )
    return {
        "did": did,
        "overall": round(overall, 4),
        "by_role": by_role,
        "by_capability": by_capability,
        "event_count": len(rows),
    }


def serializable_model(model: Any) -> dict[str, Any]:
    """Best-effort JSON representation for outbox payloads."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json", by_alias=True)
    return json.loads(json.dumps(model))
