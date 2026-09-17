"""Retryable outbox helpers for coordination adapters.

Delivery is at-least-once. Each attempt publishes the *same* signed canonical
envelope for a given event, because the envelope is derived deterministically
from the stored row and the current publisher key. Receivers must therefore
deduplicate by ``event_id``; the envelope signature lets them prove that two
copies of an event are the same published record rather than two transitions.

A disabled or unconfigured transport is a no-op: events stay ``PENDING`` with
their attempt count untouched instead of being dead-lettered for a transport the
operator never enabled. Attempts are only consumed once a publish is really
attempted, which keeps ``last_error`` and the retry budget meaningful.
"""

from __future__ import annotations

import uuid
import threading
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from .adapters.technocore import TechnocoreAdapter
from .event_envelope import EnvelopeError, build_envelope
from .models import OutboxEvent
from .publisher import get_event_publisher
from .services import now

OUTBOX_LEASE_SECONDS = 60
OUTBOX_MAX_ATTEMPTS = 10
OUTBOX_BACKOFF_CAP_SECONDS = 3600
MAX_ERROR_LENGTH = 300


def _claimable_condition():
    return or_(
        and_(OutboxEvent.status == "PENDING", OutboxEvent.next_attempt_at <= now()),
        and_(OutboxEvent.status == "PROCESSING", OutboxEvent.lease_expires_at <= now()),
    )


def _truncate_error(error: str | None) -> str | None:
    if error is None:
        return None
    return error[:MAX_ERROR_LENGTH]


def drain_once(
    db: Session, adapter: TechnocoreAdapter, limit: int = 50,
    *, stop_event: threading.Event | None = None,
) -> int:
    """Claim events atomically, then publish their signed envelopes."""
    if not getattr(adapter, "enabled", True):
        return 0

    # Configuration errors are not event delivery failures. Validate before any
    # lease/attempt write, including when the queue is empty.
    publisher = get_event_publisher()

    candidates = db.scalars(
        select(OutboxEvent)
        .where(_claimable_condition())
        .order_by(OutboxEvent.created_at.asc())
        .limit(limit)
    ).all()
    delivered = 0

    for candidate in candidates:
        if stop_event is not None and stop_event.is_set():
            break
        owner = uuid.uuid4().hex
        claimed = db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == candidate.id, _claimable_condition())
            .values(
                status="PROCESSING",
                lease_owner=owner,
                lease_expires_at=now() + OUTBOX_LEASE_SECONDS,
                attempts=OutboxEvent.attempts + 1,
                last_attempt_at=now(),
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            continue

        event = db.get(OutboxEvent, candidate.id, populate_existing=True)
        if not event or event.lease_owner != owner:
            continue

        published = False
        error: str | None = None
        try:
            envelope = build_envelope(event, publisher=publisher)
            published = bool(adapter.publish_event(envelope))
            if not published:
                error = "transport did not accept the envelope"
        except EnvelopeError as exc:
            error = f"envelope rejected: {exc}"
        except Exception as exc:  # noqa: BLE001 - failure is recorded, not raised
            error = f"delivery failed: {type(exc).__name__}"

        if published:
            finalized = db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event.id, OutboxEvent.lease_owner == owner)
                .values(
                    status="DELIVERED",
                    delivered_at=now(),
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=None,
                )
            )
            delivered += int(finalized.rowcount == 1)
        elif event.attempts >= OUTBOX_MAX_ATTEMPTS:
            db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event.id, OutboxEvent.lease_owner == owner)
                .values(
                    status="DEAD",
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=_truncate_error(error),
                )
            )
        else:
            db.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event.id, OutboxEvent.lease_owner == owner)
                .values(
                    status="PENDING",
                    next_attempt_at=now()
                    + min(OUTBOX_BACKOFF_CAP_SECONDS, 2 ** min(event.attempts, 10)),
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=_truncate_error(error),
                )
            )
        db.commit()
    return delivered


def outbox_metrics(db: Session) -> dict[str, Any]:
    """Return delivery observability counters for logs, dashboards, or a probe.

    Counts only. No event payload, envelope, or signature is exposed here, so it
    is safe to log on every worker tick.
    """

    def count(*conditions: Any) -> int:
        return int(
            db.scalar(select(func.count()).select_from(OutboxEvent).where(*conditions)) or 0
        )

    oldest_pending = db.scalar(
        select(func.min(OutboxEvent.created_at)).where(OutboxEvent.status == "PENDING")
    )
    last_success = db.scalar(select(func.max(OutboxEvent.delivered_at)))
    last_failure = db.scalar(
        select(func.max(OutboxEvent.last_attempt_at)).where(OutboxEvent.last_error.is_not(None))
    )
    return {
        "pending_count": count(OutboxEvent.status == "PENDING"),
        "processing_count": count(OutboxEvent.status == "PROCESSING"),
        "delivered_count": count(OutboxEvent.status == "DELIVERED"),
        "dead_count": count(OutboxEvent.status == "DEAD"),
        "oldest_pending_age_seconds": (now() - float(oldest_pending)) if oldest_pending else None,
        "last_success_at": float(last_success) if last_success else None,
        "last_failure_at": float(last_failure) if last_failure else None,
    }
