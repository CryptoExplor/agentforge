"""Structured SDK errors.

``AgentForgeError`` keeps its historical shape: a plain ``RuntimeError``
whose message for HTTP failures is exactly ``"HTTP <status>: <detail>"``.
Everything the SDK raises is a subclass of it, so existing
``except AgentForgeError`` handlers keep working unchanged.

The subclasses classify the failure modes the API actually produces today:
401 authentication failures, 401 clock-drift rejections and 409
idempotency-key conflicts. They carry ``status_code``/``detail`` attributes
taken from the real HTTP response; no error codes are invented beyond what
the server returns. The flat hierarchy is deliberate — the classes are
siblings, not a taxonomy, so catching one never silently widens to another.
"""

from __future__ import annotations

from typing import Any


class AgentForgeError(RuntimeError):
    """Base class for every error the SDK raises.

    ``status_code`` and ``detail`` are present only when the error came
    from an HTTP response; errors raised before a request is sent (for
    example a corrupt identity file) leave them ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class AuthenticationError(AgentForgeError):
    """401: the signed request did not authenticate.

    Covers the server's actual 401 details: missing signed headers, an
    unknown or inactive agent, an invalid timestamp, an invalid request /
    registration / validation signature, or a replayed request nonce.
    """


class ClockDriftError(AgentForgeError):
    """401 ``client clock drift exceeds tolerance``.

    The signed timestamp fell outside the server's drift window. The
    transport recalibrates its clock offset from the
    ``X-Server-Timestamp`` header of every response — including this one —
    so retrying the same logical operation normally succeeds immediately.
    """


class IdempotencyConflictError(AgentForgeError):
    """409: an ``Idempotency-Key`` conflict.

    The key was reused with a different request, an identical request is
    already in progress, or two workers raced to reserve the key. These
    responses are never retried blindly: the caller must decide whether
    the conflicting operation is the one it wanted.
    """
