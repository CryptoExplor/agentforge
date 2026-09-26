"""Replica-shared, bounded fixed-window admission for the small hosted MVP.

No trust in arbitrary forwarded headers or unverified DID headers. SQL counters
are atomically incremented; denial is committed independently of business work.
This deliberately simple global counter is not a 100k-client scaling solution.
"""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
import math
import time

from sqlalchemy import delete

from . import db as database
from .crypto import public_key_from_did
from .models import RegistrationChallenge, RequestQuota, UsedNonce
from .settings import settings


MAX_CLOCK_SKEW_SECONDS = 3600


class AdmissionDenied(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


def validate_security_configuration() -> None:
    for name in (
        "request_global_per_minute", "request_ip_per_minute",
        "registration_global_per_minute", "registration_ip_per_minute",
        "request_did_per_minute",
    ):
        value = getattr(settings, name)
        if type(value) is not int or not 1 <= value <= 1_000_000:
            raise ValueError("admission limits must be integers between 1 and 1000000")
    if type(settings.max_inflight_requests) is not int or not 1 <= settings.max_inflight_requests <= 256:
        raise ValueError("max in-flight requests must be between 1 and 256")
    if not math.isfinite(settings.body_timeout_seconds) or not 0 < settings.body_timeout_seconds <= 60:
        raise ValueError("body timeout must be finite, positive and at most 60 seconds")
    if not 0 < settings.request_clock_skew_seconds <= MAX_CLOCK_SKEW_SECONDS:
        raise ValueError("clock skew must be positive and at most 3600 seconds")
    # Server-anchored time (Grok roadmap 1.4). The drift window bounds how far a
    # client clock may sit from the server clock; the database tolerance bounds
    # how far the database clock may sit from the API host clock before deadline
    # decisions fail closed. Neither may be zero, negative, unbounded or NaN.
    if not math.isfinite(settings.db_clock_skew_tolerance_seconds) or not 0.1 <= settings.db_clock_skew_tolerance_seconds <= 60:
        raise ValueError("database clock skew tolerance must be between 0.1 and 60 seconds")
    if type(settings.dispute_window_seconds) is not int or not 0 <= settings.dispute_window_seconds <= 30 * 24 * 60 * 60:
        raise ValueError("dispute window must be an integer between 0 and 30 days")
    if len(settings.trusted_validator_dids) > 1000:
        raise ValueError("too many trusted validators")
    for did in settings.trusted_validator_dids:
        try:
            public_key_from_did(did)
        except Exception as exc:
            raise ValueError("invalid trusted validator identity") from exc
    if settings.environment == "production" and settings.enable_mock_faucet:
        raise ValueError("mock faucet is forbidden in production")
    if settings.environment == "production" and settings.open_operators:
        raise ValueError("OPEN_OPERATORS self-registration is forbidden in production; use explicit operator role grants")
    if type(settings.max_service_fee_bps) is not int or not 0 <= settings.max_service_fee_bps <= 10_000:
        raise ValueError("max service fee bps must be an integer between 0 and 10000")


def _bucket(scope: str, identity: str) -> str:
    return scope + ":" + hashlib.sha256(identity.encode()).hexdigest()


def consume(buckets: list[tuple[str, str, int]], *, timestamp: float | None = None, session=None) -> None:
    """Consume quotas in a stable order; earlier charges survive later denial."""
    timestamp = time.time() if timestamp is None else timestamp
    window = int(timestamp // 60)
    denied = False
    table = RequestQuota.__table__
    # A verified request already has a session. Reuse it instead of holding one
    # pooled connection while waiting for a second (pool starvation under load).
    context = nullcontext(session.connection()) if session is not None else database.engine.begin()
    with context as connection:
        if connection.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif connection.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise RuntimeError("admission requires PostgreSQL or SQLite")
        for scope, identity, limit in buckets:
            if type(limit) is not int or not 1 <= limit <= 1_000_000:
                raise ValueError("invalid admission limit")
            statement = insert(table).values(bucket=_bucket(scope, identity), window=window, count=1)
            statement = statement.on_conflict_do_update(
                index_elements=[table.c.bucket, table.c.window],
                set_={"count": table.c.count + 1},
                where=table.c.count < limit,
            ).returning(table.c.count)
            if connection.execute(statement).scalar_one_or_none() is None:
                denied = True
                break
        # No identity-controlled state is allocated after a failed global quota.
        # Keep current/previous windows; cleanup does not delete active buckets.
        connection.execute(delete(table).where(table.c.window < window - 1))
    if session is not None:
        session.commit()  # admission happens before business writes; survives their rollback
    if denied:
        raise AdmissionDenied(max(1, math.ceil((window + 1) * 60 - timestamp)))


def consume_ingress(peer: str, registration: bool) -> None:
    buckets = [
        ("global", "all", settings.request_global_per_minute),
        ("ip", peer, settings.request_ip_per_minute),
    ]
    if registration:
        buckets.extend([
            ("register-global", "all", settings.registration_global_per_minute),
            ("register-ip", peer, settings.registration_ip_per_minute),
        ])
    consume(buckets)


def consume_authenticated(did: str, session) -> None:
    # Called only after cryptographic verification, never from a claimed header.
    consume([("did", did, settings.request_did_per_minute)], session=session)


def prune_security_state(session, *, timestamp: float | None = None) -> None:
    timestamp = time.time() if timestamp is None else timestamp
    session.execute(delete(RequestQuota).where(RequestQuota.window < int(timestamp // 60) - 1))
    session.execute(delete(RegistrationChallenge).where(RegistrationChallenge.expires_at < timestamp - 60))
    # A request accepted with a future timestamp can stay fresh for 2*skew.
    # Use the maximum supported skew, not this worker's setting: API replicas
    # can temporarily have different settings during a rolling deployment.
    session.execute(delete(UsedNonce).where(
        UsedNonce.created_at < timestamp - 2 * MAX_CLOCK_SKEW_SECONDS - 60
    ))
    # Never silently expire business idempotency records: that could re-execute
    # completed operations. Audit/outbox/business retention needs explicit policy.
    session.commit()
