"""Server-side event publisher identity.

The publisher key signs canonical event envelopes for the gossip outbox. It is a
*publisher* identity only:

- it never authenticates agent requests, signs proofs, or acts on behalf of a DID;
- it is not an identity root, and reputation is still derived from signed,
  validated marketplace history rather than from this key;
- losing or rotating it changes only who can assert "this instance emitted this
  event", never who an agent is.

Production deployments must supply ``AGENTFORGE_EVENT_SIGNING_KEY`` from the
deployment secret manager. Development and test runs may generate an ephemeral
in-process key, which is never written to disk and never committed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .crypto import b64url_decode, b64url_encode, sha256_bytes, sign_bytes
from .settings import settings

SEED_LENGTH = 32
DEFAULT_PUBLISHER_ID = "agentforge-reference-server"


@dataclass(frozen=True)
class EventPublisher:
    """A signing identity for published events. Never an agent identity."""

    publisher_id: str
    key_id: str
    source: str
    private_key: Ed25519PrivateKey = field(repr=False)

    @property
    def public_key_bytes(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    @property
    def public_key_b64(self) -> str:
        return b64url_encode(self.public_key_bytes)

    def sign(self, message: bytes) -> str:
        return sign_bytes(self.private_key, message)


def key_id_for(public_key_bytes: bytes) -> str:
    """Return a stable short identifier used for rotation and key versioning."""
    return sha256_bytes(public_key_bytes)[:16]


def _seed_from_secret(secret: str) -> bytes:
    value = secret.strip()
    lowered = value.lower()
    if lowered.startswith("hex:"):
        value = value[4:]
    elif lowered.startswith("base64:") or lowered.startswith("b64:"):
        value = value.split(":", 1)[1]
    if len(value) == SEED_LENGTH * 2:
        try:
            return bytes.fromhex(value)
        except ValueError:
            pass
    try:
        return b64url_decode(value)
    except Exception as exc:  # noqa: BLE001 - normalized into a configuration error
        raise RuntimeError(
            "AGENTFORGE_EVENT_SIGNING_KEY must be a 32-byte Ed25519 seed encoded as hex or base64url"
        ) from exc


def _private_key_from_seed(seed: bytes) -> Ed25519PrivateKey:
    if len(seed) != SEED_LENGTH:
        raise RuntimeError(
            "AGENTFORGE_EVENT_SIGNING_KEY must decode to exactly 32 bytes "
            f"(got {len(seed)}); generate one with "
            "python -c \"import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='))\""
        )
    return Ed25519PrivateKey.from_private_bytes(seed)


def load_event_publisher() -> EventPublisher:
    """Build the configured publisher, or fail closed in production."""
    secret = (settings.event_signing_key or "").strip()
    publisher_id = (settings.event_publisher_id or "").strip() or DEFAULT_PUBLISHER_ID
    if secret:
        private_key = _private_key_from_seed(_seed_from_secret(secret))
        source = "environment"
    elif settings.environment == "production":
        raise RuntimeError(
            "AGENTFORGE_EVENT_SIGNING_KEY is required in production; "
            "ephemeral publisher keys are development-only"
        )
    else:
        private_key = Ed25519PrivateKey.generate()
        source = "ephemeral"
    public_key_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return EventPublisher(
        publisher_id=publisher_id,
        key_id=key_id_for(public_key_bytes),
        source=source,
        private_key=private_key,
    )


_publisher: EventPublisher | None = None


def get_event_publisher() -> EventPublisher:
    """Return the process publisher.

    The value is cached so that an ephemeral development key stays stable for the
    lifetime of the process. A retried event therefore republishes identical
    bytes, which keeps receiver-side deduplication by ``event_id`` meaningful.
    """
    global _publisher
    if _publisher is None:
        _publisher = load_event_publisher()
    return _publisher


def reset_event_publisher() -> None:
    """Drop the cached publisher (tests and key rotation only)."""
    global _publisher
    _publisher = None
