"""Domain routers for the AgentForge HTTP API.

``DOMAIN_ROUTERS`` is the mount order used by ``app.create_app()``. Starlette
matches routes in registration order, so this tuple is part of the API's
behaviour and not just its layout: the one order-sensitive pair in the API is
``GET /api/v1/agents/search`` having to be registered before
``GET /api/v1/agents/{did}``, and both live in ``agents.router`` in that order.
No two routers expose paths that can match the same request, so the relative
order of the routers themselves is not load-bearing -- but it is kept explicit
here so a future router cannot silently reorder that pair.

Mount order: identity -> demand -> lease -> execution -> review -> dispute ->
operational surface.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    agents,
    claims,
    disputes,
    submissions,
    system,
    tasks,
    validations,
)

#: Domain routers in mount order. See the module docstring.
DOMAIN_ROUTERS: tuple[APIRouter, ...] = (
    agents.router,
    tasks.router,
    claims.router,
    submissions.router,
    validations.router,
    disputes.router,
    system.router,
)

__all__ = ["DOMAIN_ROUTERS"]
