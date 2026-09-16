"""Conservative provenance verification extension point.

A client may report a source reference, novelty hash, or attestation, but those
fields are claims until a server-registered adapter verifies them. The MVP ships
with no network-backed adapters; deployments may register a narrowly scoped
adapter during application setup.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


SourceVerifier = Callable[[dict[str, Any]], bool]
_SOURCE_ADAPTERS: dict[str, SourceVerifier] = {}


def register_source_adapter(name: str, verifier: SourceVerifier) -> None:
    """Register a trusted, deterministic verifier for a source namespace."""
    normalized = name.strip().lower()
    if not normalized or not callable(verifier):
        raise ValueError("source adapter name and verifier are required")
    _SOURCE_ADAPTERS[normalized] = verifier


def clear_source_adapters() -> None:
    """Reset adapters, primarily for isolated tests and development reloads."""
    _SOURCE_ADAPTERS.clear()


def verify_registered_source(provenance: dict[str, Any]) -> bool:
    """Return true only when a registered adapter accepts the complete claim."""
    adapter_name = provenance.get("source_adapter") or provenance.get("source")
    if not isinstance(adapter_name, str):
        return False
    verifier = _SOURCE_ADAPTERS.get(adapter_name.strip().lower())
    if verifier is None:
        return False
    try:
        return bool(verifier(dict(provenance)))
    except Exception:
        # A source adapter must fail closed; adapter faults cannot grant trust.
        return False


def verified_provenance_level(provenance: dict[str, Any]) -> int:
    """Derive server-trusted provenance level; unverified client claims are level 0."""
    if provenance.get("type") == "synthetic_demo":
        return 0
    required = ("source_ref", "novelty_hash")
    if not all(provenance.get(field) for field in required):
        return 0
    return 1 if verify_registered_source(provenance) else 0
