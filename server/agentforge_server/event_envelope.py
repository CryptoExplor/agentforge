"""Versioned canonical event envelopes for the signed gossip outbox.

An outbox event records an authoritative AgentForge state transition. When that
record is published outside this instance, two independent attributions must
survive:

- **Actor attribution**: the DID that requested the operation, plus the verified
  request signature, request identifier, and body hash that caused the
  transition. This states "this agent requested this operation".
- **Server attribution**: the Ed25519 signature of this AgentForge instance over
  a canonical envelope. This states "this AgentForge instance emitted this
  recorded event".

An agent signature alone cannot prove that the resulting marketplace event
happened: the request may have been rejected, or produced different state. The
server signature is what authenticates the published record. The publisher key is
a *publisher* identity only. It is not an identity root: agent DIDs remain the
only proof of key control, and reputation remains derived from signed, validated
history.

Only non-sensitive identifiers leave the instance. The raw event payload stays in
the local database; the envelope carries its SHA-256 hash plus a small
allow-listed set of scalar attributes. Private task payloads, evidence bodies,
secrets, API keys, wallet keys, and TCLK material must never appear in an
envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .crypto import b64url_decode, canonical_json, sha256_json

ENVELOPE_VERSION = "agentforge-event/1"
HASH_PREFIX = "sha256:"
MAX_ATTRIBUTE_STRING = 200

#: Attributes copied from an event payload. Everything else stays local. Each
#: entry is a non-sensitive identifier or a commitment hash; no field here can
#: carry private input, evidence text, or credentials.
ATTRIBUTE_ALLOWLIST = frozenset(
    {
        "task_id",
        "submission_id",
        "claim_id",
        "dispute_id",
        "decision",
        "decision_id",
        "evidence_hash",
        "task_hash",
        "proof_hash",
        "kind",
        "visibility",
        "status",
        "executor_did",
        "poster_did",
        "validator_did",
        "asset",
        "amount",
        "reason",
    }
)

#: Outbox event kinds mapped to the aggregate they describe. Unknown kinds are
#: published honestly as ``unknown`` rather than guessed.
AGGREGATE_TYPES = {
    "TASK_CREATED": "task",
    "TASK_CLAIMED": "task",
    "TASK_CANCELLED": "task",
    "TASK_EXPIRED": "task",
    "CLAIM_EXPIRED": "task",
    "PROOF_SUBMITTED": "submission",
    "VALIDATION_RECORDED": "submission",
    "DISPUTE_OPENED": "submission",
}

REQUIRED_FIELDS = (
    "version",
    "event_id",
    "event_type",
    "occurred_at",
    "aggregate",
    "actor",
    "payload_hash",
    "attributes",
    "causation",
    "server",
)

CAUSATION_FIELDS = ("request_id", "request_signature", "request_body_hash")


class EnvelopeError(ValueError):
    """Raised when an envelope is structurally invalid and must not be published."""


class EnvelopeSigner(Protocol):
    """The publisher identity used to sign an envelope."""

    publisher_id: str
    key_id: str

    def sign(self, message: bytes) -> str: ...


def hash_payload(payload: Any) -> str:
    """Return the canonical ``sha256:``-prefixed hash of an event payload."""
    return HASH_PREFIX + sha256_json(payload if payload is not None else {})


def occurred_at_from_epoch(created_at: float) -> str:
    """Return a deterministic ISO-8601 UTC timestamp for a stored epoch value."""
    return (
        datetime.fromtimestamp(float(created_at), tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def aggregate_for(kind: str, aggregate_id: str) -> dict[str, str]:
    return {"type": AGGREGATE_TYPES.get(kind, "unknown"), "id": str(aggregate_id)}


def public_attributes(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return the minimal identifying attributes that may leave the instance.

    Only allow-listed scalar keys are copied. Nested objects and long strings are
    dropped, so a proof bundle, private task input, or credential cannot travel
    inside an envelope by accident.
    """
    attributes: dict[str, Any] = {}
    for key in sorted(payload or {}):
        if key not in ATTRIBUTE_ALLOWLIST:
            continue
        value = (payload or {})[key]
        if isinstance(value, bool) or isinstance(value, (int, float)):
            attributes[key] = value
        elif isinstance(value, str) and 0 < len(value) <= MAX_ATTRIBUTE_STRING:
            attributes[key] = value
    return attributes


