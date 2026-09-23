"""Technocore coordination adapter.

Publication is opt-in and deliberately contract-free: AgentForge does not invent
a remote endpoint, payload contract, or response semantics. The adapter posts the
signed canonical event envelope to an operator-supplied path on the configured
base URL, and reports itself as disabled unless **both**
``AGENTFORGE_GOSSIP_ENABLED`` is true and ``AGENTFORGE_TECHNOCORE_PUBLISH_PATH``
is configured.

AgentForge state is authoritative; publication is outbox-driven and best-effort.
No agent private key is accepted here, no envelope carries private payloads, and
no response body is treated as settlement or reputation evidence.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..crypto import canonical_json
from ..event_envelope import ENVELOPE_VERSION


class TechnocoreAdapter:
    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        *,
        gossip_enabled: bool = False,
        publish_path: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.gossip_enabled = bool(gossip_enabled)
        self.publish_path = (publish_path or "").strip()
        self._client = client

    @classmethod
    def from_settings(cls) -> "TechnocoreAdapter":
        from ..settings import settings

        return cls(
            settings.technocore_base_url,
            gossip_enabled=settings.gossip_enabled,
            publish_path=settings.technocore_publish_path,
        )

    @property
    def configured(self) -> bool:
        """True when the operator explicitly opted into a publish location."""
        return self.gossip_enabled and bool(self.publish_path)

    @property
    def enabled(self) -> bool:
        """True when the adapter can actually publish.

        ``drain_once`` treats ``enabled is False`` as a no-op so that a disabled
        transport never consumes retry attempts or dead-letters events.
        """
        return self.configured

    def status(self) -> dict[str, Any]:
        """Operational status. Contains no secrets and no event payloads."""
        return {
            "adapter": "technocore",
            # URLs can contain credentials in userinfo, query, fragment or path.
            # Worker startup logs this object: never echo the configured value.
            "base_url": "<configured>" if self.base_url else "",
            "enabled": self.enabled,
            "configured": self.enabled,
            "gossip_enabled": self.gossip_enabled,
            "publish_path_configured": bool(self.publish_path),
            "envelope_version": ENVELOPE_VERSION,
        }

    def health(self) -> dict[str, Any]:
        return self.status()

    def publish_event(self, envelope: dict[str, Any]) -> bool:
        """Publish one signed envelope; return True only on a 2xx response."""
        if not self.enabled:
            return False
        server = envelope.get("server") or {}
        headers = {
            "Content-Type": "application/json",
            # Identity headers only; the signature travels inside the envelope.
            "X-AgentForge-Event": str(envelope.get("event_id", "")),
            "X-AgentForge-Publisher": str(server.get("publisher_id", "")),
            "X-AgentForge-Key-Id": str(server.get("key_id", "")),
            "X-AgentForge-Envelope-Version": str(envelope.get("version", "")),
        }
        body = canonical_json(envelope).encode("utf-8")
        url = f"{self.base_url}{self.publish_path}"
        client = self._client
        close_client = False
        if client is None:
            client = httpx.Client()
            close_client = True
        try:
            response = client.post(url, content=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError:
            # Transport failures are retried by the outbox; the body is never
            # logged because it may describe private work.
            return False
        finally:
            if close_client:
                client.close()
        return response.is_success
