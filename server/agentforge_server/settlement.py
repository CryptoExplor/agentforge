"""Settlement provider boundary.

This module defines the smallest possible SettlementProvider interface and
holds the default provider singleton. The concrete mock implementation lives
in ``adapters/mock_settlement.py`` and preserves the existing mock escrow
semantics exactly.

No external provider calls, TCLK integration, FLOP contracts, or new receipt
formats are introduced here. The interface is intentionally minimal:

- fund: reserve task economics into escrow (or no-op if zero)
- settle: apply one of FULL_RELEASE, PARTIAL_RELEASE, REFUND, SLASH

All Decimal/string accounting, ledger idempotency keys, append-only audit
events, mock_burn slash behavior, private balance authorization, terminal
escrow exclusivity, and conservation invariants are preserved inside the
mock provider.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from .models import Escrow, Task


class SettlementProvider(Protocol):
    """Minimal escrow provider interface for the MVP."""

    def fund(self, db: Session, task: Task) -> Escrow | None:
        """Reserve task economics; return escrow or None if zero."""

    def settle(
        self,
        db: Session,
        *,
        task: Task,
        executor_did: str,
        decision: str,
        settlement: dict[str, Any] | None = None,
    ) -> Escrow | None:
        """Apply terminal escrow transition for a validation decision."""


_provider: SettlementProvider | None = None


def get_settlement_provider() -> SettlementProvider:
    """Return the active settlement provider (mock by default).

    Provider selection is server-derived from settings, not client requests.
    Only explicit 'mock' and its legacy 'local' alias are supported. Validate on
    every resolution, BEFORE consulting the singleton or an injected override:
    cached mock state must not hide invalid/unsupported current configuration.
    Settings remain process configuration, not an atomic hot-reload interface.
    """
    global _provider
    from .settings import settings

    configured = getattr(settings, "settlement_provider", None)
    if not isinstance(configured, str) or configured.lower() not in {"mock", "local"}:
        # Do not echo arbitrary configuration values into errors or logs.
        raise RuntimeError("settlement provider is not enabled in MVP; use 'mock' or 'local'")
    if _provider is None:
        from .adapters.mock_settlement import MockSettlementProvider

        _provider = MockSettlementProvider()
    return _provider


def get_deployment_mode() -> str:
    """Return server-derived deployment mode (local, testnet, mainnet, etc)."""
    from .settings import settings

    return getattr(settings, "deployment_mode", "local")


def set_settlement_provider(provider: SettlementProvider) -> None:
    """Inject a trusted local provider for tests; configured-name checks still apply.

    Enabling a future provider requires an explicit supported-name policy change,
    not just seeding this cache. This is not an external client extension point.
    """
    global _provider
    _provider = provider


def reset_settlement_provider() -> None:
    """Reset to default lazy-initialized mock provider."""
    global _provider
    _provider = None
