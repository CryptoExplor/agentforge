"""AgentForge pre-testnet MVP HTTP API.

This module is the composition root and nothing else. It owns three things:

1. the tunables and service references the API's guards are read through;
2. ``lifespan``, which decides how the schema is prepared and refuses to start
   in an unsafe configuration;
3. ``create_app()``, which wires middleware, mounts the domain routers and
   returns the application.

Every request handler lives in :mod:`agentforge_server.routes` -- one module
per domain (agents, tasks, claims, submissions, validations, disputes, system)
-- and the cross-domain request plumbing they share is in
:mod:`agentforge_server.routes._shared`.

The imports marked ``noqa: F401`` are not dead code. The regression suites
monkeypatch these names *on this module* to prove the API really consults the
capability check, the atomic claim guard, the pending-submission guard and the
outbox write, and to exercise the response-byte budget. The routers therefore
read them as ``kernel.<name>`` at call time (see ``routes/_shared.py``), so
they must stay importable here even though this module never calls them.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db as database
from .admission import validate_security_configuration
from .db import init_db, verify_schema
from .middleware import RequestSecurityMiddleware
from .routes import DOMAIN_ROUTERS
from .services import (  # noqa: F401  (monkeypatch surface, see module docstring)
    can_execute,
    guard_active_claim,
    guard_pending_submission,
    queue_outbox,
)
from .settings import settings


#: Claim lease length. Executors extend it by heartbeating; the lease always
#: starts at the server's own receipt time for the request that created or
#: extended it, never at a client-declared instant.
CLAIM_LEASE_SECONDS = 15 * 60

#: Upper bound on the serialised size of a list response. Discovery renders an
#: unbounded number of rows, so it stops adding items at this budget and
#: continues the cursor from the last item it did render.
MAX_LIST_BYTES = 4_000_000


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

    # Mount order is defined once, in routes/__init__.py, because Starlette
    # matches in registration order.
    for router in DOMAIN_ROUTERS:
        app.include_router(router)

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
