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

Redaction is not audience authorization: even identifiers/hashes can reveal
private relationships. Keep publication disabled unless its audience is approved.
The raw event payload stays in the local database; the envelope carries its SHA-256 hash plus a small
allow-listed set of scalar attributes. Private task payloads, evidence bodies,
secrets, API keys, wallet keys, and TCLK material must never appear in an
envelope.
"""

from __future__ import annotations

import json
import math
from importlib.resources import files
from datetime import datetime, timezone
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from jsonschema import Draft202012Validator, FormatChecker

from .crypto import b64url_decode, canonical_json, sha256_json, verify_signature

LEGACY_ENVELOPE_VERSION = "agentforge-event/1"
ENVELOPE_VERSION = "agentforge-event/2"

# Load the exact public schemas from package resources, including in wheels.
# No copied handwritten validator and no filesystem/repository-root dependency.
_VALIDATORS = {
    version: Draft202012Validator(
        json.loads(files("agentforge_protocol").joinpath(name).read_text(encoding="utf-8")),
        format_checker=FormatChecker(),
    )
    for version, name in (
        (LEGACY_ENVELOPE_VERSION, "event-envelope.schema.json"),
        (ENVELOPE_VERSION, "event-envelope-v2.schema.json"),
    )
}
HASH_PREFIX = "sha256:"
MAX_ATTRIBUTE_STRING = 200

#: Attributes copied from an event payload. Everything else stays local. Each
#: entry is an identifier or commitment hash, NOT an authorization decision.
#: Private identifiers/relationships still require an audience policy.
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
    """Enforce the public versioned schema; errors never echo supplied values."""
    if not isinstance(envelope, dict):
        raise EnvelopeError("envelope must be an object")
    version = envelope.get("version")
    if not isinstance(version, str) or version not in _VALIDATORS:
        raise EnvelopeError("unsupported envelope version")
    if "payload" in envelope:
        raise EnvelopeError("raw event payloads must not be published in an envelope")
    error = next(_VALIDATORS[version].iter_errors(envelope), None)
    if error is not None:
        # JSONSchema messages can contain private input values. Only report the
        # failed rule; never store its message or instance in delivery telemetry.
        raise EnvelopeError(f"envelope schema violation ({error.validator})")
    try:
        canonical_json(envelope)  # Reject NaN/Infinity even in JSON number fields.
    except (TypeError, ValueError) as exc:
        raise EnvelopeError("envelope is not canonical JSON") from exc


def verify_actor_causation(envelope: dict[str, Any]) -> bool:
    """Verify actor attribution solely from v2 envelope data (no private body).

    This does not verify the publisher, prove successful execution, authorize a
    new request, or apply a present-day replay/skew window to historical data.
    Call verify_envelope with a trusted publisher key independently.
    Legacy/absent causation is not independently verifiable and returns False.
    """
    try:
        validate_envelope(envelope)
        if envelope["version"] != ENVELOPE_VERSION or not envelope["actor"] or not envelope["causation"]:
            return False
        cause = envelope["causation"]
        timestamp = cause["request_timestamp"]
        if not math.isfinite(float(timestamp)):
            return False
        message = "\n".join([
            cause["method"], cause["path"], cause["request_body_hash"][len(HASH_PREFIX):],
            timestamp, cause["request_id"],
        ]).encode("utf-8")
        return verify_signature(envelope["actor"]["did"], message, cause["request_signature"])
    except (ValueError, TypeError, KeyError):
        return False


def build_envelope(event: Any, *, publisher: EnvelopeSigner) -> dict[str, Any]:
    """Build and sign the canonical envelope for an outbox event."""
    if not getattr(event, "id", None) or not getattr(event, "kind", None):
        raise EnvelopeError("outbox event requires an id and kind")
    causation = getattr(event, "causation", None)
    # Old rows lack the exact timestamp. Keep their original v1 shape rather
    # than fabricating data or falsely presenting complete v2 attribution.
    version = (LEGACY_ENVELOPE_VERSION if causation and "request_timestamp" not in causation
               else ENVELOPE_VERSION)
    envelope: dict[str, Any] = {
        "version": version,
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
