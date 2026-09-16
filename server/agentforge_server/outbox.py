"""Retryable outbox helpers for coordination adapters."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .adapters.technocore import TechnocoreAdapter
from .models import OutboxEvent
from .services import now


OUTBOX_LEASE_SECONDS = 60


def drain_once(db: Session, adapter: TechnocoreAdapter, limit: int = 50) -> int:
    """Claim events atomically before publishing them.

    Adapter delivery remains at-least-once. Adapters must therefore be
    idempotent by event ID; the lease prevents ordinary multi-worker races.
    """
    candidates = db.scalars(
        select(OutboxEvent)
        .where(
            or_(
                and_(OutboxEvent.status == "PENDING", OutboxEvent.next_attempt_at <= now()),
                and_(OutboxEvent.status == "PROCESSING", OutboxEvent.lease_expires_at <= now()),
            )
        )
        .order_by(OutboxEvent.created_at.asc())
        .limit(limit)
    ).all()
    delivered = 0

    for candidate in candidates:
        owner = uuid.uuid4().hex
        claimed = db.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == candidate.id,
                or_(
                    and_(OutboxEvent.status == "PENDING", OutboxEvent.next_attempt_at <= now()),
                    and_(OutboxEvent.status == "PROCESSING", OutboxEvent.lease_expires_at <= now()),
                ),
            )
            .values(
                status="PROCESSING",
                lease_owner=owner,
                lease_expires_at=now() + OUTBOX_LEASE_SECONDS,
                attempts=OutboxEvent.attempts + 1,
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            continue

        event = db.get(OutboxEvent, candidate.id)
        if not event:
            continue
        try:
            ok = adapter.publish_event(
                {"id": event.id, "kind": event.kind, "aggregate_id": event.aggregate_id, "payload": event.payload}
            )
        except Exception:
            ok = False
        if ok:
            db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event.id, OutboxEvent.lease_owner == owner)
                .values(status="DELIVERED", delivered_at=now(), lease_owner=None, lease_expires_at=None)
            )
            delivered += 1
        elif event.attempts >= 10:
            db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event.id, OutboxEvent.lease_owner == owner)
                .values(status="DEAD", lease_owner=None, lease_expires_at=None)
            )
        else:
            db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event.id, OutboxEvent.lease_owner == owner)
                .values(
                    status="PENDING",
                    next_attempt_at=now() + min(3600, 2 ** min(event.attempts, 10)),
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
        db.commit()
    return delivered
