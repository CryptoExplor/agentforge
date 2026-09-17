"""Outbox and claim-reaper worker entry point.

Run the API and the worker as separate processes against the same database::

    python -m agentforge_server.worker          # continuous loop
    python -m agentforge_server.worker --once   # single tick, for cron or tests

The loop stops cleanly on ``SIGINT``/``SIGTERM`` after the current tick, so a
lease is never abandoned mid-publish and no event is delivered twice for one
claim. Each tick reaps expired claims first and then drains the outbox, which
keeps the active-claim invariant enforced independently of API traffic.
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import threading
import time
from typing import Any

from . import db as database
from .adapters.technocore import TechnocoreAdapter
from .db import init_db, verify_schema
from .outbox import drain_once, outbox_metrics
from .services import reap_expired_claims
from .settings import settings

LOGGER = logging.getLogger("agentforge.worker")


def prepare_database() -> None:
    """Mirror the API startup rule instead of creating an implicit schema."""
    if settings.environment == "production":
        verify_schema()
    elif settings.auto_create_schema:
        init_db()
    else:
        verify_schema()


def run_once(adapter: TechnocoreAdapter | None = None) -> dict[str, Any]:
    """Run one reap + drain tick and return counts for logging or assertions."""
    active_adapter = adapter or TechnocoreAdapter.from_settings()
    # Read the module attribute at call time: configure_database() rebinds it.
    with database.SessionLocal() as db:
        reaped = reap_expired_claims(db)
        delivered = drain_once(db, active_adapter)
        metrics = outbox_metrics(db)
    return {"reaped_claims": reaped, "delivered": delivered, **metrics}


def run_worker(
    stop_event: threading.Event | None = None,
    *,
    adapter: TechnocoreAdapter | None = None,
    once: bool = False,
) -> int:
    """Run the worker loop until stopped; return the number of completed ticks."""
    stop_event = stop_event or threading.Event()
    active_adapter = adapter or TechnocoreAdapter.from_settings()
    prepare_database()
    LOGGER.info("worker starting: %s", active_adapter.status())
    ticks = 0
    while not stop_event.is_set():
        summary = run_once(active_adapter)
        ticks += 1
        if summary["delivered"] or summary["reaped_claims"] or summary["dead_count"]:
            LOGGER.info(
                "tick delivered=%s reaped=%s pending=%s processing=%s dead=%s",
                summary["delivered"],
                summary["reaped_claims"],
                summary["pending_count"],
                summary["processing_count"],
                summary["dead_count"],
            )
        if once:
            break
        stop_event.wait(_next_delay())
    LOGGER.info("worker stopped after %s tick(s)", ticks)
    return ticks


def _next_delay() -> float:
    """Interval plus bounded jitter so replicas do not poll in lockstep."""
    interval = max(0.1, float(settings.outbox_interval_seconds))
    jitter = max(0.0, float(settings.outbox_jitter_seconds))
    return interval + (random.uniform(0, jitter) if jitter else 0.0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AgentForge outbox worker")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s; finishing the current tick", signum)
        stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, name, None)
        if handler is not None:
            try:
                signal.signal(handler, handle_signal)
            except ValueError:
                # Not the main thread (embedded/test use); stop_event still works.
                pass
    started = time.monotonic()
    ticks = run_worker(stop_event, once=args.once)
    LOGGER.info("worker exiting: ticks=%s uptime_seconds=%.1f", ticks, time.monotonic() - started)


if __name__ == "__main__":
    main()
