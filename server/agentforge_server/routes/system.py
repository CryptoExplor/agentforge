"""Operational surface: the console entry point, readiness/health with clock
diagnostics, and the per-agent signed event outbox feed.

None of these are part of the ``/api/v1`` task exchange, but they are the
operator's window into the server: ``/health`` reports the authoritative time
source and how far the database clock is from the API host clock, and
``/api/v1/events`` lets an agent replay its own causally attributed events.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .. import clock
from ..db import get_db
from ..models import AuditEvent
from ..settings import settings
from ._shared import authenticate, http_error

router = APIRouter()

# ``parents[3]`` reaches the repository root from this subpackage
# (routes -> agentforge_server -> server -> repo root). It was ``parents[2]``
# while this handler lived in ``app.py`` one directory up.
_REPO_ROOT = Path(__file__).resolve().parents[3]


@router.get("/", include_in_schema=False)
def root():
    web_path = _REPO_ROOT / "web" / "index.html"
    if web_path.exists():
        return FileResponse(web_path)
    return {
        "name": settings.server_name,
        "version": "0.1.0",
        "protocol": "/api/v1",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


@router.get("/health")
def health(db: Session = Depends(get_db)):
    # Clock diagnostics are part of readiness: an operator must be able to see
    # the authoritative time source and how far the database clock is from the
    # API host clock without needing a signed request.
    return {
        "status": "ok",
        "service": "agentforge",
        "version": "0.1.0",
        "clock": clock.clock_status(db),
    }


@router.get("/api/v1/events")
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
