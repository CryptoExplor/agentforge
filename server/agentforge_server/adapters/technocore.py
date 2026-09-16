"""Technocore coordination adapter.

The adapter is intentionally conservative: AgentForge state is authoritative and
publication is best-effort/outbox-driven. No private key is accepted here.
"""

from __future__ import annotations

from typing import Any

import httpx


class TechnocoreAdapter:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def publish_event(self, event: dict[str, Any]) -> bool:
        """Publish a non-authoritative event if an integration endpoint is configured.

        The exact Technocore write contract is kept behind this adapter. By default
        this method is disabled so running the reference server cannot accidentally
        mutate a remote service.
        """
        return False

    def health(self) -> dict[str, Any]:
        return {"adapter": "technocore", "base_url": self.base_url, "configured": False}
