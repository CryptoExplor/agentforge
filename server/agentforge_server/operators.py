"""Operator registry and role grants (Grok roadmap 1.1).

Self-declared manifest capabilities describe what an agent *can* do; they never
grant authority to act. Peer validation is a privileged, decision-making role,
so an agent that declares ``capabilities=["validator"]`` must additionally hold
an explicit, revocable ``validator`` role grant in the ``operator_role_grants``
registry before it may submit a validation decision.

Two modes:

- ``OPEN_OPERATORS=false`` (production default): only an ``ACTIVE`` registry
  grant attributed to an operator (``granted_by`` set) authorizes validation.
  Unauthorized submissions receive
  ``403 Forbidden ("agent not authorized as validator")``.
- ``OPEN_OPERATORS=true`` (development default): registration self-grants the
  validator role to capability-declaring agents, and the authorization check
  falls back to that same rule at decision time, so existing local dev suites
  that never wrote registry rows keep working. Startup refuses this mode in
  production.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import OperatorRoleGrant
from .services import capability_names, now
from .settings import settings

#: Registry role accepted for peer-validation decisions.
VALIDATOR_ROLE = "validator"
#: Manifest capabilities that express validator intent (never authority).
VALIDATOR_CAPABILITIES = frozenset({"validation", "validator"})


def has_validator_capability(db: Session, did: str) -> bool:
    return bool(VALIDATOR_CAPABILITIES & capability_names(db, did))


def active_grant(db: Session, did: str, role: str = VALIDATOR_ROLE) -> OperatorRoleGrant | None:
    """Return the agent's current ACTIVE grant for a role, if any."""
    return db.scalar(
        select(OperatorRoleGrant).where(
            OperatorRoleGrant.agent_did == did,
            OperatorRoleGrant.role == role,
            OperatorRoleGrant.status == "ACTIVE",
        )
    )


def active_operator_grant(db: Session, did: str, role: str = VALIDATOR_ROLE) -> OperatorRoleGrant | None:
    """Return the agent's current ACTIVE, operator-attributed grant.

    ``granted_by`` NULL marks a development self-grant; such rows never
    authorize anything once the instance runs in restricted mode.
    """
    return db.scalar(
        select(OperatorRoleGrant).where(
            OperatorRoleGrant.agent_did == did,
            OperatorRoleGrant.role == role,
            OperatorRoleGrant.status == "ACTIVE",
            OperatorRoleGrant.granted_by.is_not(None),
        )
    )


def validator_role_active(db: Session, did: str) -> bool:
    """Whether the agent holds an effective validator registry role.

    True for an explicit operator-attributed ACTIVE grant, or — only while the
    development ``OPEN_OPERATORS`` self-registration mode is enabled — for a
    capability-declaring agent. Capability alone is never sufficient in
    restricted mode, and self-granted rows lose effect when the mode flips off.
    """
    if active_operator_grant(db, did):
        return True
    return settings.open_operators and has_validator_capability(db, did)


def validator_authorized(db: Session, did: str) -> bool:
    """Full peer-validation authorization: capability AND an effective role.

    This is the single decision used by every validation submission endpoint
    (``POST /api/v1/tasks/{task_id}/validations``,
    ``POST /api/v1/submissions/{submission_id}/validate`` and
    ``POST /api/v1/disputes/{dispute_id}/resolve``).
    """
    return has_validator_capability(db, did) and validator_role_active(db, did)


def grant_role(
    db: Session,
    *,
    did: str,
    granted_by: str,
    role: str = VALIDATOR_ROLE,
    reason: str | None = None,
) -> OperatorRoleGrant:
    """Record an explicit operator grant.

    ``granted_by`` is required: a grant without an accountable operator DID is
    exactly what the registry exists to prevent. Idempotent; an operator grant
    on top of a development self-grant upgrades that row's attribution, and a
    revoked grant is never resurrected (create a fresh explicit grant instead).
    """
    if not granted_by or not granted_by.strip():
        raise ValueError("granted_by operator DID is required for explicit grants")
    existing = db.scalar(
        select(OperatorRoleGrant).where(
            OperatorRoleGrant.agent_did == did,
            OperatorRoleGrant.role == role,
        )
    )
    if existing is not None:
        if existing.status != "ACTIVE":
            raise ValueError("role grant was revoked; create a new explicit grant")
        if existing.granted_by is None:
            # A development self-grant is converted into an operator-attributed
            # grant: the operator's explicit decision becomes the authority.
            existing.granted_by = granted_by.strip()
            existing.reason = reason
            db.flush()
        return existing
    grant = OperatorRoleGrant(
        agent_did=did,
        role=role,
        status="ACTIVE",
        granted_by=granted_by.strip(),
        reason=reason,
        created_at=now(),
    )
    db.add(grant)
    db.flush()
    return grant


def revoke_role(db: Session, *, did: str, role: str = VALIDATOR_ROLE) -> bool:
    """Revoke an ACTIVE grant; revocation takes effect immediately."""
    grant = active_grant(db, did, role)
    if grant is None:
        return False
    grant.status = "REVOKED"
    grant.revoked_at = now()
    db.flush()
    return True


def ensure_self_grant(db: Session, did: str, capabilities: set[str]) -> OperatorRoleGrant | None:
    """Development self-registration (OPEN_OPERATORS=true).

    On registration, capability-declaring validator agents receive a
    self-granted registry row (``granted_by`` NULL). An existing row is never
    modified: a grant explicitly revoked by an operator stays revoked even if
    the agent re-registers with the same capability, and operator-attributed
    grants are left untouched.
    """
    if not settings.open_operators:
        return None
    if not (VALIDATOR_CAPABILITIES & capabilities):
        return None
    existing = db.scalar(
        select(OperatorRoleGrant).where(
            OperatorRoleGrant.agent_did == did,
            OperatorRoleGrant.role == VALIDATOR_ROLE,
        )
    )
    if existing is not None:
        return None
    grant = OperatorRoleGrant(
        agent_did=did,
        role=VALIDATOR_ROLE,
        status="ACTIVE",
        granted_by=None,
        reason="self_registered_open_mode",
        created_at=now(),
    )
    db.add(grant)
    db.flush()
    return grant
