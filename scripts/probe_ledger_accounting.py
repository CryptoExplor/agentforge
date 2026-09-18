"""Opt-in ledger integrity probes; only a disposable local SQLite database is used.

Run after installing the project: python scripts/probe_ledger_accounting.py
Exit 1 means an accounting invariant failed; exit 0 means these probes passed.
This is helper-level evidence, NOT a concurrent HTTP/PostgreSQL certification.
The race probe changes scheduling only: both actual account reads finish before
allowing either actual ledger operation to continue. No account value is forged.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, getcontext, localcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from agentforge_server import services
from agentforge_server.models import Agent, Base, LedgerAccount, LedgerEvent
from agentforge_server.schemas import Money


def seed(engine, did, balance):
    with Session(engine) as session:
        session.add(Agent(did=did, name="Disposable audit identity", created_at=0, updated_at=0))
        session.flush()
        services.ensure_account(session, did, "MOCK", balance)
        session.commit()


def race_probe(engine):
    did = "did:audit:ledger-race"
    seed(engine, did, "100")
    both_read = Barrier(2, timeout=10)
    original = services.ensure_account

    def synchronized_read(*args, **kwargs):
        account = original(*args, **kwargs)
        both_read.wait()
        return account

    def debit(index):
        with Session(engine, autoflush=False, expire_on_commit=False) as session:
            try:
                services.post_ledger_event(
                    session, did=did, asset="MOCK", delta=Decimal("-60"),
                    reason="AUDIT_ONLY", idempotency_key=f"audit:race:{index}",
                )
                session.commit()
                return "accepted"
            except ValueError as exc:
                session.rollback()
                if str(exc) != "insufficient mock balance":
                    raise
                return "insufficient_balance"

    with patch.object(services, "ensure_account", synchronized_read):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(debit, index) for index in range(2)]
            outcomes = [future.result(timeout=20) for future in futures]

    with Session(engine) as session:
        balance = Decimal(session.scalar(select(LedgerAccount.balance).where(LedgerAccount.did == did)))
        deltas = [Decimal(value) for value in session.scalars(
            select(LedgerEvent.amount_delta).where(LedgerEvent.did == did)
        )]
    expected = Decimal("100") + sum(deltas, Decimal("0"))
    return {
        "probe": "concurrent_helper_debits",
        "outcomes": outcomes,
        "stored_balance": str(balance),
        "initial_balance_plus_recorded_deltas": str(expected),
        "event_count": len(deltas),
        "invariant_ok": balance == expected and balance >= 0 and outcomes.count("accepted") <= 1,
    }


def precision_probe(engine):
    did = "did:audit:ledger-precision"
    seed(engine, did, "1000")
    amount = Money(amount="1e-29", asset="MOCK").amount
    delta = Decimal(amount).copy_negate()
    with Session(engine) as session:
        services.post_ledger_event(
            session, did=did, asset="MOCK", delta=delta,
            reason="AUDIT_ONLY", idempotency_key="audit:precision",
        )
        session.commit()
        balance = Decimal(session.scalar(select(LedgerAccount.balance).where(LedgerAccount.did == did)))
        recorded_delta = session.scalar(select(LedgerEvent.amount_delta).where(LedgerEvent.did == did))
    # Higher precision is used ONLY to calculate the reference answer, not to
    # alter the application operation or its ordinary decimal context.
    with localcontext() as context:
        context.prec = 200
        expected = Decimal("1000") + delta
    return {
        "probe": "accepted_amount_exact_debit",
        "decimal_context_precision": getcontext().prec,
        "schema_accepted_amount": amount,
        "recorded_delta": recorded_delta,
        "stored_balance": format(balance, "f"),
        "expected_exact_balance": format(expected, "f"),
        "invariant_ok": balance == expected and Decimal(recorded_delta) == delta,
    }


def main():
    with TemporaryDirectory(prefix="agentforge-ledger-audit-") as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'disposable.db'}")
        try:
            Base.metadata.create_all(engine)
            results = [race_probe(engine), precision_probe(engine)]
        finally:
            engine.dispose()
    print(json.dumps(results, indent=2))
    return 0 if all(result["invariant_ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
