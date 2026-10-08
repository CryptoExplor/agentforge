"""Dormant chain-agnostic settlement worker; no provider selection or RPC driver.

Only plain immutable values cross the I/O boundary. Each database phase uses a
fresh, short transaction and closes its Session before calling either port.
Submission is at-most-once *invocation*, not a claim of exactly-once economics:
crash/timeout ambiguity is reconciled, never blindly resubmitted. External
exactly-once effects still require a pinned rail's intent-ID deduplication.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .models import SettlementAttempt, SettlementIntent
from .services import new_id, now
from .settlement_attempts import (
    Action, Command, Observation, ObservationKind, ReceiptVerifier, State,
    SubmissionReference, Submitter, VerifiedReceipt, bounded_int, command_from,
    finalized_hold, journal, reference,
)

# FINALIZED remains under read-only observation for deep reorg detection.
# FAILED and REPLACED are terminal for this attempt, not automatic retry signals.
RECONCILABLE = (State.PENDING, State.SUBMITTED, State.CONFIRMED, State.FINALIZED,
                State.REORGED, State.MANUAL_REVIEW)


@dataclass(frozen=True)
class Work:
    attempt_id: str
    owner: str
    command: Command
    transaction_ref: str | None
    submit: bool


class SettlementWorker:
    """One bounded tick, for a future operator-owned scheduler to invoke.

    No default ports, dynamic imports, environment enable switch, or HTTP route.
    Ports must impose I/O timeouts; slow calls can lose their lease and their
    writes will be fenced out. A successor only reconciles the committed intent.
    """
    def __init__(self, sessions: Callable[[], Session], *, target: str,
                 submitter: Submitter, verifier: ReceiptVerifier,
                 lease_seconds: float = 60, poll_seconds: float = 30):
        self.target = reference(target)
        if submitter is verifier:
            raise ValueError("submission and receipt verification must be separate ports")
        for value in (lease_seconds, poll_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 3600:
                raise ValueError("worker intervals must be finite and between 1 and 3600 seconds")
        self.sessions, self.submitter, self.verifier = sessions, submitter, verifier
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds

    @staticmethod
    def _available(stamp):
        return (
            SettlementAttempt.status.in_(RECONCILABLE),
            SettlementAttempt.next_attempt_at <= stamp,
            or_(SettlementAttempt.lease_owner.is_(None), SettlementAttempt.lease_expires_at <= stamp),
        )

    def tick(self, *, limit: int = 20) -> int:
        """Process at most limit candidates; returns number of port calls."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("worker limit must be between 1 and 100")
        with self.sessions() as db:
            identifiers = list(db.scalars(select(SettlementAttempt.id).join(
                SettlementIntent, SettlementIntent.id == SettlementAttempt.intent_id,
            ).where(SettlementIntent.target == self.target, *self._available(now()))
                .order_by(SettlementAttempt.next_attempt_at, SettlementAttempt.id).limit(limit)))
        processed = 0
        for identifier in identifiers:
            work = self._claim(identifier)
            if work is None:
                continue
            processed += 1
            # Absolutely no Session/ORM object survives into these calls.
            if work.submit:
                try:
                    result = self.submitter.submit(work.command)
                    if type(result) is not SubmissionReference:
                        raise ValueError("invalid submission reference")
                    reference(result.transaction_ref)
                except Exception:
                    self._record_error(work, "SUBMISSION_UNCERTAIN")
                else:
                    self._record_submission(work, result.transaction_ref)
            else:
                try:
                    observation = self.verifier.inspect(work.command, work.transaction_ref)
                    self._validate_observation(work, observation)
                except Exception:
                    self._record_error(work, "VERIFICATION_UNAVAILABLE")
                else:
                    self._record_observation(work, observation)
        return processed

    def _claim(self, identifier: str) -> Work | None:
        stamp, owner = now(), uuid.uuid4().hex
        with self.sessions() as db, db.begin():
            won = db.execute(update(SettlementAttempt).where(
                SettlementAttempt.id == identifier, *self._available(stamp),
                SettlementAttempt.intent_id.in_(select(SettlementIntent.id).where(SettlementIntent.target == self.target)),
            ).values(lease_owner=owner, lease_expires_at=stamp + self.lease_seconds)
                .execution_options(synchronize_session=False)).rowcount
            if won != 1:
                return None
            row = db.get(SettlementAttempt, identifier, populate_existing=True)
            # The conditional write can wait on a database lock. Do not begin
            # I/O on a lease that expired while acquiring that lock.
            if row.lease_expires_at <= now():
                row.lease_owner = row.lease_expires_at = None
                return None
            before = row.status
            try:
                command = command_from(db.get(SettlementIntent, row.intent_id))
            except (ValueError, TypeError, KeyError):
                self._error(db, row, "INTENT_CORRUPT")
                return None
            submit = row.status == State.PENDING and row.submission_started_at is None
            if submit and command.spec.action != Action.HOLD and not finalized_hold(db, command.spec):
                self._error(db, row, "HOLD_NOT_FINALIZED")
                return None
            if submit:
                # Commit before I/O: if the process dies even BEFORE calling the
                # port, recovery must assume submission may have happened.
                row.submission_started_at = stamp
            else:
                row.verification_attempts += 1
                if row.status == State.PENDING:
                    row.status = State.MANUAL_REVIEW
                    row.last_error = "SUBMISSION_UNCERTAIN"
            row.updated_at = stamp
            journal(db, row, "DISPATCH_STARTED" if submit else "RECONCILIATION_STARTED", before)
            return Work(identifier, owner, command, row.transaction_ref, submit)

    def _owned(self, db: Session, work: Work) -> SettlementAttempt | None:
        # Fence by both owner AND unexpired lease, including when there is not
        # yet a successor. Conditional write holds the row until this commit.
        won = db.execute(update(SettlementAttempt).where(
            SettlementAttempt.id == work.attempt_id,
            SettlementAttempt.lease_owner == work.owner,
            SettlementAttempt.lease_expires_at > now(),
        ).values(status=SettlementAttempt.status).execution_options(synchronize_session=False)).rowcount
        if won != 1:
            return None
        row = db.get(SettlementAttempt, work.attempt_id, populate_existing=True)
        # The UPDATE may have blocked. Recheck wall time under the acquired lock,
        # not just the instant at which its predicate was constructed.
        return row if row.lease_expires_at > now() else None

    def _release(self, row: SettlementAttempt, *, backoff: bool = False):
        delay = self.poll_seconds
        if backoff:
            delay = min(3600, delay * 2 ** min(row.verification_attempts, 7))
        row.next_attempt_at = now() + delay
        row.lease_owner = row.lease_expires_at = None
        row.updated_at = now()

    def _error(self, db: Session, row: SettlementAttempt, code: str):
        before = row.status
        row.status, row.last_error = State.MANUAL_REVIEW, code
        self._release(row, backoff=True)
        journal(db, row, "REVIEW_REQUIRED", before, error_code=code)

    def _record_error(self, work: Work, code: str):
        with self.sessions() as db, db.begin():
            row = self._owned(db, work)
            if row is not None:
                self._error(db, row, code)

    def _record_submission(self, work: Work, transaction_ref: str):
        with self.sessions() as db, db.begin():
            row = self._owned(db, work)
            if row is None:
                return
            before = row.status
            row.transaction_ref, row.status = transaction_ref, State.SUBMITTED
            row.last_error = None
            self._release(row)
            journal(db, row, "SUBMISSION_RECORDED", before, transaction_ref=transaction_ref)

    @staticmethod
    def _validate_observation(work: Work, observation: Observation):
        if type(observation) is not Observation or type(observation.kind) is not ObservationKind:
            raise ValueError("invalid independent observation")
        if observation.kind == ObservationKind.UNKNOWN:
            if observation.receipt is not None or observation.replacement_ref is not None:
                raise ValueError("unknown observation cannot carry verified facts")
            return
        receipt = observation.receipt
        if type(receipt) is not VerifiedReceipt or type(receipt.spec) is not type(work.command.spec):
            raise ValueError("independent receipt required")
        if (receipt.intent_id != work.command.intent_id or receipt.intent_hash != work.command.intent_hash
                or receipt.spec != work.command.spec):
            raise ValueError("receipt does not bind the committed economic intent")
        reference(receipt.transaction_ref)
        if work.transaction_ref is not None and receipt.transaction_ref != work.transaction_ref:
            raise ValueError("receipt transaction does not match attempt")
        bounded_int(receipt.finality_depth)
        if receipt.block_ref is not None:
            reference(receipt.block_ref)
        if observation.kind in (ObservationKind.CONFIRMED, ObservationKind.FINALIZED, ObservationKind.FAILED):
            if receipt.block_ref is None or receipt.finality_depth < 1:
                raise ValueError("confirmation requires block evidence")
        if observation.kind in (ObservationKind.FINALIZED, ObservationKind.FAILED) and receipt.finality_depth < work.command.spec.minimum_finality:
            raise ValueError("insufficient finality")
        if observation.kind == ObservationKind.REPLACED:
            reference(observation.replacement_ref)
            if observation.replacement_ref == receipt.transaction_ref:
                raise ValueError("replacement must identify another transaction")
        elif observation.replacement_ref is not None:
            raise ValueError("unexpected replacement reference")

    def _record_observation(self, work: Work, observation: Observation):
        with self.sessions() as db, db.begin():
            row = self._owned(db, work)
            if row is None:
                return
            if observation.kind == ObservationKind.UNKNOWN:
                self._error(db, row, "RECEIPT_UNKNOWN")
                return
            receipt = observation.receipt
            # A recovered submission may name an old/replaced hash: never let
            # an unbound lookup resurrect a prior economic attempt.
            duplicate = db.scalar(select(SettlementAttempt.id).where(
                SettlementAttempt.intent_id == row.intent_id,
                SettlementAttempt.id != row.id,
                SettlementAttempt.transaction_ref == receipt.transaction_ref,
            ))
            if duplicate:
                self._error(db, row, "TRANSACTION_REUSED")
                return
            before = row.status
            row.transaction_ref = receipt.transaction_ref
            row.block_ref, row.finality_depth = receipt.block_ref, receipt.finality_depth
            if observation.kind == ObservationKind.REPLACED:
                # The independent verifier attests replacement of the same
                # intent, not permission for the worker to send another action.
                seen = db.scalar(select(SettlementAttempt.id).where(
                    SettlementAttempt.intent_id == row.intent_id,
                    SettlementAttempt.transaction_ref == observation.replacement_ref,
                ))
                if seen or row.number >= 100:
                    self._error(db, row, "REPLACEMENT_REJECTED")
                    return
                row.status = State.REPLACED
                db.flush()  # release the partial unique active-attempt slot
                successor = SettlementAttempt(
                    id=new_id("SAT"), intent_id=row.intent_id, number=row.number + 1,
                    status=State.SUBMITTED, transaction_ref=observation.replacement_ref,
                    submission_started_at=row.submission_started_at,
                    next_attempt_at=now() + self.poll_seconds, verification_attempts=0,
                    finality_depth=0, created_at=now(), updated_at=now(),
                )
                db.add(successor)
                db.flush()
                journal(db, successor, "REPLACEMENT_OBSERVED", None, replaces_attempt_id=row.id)
            elif before == State.FINALIZED and observation.kind in (ObservationKind.SUBMITTED, ObservationKind.CONFIRMED):
                row.status = State.REORGED
            else:
                row.status = State(observation.kind.value)
            row.last_error = None
            self._release(row)
            journal(db, row, "RECEIPT_OBSERVED", before,
                    transaction_ref=receipt.transaction_ref, block_ref=receipt.block_ref,
                    finality_depth=receipt.finality_depth)
