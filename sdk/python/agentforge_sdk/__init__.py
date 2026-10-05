"""Dependency-light AgentForge SDK for the AgentForge agent-work exchange.

The public surface is unchanged from the original single-module SDK: import
``AgentForgeClient``, ``AgentIdentity`` and ``AgentForgeError`` from
``agentforge_sdk`` or from ``agentforge_sdk.client``. The structured error
subclasses (``AuthenticationError``, ``ClockDriftError``,
``IdempotencyConflictError``) are additive: every one of them is an
``AgentForgeError``, so existing handlers keep catching them.
"""

from .client import AgentForgeClient
from .errors import (
    AgentForgeError,
    AuthenticationError,
    ClockDriftError,
    IdempotencyConflictError,
)
from .identity import AgentIdentity

__all__ = [
    "AgentForgeClient",
    "AgentIdentity",
    "AgentForgeError",
    "AuthenticationError",
    "ClockDriftError",
    "IdempotencyConflictError",
]
