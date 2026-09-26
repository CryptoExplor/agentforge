"""Signed HTTP transport for the AgentForge SDK.

The transport owns everything wire-level and nothing else: the
``httpx.Client``, protocol v1 canonical request signing, clock-drift
calibration against the server's ``X-Server-Timestamp`` response header,
and mapping HTTP error responses onto structured SDK errors. Marketplace
semantics — resource paths, payloads, proofs and decisions — live in
:mod:`agentforge_sdk.client`, which composes a transport with an
:class:`~agentforge_sdk.identity.AgentIdentity`.

Signing remains exactly the historical format (see
``agentforge_sdk.crypto.request_bytes``)::

    METHOD\\nPATH\\nSHA256(RAW_BODY)\\nEXACT_TIMESTAMP\\nNONCE

The signed path never includes the query string, an empty body signs as
empty bytes (distinct from ``{}``), and each request mints a fresh nonce
that doubles as its ``Idempotency-Key``. No automatic retries: a failed
mutation is surfaced to the caller, who owns the retry decision.
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any

import httpx

from .crypto import canonical_json, request_bytes
from .errors import AgentForgeError, AuthenticationError, ClockDriftError, IdempotencyConflictError
from .identity import AgentIdentity


def error_for(status_code: int, detail: Any) -> AgentForgeError:
    """Map a real HTTP error response onto a structured SDK error.

    Classification follows only the status codes and detail strings the
    server actually returns; an unrecognized failure stays a plain
    ``AgentForgeError`` whose message keeps the exact historical
    ``"HTTP <status>: <detail>"`` format.
    """
    message = f"HTTP {status_code}: {detail}"
    text = str(detail).lower()
    if status_code == 401 and "clock drift" in text:
        return ClockDriftError(message, status_code=status_code, detail=detail)
    if status_code == 401:
        return AuthenticationError(message, status_code=status_code, detail=detail)
    if status_code == 409 and ("idempotency" in text or "identical request is already in progress" in text):
        return IdempotencyConflictError(message, status_code=status_code, detail=detail)
    return AgentForgeError(message, status_code=status_code, detail=detail)


class Transport:
    """HTTP transport with signed requests and server-clock calibration."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        # Injectable so tests can route requests through an in-process ASGI
        # client; production callers leave the default ``httpx.Client``.
        self.http: Any = httpx.Client(timeout=timeout)
        # Seconds to add to the local clock when signing, learned from the
        # server's own ``X-Server-Timestamp`` response header. The server clock
        # is authoritative and its drift window is tight, so a client whose
        # local clock is skewed corrects itself instead of being locked out.
        self.clock_offset: float = 0.0
        # Local clock reading taken when the in-flight request was signed; the
        # header learned afterwards is compared against it, not against "now".
        self._received_at: float = time.time()

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------
    # Unsigned requests (public discovery, registration challenge)
    # ------------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return self.http.get(self.base_url + path, params=params)

    def post(
        self,
        path: str,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.http.post(self.base_url + path, content=content, headers=headers)

    # ------------------------------------------------------------------
    # Response handling: clock calibration and error mapping
    # ------------------------------------------------------------------

    def decode(self, response: httpx.Response) -> Any:
        """Decode a response body, raising a structured error on HTTP >= 400."""
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise error_for(response.status_code, detail)
        return response.json()

    def sync_clock(self, response: httpx.Response) -> None:
        """Learn the server's clock from its response header.

        Only the server's own header is trusted, and it moves a local offset —
        it never changes what the server recorded. A malformed header is
        ignored rather than allowed to corrupt signing.
        """
        header = response.headers.get("X-Server-Timestamp")
        if not header:
            return
        try:
            server_time = float(header)
        except (TypeError, ValueError):
            return
        if not math.isfinite(server_time) or server_time <= 0:
            return
        self.clock_offset = server_time - self._received_at

    # ------------------------------------------------------------------
    # Signed requests
    # ------------------------------------------------------------------

    def signed_request(
        self,
        identity: AgentIdentity,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        signing_path: str | None = None,
    ) -> Any:
        """Send one authenticated request and decode its response.

        ``signing_path`` signs a path stripped of its query string when the
        wire path carries one. The clock is synchronized from the response
        before the body is decoded, so even a rejected request (for example a
        401 clock-drift failure) leaves the transport recalibrated.
        """
        body = b"" if payload is None else canonical_json(payload).encode()
        # Local clock plus the offset learned from the server, so a skewed local
        # clock does not push the signature outside the server's drift window.
        self._received_at = time.time()
        timestamp = str(int(self._received_at + self.clock_offset))
        nonce = uuid.uuid4().hex
        signature = identity.sign(
            request_bytes(method, signing_path or path, body, timestamp, nonce)
        )
        headers = {
            "Content-Type": "application/json",
            "X-Agent-DID": identity.did,
            "X-Agent-Timestamp": timestamp,
            "X-Agent-Nonce": nonce,
            "X-Agent-Signature": signature,
            "Idempotency-Key": nonce,
        }
        response = self.http.request(method, self.base_url + path, content=body, headers=headers)
        self.sync_clock(response)
        return self.decode(response)