def signing_bytes(envelope: dict[str, Any]) -> bytes:
    """Return the canonical bytes covered by the server signature.

    Everything except ``server.signature`` is signed, including the publisher id
    and key id, so a signature cannot be replayed under a different publisher or
    key.
    """
    core = {key: value for key, value in envelope.items() if key != "server"}
    server = envelope.get("server") or {}
    core["server"] = {
        "publisher_id": server.get("publisher_id"),
        "key_id": server.get("key_id"),
    }
    return canonical_json(core).encode("utf-8")


def validate_envelope(envelope: dict[str, Any]) -> None:
    """Fail closed on structurally invalid envelopes.

    This is a structural check only; it never verifies or replaces the
    signature, and it is deliberately usable by an external verifier before the
    signature check.
    """
    if not isinstance(envelope, dict):
        raise EnvelopeError("envelope must be an object")
    missing = [field for field in REQUIRED_FIELDS if field not in envelope]
    if missing:
        raise EnvelopeError(f"envelope is missing required fields: {', '.join(missing)}")
    if envelope.get("version") != ENVELOPE_VERSION:
        raise EnvelopeError(f"unsupported envelope version: {envelope.get('version')!r}")
    if not str(envelope.get("event_id") or ""):
        raise EnvelopeError("envelope requires an event_id")
    if not str(envelope.get("event_type") or ""):
        raise EnvelopeError("envelope requires an event_type")
    if not str(envelope.get("occurred_at") or ""):
        raise EnvelopeError("envelope requires occurred_at")
    aggregate = envelope.get("aggregate")
    if not isinstance(aggregate, dict) or not aggregate.get("type") or not aggregate.get("id"):
        raise EnvelopeError("envelope aggregate requires a type and id")
    actor = envelope.get("actor")
    if actor is not None and not (
        isinstance(actor, dict) and str(actor.get("did", "")).startswith("did:key:")
    ):
        raise EnvelopeError("envelope actor must be null or a did:key identifier")
    if not str(envelope.get("payload_hash") or ""):
        raise EnvelopeError("envelope requires a payload_hash")
    attributes = envelope.get("attributes")
    if not isinstance(attributes, dict):
        raise EnvelopeError("envelope requires an attributes object")
    causation = envelope.get("causation")
    if causation is not None:
        if not isinstance(causation, dict):
            raise EnvelopeError("envelope causation must be null or an object")
        for field in CAUSATION_FIELDS:
            if not causation.get(field):
                raise EnvelopeError(f"envelope causation requires {field}")
    server = envelope.get("server")
    if not isinstance(server, dict):
        raise EnvelopeError("envelope requires a server attribution object")
    for field in ("publisher_id", "key_id", "signature"):
        if not server.get(field):
            raise EnvelopeError(f"envelope server attribution requires {field}")
    if "payload" in envelope:
        raise EnvelopeError("raw event payloads must not be published in an envelope")


def build_envelope(event: Any, *, publisher: EnvelopeSigner) -> dict[str, Any]:
    """Build and sign the canonical envelope for an outbox event."""
    if not getattr(event, "id", None) or not getattr(event, "kind", None):
        raise EnvelopeError("outbox event requires an id and kind")
    envelope: dict[str, Any] = {
        "version": ENVELOPE_VERSION,
        "event_id": event.id,
        "event_type": event.kind,
        "occurred_at": occurred_at_from_epoch(event.created_at),
        "aggregate": aggregate_for(event.kind, event.aggregate_id),
        "actor": {"did": event.actor_did} if getattr(event, "actor_did", None) else None,
        "payload_hash": hash_payload(getattr(event, "payload", None)),
        "attributes": public_attributes(getattr(event, "payload", None)),
        "causation": dict(event.causation) if getattr(event, "causation", None) else None,
        "server": {
            "publisher_id": publisher.publisher_id,
            "key_id": publisher.key_id,
            "signature": "",
        },
    }
    envelope["server"]["signature"] = publisher.sign(signing_bytes(envelope))
    validate_envelope(envelope)
    return envelope


def verify_envelope(envelope: dict[str, Any], public_key: Ed25519PublicKey | bytes) -> bool:
    """Verify a signed envelope against a publisher public key.

    External verifiers can copy this function: validate the structure, rebuild
    the canonical signing bytes, and verify the Ed25519 signature. A payload hash
    match additionally proves the envelope describes the same payload the
    publisher recorded.
    """
    try:
        validate_envelope(envelope)
        if isinstance(public_key, (bytes, bytearray)):
            public_key = Ed25519PublicKey.from_public_bytes(bytes(public_key))
        public_key.verify(b64url_decode(envelope["server"]["signature"]), signing_bytes(envelope))
        return True
    except Exception:
        return False
