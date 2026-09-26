"""Authoritative server time and clock-drift defence (Grok roadmap 1.4).

AgentForge treats the **server clock as the only authority** for time. A client
timestamp is an unauthenticated claim: it may be skewed, replayed or forged, so
it can never start a lease, extend a lease, or decide whether a submission or a
dispute window is still open.

Two clocks are used, with strictly separated duties:

``server_now()``
    The authoritative application clock. It returns epoch seconds anchored to
    ``time.monotonic()``, so it never moves backwards: a wall-clock step (NTP
    correction, manual ``date -s``, container migration) cannot rewind an
    in-flight lease or re-open an expired one. It is the clock that *writes*
    every lease, claim expiry and ``received_at`` value, and it is also the
    clock those values are compared against, because a lease must be measured
    by the same clock that started it.

``database_now()``
    The database server clock, evaluated in SQL. It is used only for decisions
    about *client-declared absolute* instants (a task ``deadline``), where the
    system of record's own clock is the better reference, and it is
    cross-checked against the API host clock so that neither a skewed API host
    nor a skewed database can silently widen a submission or dispute window.

Forward-only wall-clock resynchronisation
    ``time.monotonic()`` does not advance while a machine is suspended, so a
    purely monotonic clock can fall behind real time and start rejecting every
    honest client as "futuristic". ``server_now()`` therefore snaps **forward**
    when the wall clock leads the anchored value by more than
    ``WALL_RESYNC_THRESHOLD_SECONDS``. It never snaps backwards: a host whose
    clock is set back keeps issuing the later value, which can only shorten a
    lease, never extend it.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .settings import settings

#: Julian day number of the Unix epoch. SQLite's ``CURRENT_TIMESTAMP`` has
#: whole-second precision, so ``julianday('now')`` is the portable sub-second
#: database clock for the SQLite deployments this MVP runs on.
JULIAN_UNIX_EPOCH = 2440587.5
SECONDS_PER_DAY = 86_400.0

#: A monotonic/wall gap larger than this means the host clock stepped or the
#: machine was suspended. Snap forward (never backwards) so honest clients are
#: not rejected as futuristic while a rewound host clock cannot extend a lease.
WALL_RESYNC_THRESHOLD_SECONDS = 5.0

_LOCK = threading.RLock()
_epoch_base = time.time()
_monotonic_base = time.monotonic()
_last_issued = _epoch_base


class ClockUnavailable(RuntimeError):
    """The authoritative server time could not be established.

    Raised instead of falling back to a client-supplied or otherwise untrusted
    value: a deadline decision that cannot be anchored to a server clock must
    fail closed rather than settle.
    """


def monotonic_now() -> float:
    """Raw ``time.monotonic()``; useful for measuring latency, not for storage."""
    return time.monotonic()


def server_now() -> float:
    """Return the authoritative epoch-seconds timestamp (monotonic, non-decreasing).

    Every value the server records for leases, claim expiries, ``received_at``
    and request acceptance comes from here, so all of them are comparable and
    none of them can be influenced by a client clock.
    """
    global _epoch_base, _last_issued
    with _LOCK:
        anchored = _epoch_base + (time.monotonic() - _monotonic_base)
        wall = time.time()
        if wall - anchored > WALL_RESYNC_THRESHOLD_SECONDS:
            # Suspended host or a forward wall-clock step: re-anchor forward.
            _epoch_base += wall - anchored
            anchored = wall
        if anchored < _last_issued:
            # Belt and braces: monotonic clocks are non-decreasing, and the
            # authoritative clock must never hand out an earlier instant.
            anchored = _last_issued
        _last_issued = anchored
        return anchored


def server_uptime_seconds() -> float:
    """Seconds elapsed on the monotonic clock since this process anchored it."""
    return time.monotonic() - _monotonic_base


def stamp_scope(scope: dict[str, Any]) -> float:
    """Record the authoritative receipt time of a request at ingress.

    Called by the ingress middleware before the body is even read, so
    ``received_at`` is the moment the server took responsibility for the
    request. It is exposed to endpoints through ``request.state``.
    """
    received = server_now()
    try:
        state = scope.get("state")
        if not isinstance(state, dict):
            state = {}
            scope["state"] = state
        state["received_at"] = received
        state["received_monotonic"] = monotonic_now()
    except (AttributeError, TypeError):
        # A non-mutable scope (test harnesses) must not fail the request; the
        # endpoint falls back to server_now(), which is the same clock.
        pass
    return received


def request_received_at(request: Any) -> float:
    """Authoritative receipt time for the current request.

    Falls back to ``server_now()`` when the ingress stamp is missing (direct
    endpoint calls, test harnesses). A stamped value is re-validated so a
    corrupted or hostile scope state can never push a lease into the past or
    the future.
    """
    state = getattr(request, "state", None)
    value = getattr(state, "received_at", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return server_now()
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        return server_now()
    return value


def drift_seconds(client_timestamp: float, reference: float | None = None) -> float:
    """Signed client clock drift: positive when the client clock runs ahead."""
    reference = server_now() if reference is None else reference
    return float(client_timestamp) - float(reference)


def within_tolerance(client_timestamp: float, reference: float | None = None) -> bool:
    """True when a client timestamp is inside the configured drift window."""
    tolerance = float(settings.request_clock_skew_seconds)
    return abs(drift_seconds(client_timestamp, reference)) <= tolerance


def database_now_expression(dialect_name: str) -> Any:
    """SQL expression returning the database server clock as epoch seconds."""
    if dialect_name == "postgresql":
        # ``now()`` is the transaction-start server timestamp: slightly earlier
        # than the statement instant, which is the conservative direction for a
        # deadline that must not be extended.
        return func.extract("epoch", func.now())
    if dialect_name == "sqlite":
        return (func.julianday("now") - JULIAN_UNIX_EPOCH) * SECONDS_PER_DAY
    raise ClockUnavailable(f"no server-time expression for dialect {dialect_name!r}")


def database_now(db: Session) -> float:
    """Read the database server clock as epoch seconds."""
    try:
        dialect_name = db.get_bind().dialect.name
        value = db.scalar(select(database_now_expression(dialect_name)))
    except ClockUnavailable:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise ClockUnavailable("database server time is unavailable") from exc
    if value is None:
        raise ClockUnavailable("database returned no server timestamp")
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise ClockUnavailable("database server timestamp is not numeric") from exc
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise ClockUnavailable("database server timestamp is not usable")
    return timestamp


def database_skew_seconds(db: Session) -> float:
    """API host wall clock minus the database server clock, in seconds."""
    return time.time() - database_now(db)


def deadline_reference(db: Session, *, tolerance: float | None = None) -> float:
    """Authoritative "now" for submission-deadline and dispute-window decisions.

    The value comes from the database server clock, and the API host clock is
    required to agree with it inside ``tolerance`` seconds. A divergence means
    one of the two clocks is wrong, so the decision fails closed with
    :class:`ClockUnavailable` instead of guessing which one to believe.
    """
    reference = database_now(db)
    limit = float(settings.db_clock_skew_tolerance_seconds) if tolerance is None else float(tolerance)
    skew = time.time() - reference
    if not math.isfinite(skew) or abs(skew) > limit:
        raise ClockUnavailable(
            "server clock is not synchronized with the database "
            f"(skew {skew:.3f}s exceeds {limit}s tolerance)"
        )
    return reference


def clock_status(db: Session | None = None) -> dict[str, Any]:
    """Diagnostic snapshot for ``/health``; never includes client-supplied time."""
    status: dict[str, Any] = {
        "source": "monotonic-anchored",
        "server_time": server_now(),
        "uptime_seconds": round(server_uptime_seconds(), 6),
        "drift_tolerance_seconds": settings.request_clock_skew_seconds,
    }
    if db is None:
        return status
    try:
        status["database_time"] = database_now(db)
        status["database_skew_seconds"] = round(database_skew_seconds(db), 6)
        status["database_skew_tolerance_seconds"] = settings.db_clock_skew_tolerance_seconds
    except ClockUnavailable as exc:
        status["database_time"] = None
        status["database_error"] = str(exc)
    return status
