"""Chain-agnostic, internal settlement commands and independent verifier ports.

Not a SettlementProvider, public API, signer, or enabled rail. Trusted producers
call enqueue_intent in their marketplace transaction; the row IS the work
outbox. No commits or network I/O occur here. Mock settlement never calls this.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import json
import re
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .crypto import canonical_json, sha256_bytes
from .models import SettlementAttempt, SettlementAttemptEvent, SettlementIntent, Task
from .money import ZERO, dec, money_string, money_sum
from .services import new_id, now


class Action(StrEnum):
    HOLD = "hold"
    RELEASE = "release"  # Includes partial release with explicit refund allocation.
    REFUND = "refund"
    SLASH = "slash"


class State(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FINALIZED = "FINALIZED"
    FAILED = "FAILED"
    REPLACED = "REPLACED"
    REORGED = "REORGED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ObservationKind(StrEnum):
    UNKNOWN = "UNKNOWN"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FINALIZED = "FINALIZED"
    FAILED = "FAILED"
    REPLACED = "REPLACED"
    REORGED = "REORGED"


def reference(value: str, *, maximum: int = 200) -> str:
    """Bound opaque identifiers; URLs, raw receipts and credentials do not belong here."""
    if not isinstance(value, str) or "://" in value or not re.fullmatch(r"[A-Za-z0-9_:./-]{1," + str(maximum) + r"}", value):
        raise ValueError("invalid settlement reference")
    return value


def bounded_int(value: int, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 1_000_000_000:
        raise ValueError("invalid settlement counter")
    return value


def _amount(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("settlement amounts must be strings")
    text = money_string(dec(value))
    return (text.rstrip("0").rstrip(".") if "." in text else text) if dec(text) else "0"


@dataclass(frozen=True)
class IntentSpec:
    """Versioned unsigned economic intent. All fields are receipt commitments.

    target identifies the operator-pinned rail/domain/escrow authority, not an
    RPC URL. A future verifier must check that identity against actual evidence.
    Amounts are net: released + fee + refunded + slashed == reserved for every
    terminal allocation. No partial execution is accepted as successful.
    """
    task_id: str
    action: Action
    target: str
    asset: str
    payer: str
    reserved: str
    executor: str | None = None
    fee_recipient: str | None = None
    released: str = "0"
    fee: str = "0"
    refunded: str = "0"
    slashed: str = "0"
    minimum_finality: int = 1
    version: int = 1

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("unsupported settlement intent version")
        object.__setattr__(self, "action", Action(self.action))
        for name in ("task_id", "target", "asset", "payer"):
            reference(getattr(self, name))
        for name in ("executor", "fee_recipient"):
            if getattr(self, name) is not None:
                reference(getattr(self, name))
        bounded_int(self.minimum_finality, minimum=1)
        for name in ("reserved", "released", "fee", "refunded", "slashed"):
            object.__setattr__(self, name, _amount(getattr(self, name)))
        reserved, released, fee, refunded, slashed = (
            dec(getattr(self, name)) for name in ("reserved", "released", "fee", "refunded", "slashed")
        )
        if reserved <= ZERO:
            raise ValueError("zero-value work needs no settlement intent")
        if self.action == Action.HOLD:
            if money_sum(released, fee, refunded, slashed) != ZERO:
                raise ValueError("hold cannot disburse")
        elif money_sum(released, fee, refunded, slashed) != reserved:
            raise ValueError("settlement allocation does not conserve reserved amount")
        if (released and not self.executor) or (fee and not self.fee_recipient):
            raise ValueError("settlement allocation needs recipients")
        if self.action == Action.RELEASE and slashed:
            raise ValueError("release cannot slash")
        if self.action == Action.REFUND and (released or fee or slashed):
            raise ValueError("refund cannot release, charge a fee or slash")
        if self.action == Action.SLASH and (released or fee):
            raise ValueError("slash cannot release or charge a fee")

    def canonical(self) -> str:
        return canonical_json(asdict(self))


@dataclass(frozen=True)
class Command:
    intent_id: str
    intent_hash: str
    spec: IntentSpec


@dataclass(frozen=True)
class SubmissionReference:
    transaction_ref: str


@dataclass(frozen=True)
class VerifiedReceipt:
    """Facts from independently authenticated evidence, NOT a submission response.

    The verifier port is trusted code, not a client extension. It must verify
    rail identity, event authentication, unique intent binding, parties, amounts
    and canonical block/finality independently. Core rechecks all commitments.
    This type is not itself cryptographic proof; no concrete verifier ships.
    """
    intent_id: str
    intent_hash: str
    spec: IntentSpec
    transaction_ref: str
    block_ref: str | None = None
    finality_depth: int = 0


@dataclass(frozen=True)
class Observation:
    kind: ObservationKind
    receipt: VerifiedReceipt | None = None
    replacement_ref: str | None = None


class Submitter(Protocol):
    def submit(self, command: Command) -> SubmissionReference:
        """One bounded I/O call; use command.intent_id as the rail dedupe key.

        Never retry an ambiguous send internally as a new economic action.
        The worker will never call this again for an uncertain intent.
        """


class ReceiptVerifier(Protocol):
    def inspect(self, command: Command, transaction_ref: str | None) -> Observation:
        """Bounded read-only reconciliation, including lookup by unique intent ID.

        Must not submit, sign or perform economic actions. UNKNOWN (including
        not-found) never proves that an earlier submit did not take effect.
        """


def command_from(row: SettlementIntent) -> Command:
    spec = IntentSpec(**json.loads(row.canonical_intent))
    if (spec.canonical() != row.canonical_intent
            or sha256_bytes(row.canonical_intent.encode()) != row.intent_hash
            or spec.task_id != row.task_id or spec.target != row.target
            or row.slot != ("hold" if spec.action == Action.HOLD else "terminal")):
        raise ValueError("settlement intent commitment mismatch")
    return Command(row.id, row.intent_hash, spec)


def journal(db: Session, attempt: SettlementAttempt, kind: str, before: str | None, **details) -> None:
    db.add(SettlementAttemptEvent(
        id=new_id("SAE"), attempt_id=attempt.id, kind=kind,
        from_status=before, to_status=attempt.status, details=details, created_at=now(),
    ))


def finalized_hold(db: Session, spec: IntentSpec) -> bool:
    """Serialize a terminal enqueue/dispatch with hold reconciliation, SQL only."""
    row = db.scalar(select(SettlementIntent).where(
        SettlementIntent.task_id == spec.task_id, SettlementIntent.slot == "hold",
    ))
    if row is None:
        return False
    hold = command_from(row).spec
    if any(getattr(hold, k) != getattr(spec, k) for k in ("target", "asset", "payer", "reserved", "minimum_finality")):
        return False
    latest = db.scalar(select(SettlementAttempt.id).where(
        SettlementAttempt.intent_id == row.id,
    ).order_by(SettlementAttempt.number.desc()).limit(1))
    return db.execute(update(SettlementAttempt).where(
        SettlementAttempt.id == latest, SettlementAttempt.status == State.FINALIZED,
    ).values(status=SettlementAttempt.status).execution_options(synchronize_session=False)).rowcount == 1


def enqueue_intent(db: Session, spec: IntentSpec, *, idempotency_key: str) -> SettlementIntent:
    """Write intent + initial attempt + journal in the CALLER's transaction.

    Trusted in-process producers only. Caller must roll back its ENTIRE
    transaction on any error (no internal savepoint/commit). An exact key replay
    returns the immutable command, even if the attempt has since terminated.
    Competing different keys cannot create a second hold or terminal allocation.
    No existing API/provider invokes this in this phase.
    """
    reference(idempotency_key, maximum=160)
    # Re-validate even a spec constructed outside the regular dataclass path.
    spec = IntentSpec(**asdict(spec))
    encoded = spec.canonical()
    db.flush()
    existing = db.scalar(select(SettlementIntent).where(SettlementIntent.idempotency_key == idempotency_key))
    if existing:
        if command_from(existing).spec != spec:
            raise ValueError("settlement idempotency conflict")
        return existing
    task = db.get(Task, spec.task_id)
    if task is None or task.poster_did != spec.payer:
        raise ValueError("settlement intent must bind the task and its payer")
    if spec.action != Action.HOLD and not finalized_hold(db, spec):
        raise ValueError("terminal intent requires a matching finalized hold")
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise RuntimeError("settlement attempts require SQLite or PostgreSQL")
    identifier, stamp = new_id("SI"), now()
    inserted = db.scalar(insert(SettlementIntent).values(
        id=identifier, task_id=spec.task_id,
        slot="hold" if spec.action == Action.HOLD else "terminal",
        idempotency_key=idempotency_key, target=spec.target, canonical_intent=encoded,
        intent_hash=sha256_bytes(encoded.encode()), created_at=stamp,
    ).on_conflict_do_nothing().returning(SettlementIntent.id))
    if inserted is None:
        existing = db.scalar(select(SettlementIntent).where(
            SettlementIntent.idempotency_key == idempotency_key,
        ).execution_options(populate_existing=True))
        if existing is not None and command_from(existing).spec == spec:
            return existing
        raise ValueError("settlement idempotency or terminal allocation conflict")
    attempt = SettlementAttempt(
        id=new_id("SAT"), intent_id=identifier, number=1, status=State.PENDING,
        next_attempt_at=stamp, verification_attempts=0, finality_depth=0,
        created_at=stamp, updated_at=stamp,
    )
    db.add(attempt)
    db.flush()
    journal(db, attempt, "ENQUEUED", None)
    db.flush()
    return db.get(SettlementIntent, identifier)
