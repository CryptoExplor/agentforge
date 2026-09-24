"""Exact money, transaction ownership and competing lifecycle regressions.

Uses the same disposable per-test PostgreSQL schemas as the security/outbox suite
when AGENTFORGE_TEST_POSTGRES_URL is set; otherwise disposable SQLite files.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, localcontext, ROUND_UP
from pathlib import Path
import runpy
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from agentforge_server import db, services
from agentforge_server.app import create_app
from agentforge_server.models import (
    Agent, AuditEvent, Base, Claim, Dispute, Escrow, LedgerAccount, LedgerEvent,
    OutboxEvent, ReputationEvent, Submission, Task, ValidationDecision,
)
from agentforge_server.money import dec, money_string, money_sum
from agentforge_server.settings import settings
from agentforge_sdk.client import AgentIdentity
from test_outbox_regressions import audit_database, client
from test_audit_fixes import (
    register, signed_request, task_payload, create_task, submit, decision_payload,
)

PROBES = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/probe_ledger_accounting.py"))
seed = PROBES["seed"]


@pytest.fixture
def ledger_engine(audit_database):
    engine = create_engine(audit_database)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def balance(session, did, asset="MOCK"):
    return Decimal(session.scalar(select(LedgerAccount.balance).where(
        LedgerAccount.did == did, LedgerAccount.asset == asset,
    )) or "0")


def post(session, *, did="did:audit:payer", delta="1", key="event", **kwargs):
    return services.post_ledger_event(
        session, did=did, asset=kwargs.pop("asset", "MOCK"), delta=Decimal(delta),
        reason=kwargs.pop("reason", "AUDIT"), idempotency_key=key, **kwargs,
    )


def parallel(*operations):
    start = threading.Barrier(len(operations), timeout=15)
    def run(operation):
        start.wait()
        return operation()
    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        futures = [pool.submit(run, operation) for operation in operations]
        return [future.result(timeout=30) for future in futures]


@pytest.mark.parametrize("probe", ["race_probe", "precision_probe"])
def test_original_ledger_probes_are_regressions(ledger_engine, probe):
    result = PROBES[probe](ledger_engine)
    assert result["invariant_ok"], result


@pytest.mark.parametrize("value", [float("nan"), 0.1, True, "NaN", "Infinity", "-1", "1e999999999", "1e-999999999", "9" * 81])
def test_money_rejects_inexact_or_unbounded_input(value):
    with pytest.raises(ValueError):
        dec(value)


def test_money_uses_own_exact_context_and_checks_signed_storage_width():
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        assert money_sum(Decimal("1000"), Decimal("-1e-29")) == Decimal("999.99999999999999999999999999999")
    with pytest.raises(ValueError):
        money_sum(Decimal("9" * 80), Decimal("1"))
    with pytest.raises(ValueError):
        money_string(Decimal("9" * 80).copy_negate())
    with pytest.raises(ValueError):
        money_sum(Decimal("1000"), Decimal("-1e-78"))


@pytest.mark.parametrize("change", [
    {"did": "did:audit:other"}, {"asset": "TEST_CREDIT"}, {"delta": "2"},
    {"reason": "DIFFERENT"}, {"task_id": "different-task"},
])
def test_ledger_replay_compares_all_immutable_content(ledger_engine, change):
    seed(ledger_engine, "did:audit:payer", "100")
    with Session(ledger_engine) as session:
        event = post(session)
        session.commit()
        event_id = event.id
        assert post(session, delta="1.00").id == event_id
        with pytest.raises(ValueError, match="different content"):
            post(session, **change)
        session.rollback()
        assert balance(session, "did:audit:payer") == 101
        assert session.scalar(select(func.count()).select_from(LedgerEvent)) == 1


@pytest.mark.parametrize("same_key", [False, True])
@pytest.mark.parametrize("new_account", [False, True])
def test_concurrent_credits_and_account_creation(ledger_engine, same_key, new_account):
    seed(ledger_engine, "did:audit:payer", "0")
    asset = "TEST_CREDIT" if new_account else "MOCK"
    def operation(index):
        with Session(ledger_engine, autoflush=False) as session:
            event = post(session, key="same" if same_key else str(index), asset=asset)
            session.commit()
            return event.id
    ids = parallel(lambda: operation(0), lambda: operation(1))
    with Session(ledger_engine) as session:
        expected = 1 if same_key else 2
        assert balance(session, "did:audit:payer", asset) == expected
        assert len(set(ids)) == expected
        assert session.scalar(select(func.count()).select_from(LedgerEvent)) == expected
        if new_account:
            assert balance(session, "did:audit:payer") == 0


def test_concurrent_conflicting_replay_never_applies_second_delta(ledger_engine):
    seed(ledger_engine, "did:audit:payer", "100")
    def operation(delta):
        with Session(ledger_engine) as session:
            try:
                post(session, delta=delta)
                session.commit()
                return "accepted"
            except ValueError:
                session.rollback()
                return "conflict"
    assert sorted(parallel(lambda: operation("1"), lambda: operation("2"))) == ["accepted", "conflict"]
    with Session(ledger_engine) as session:
        events = session.scalars(select(LedgerEvent)).all()
        assert len(events) == 1
        assert balance(session, "did:audit:payer") == 100 + Decimal(events[0].amount_delta)


def test_stale_identity_map_and_transaction_rollback(ledger_engine):
    seed(ledger_engine, "did:audit:payer", "100")
    with Session(ledger_engine, expire_on_commit=False) as stale:
        account = services.ensure_account(stale, "did:audit:payer")
        with Session(ledger_engine) as fresh:
            post(fresh, key="fresh", delta="5")
            fresh.commit()
        assert account.balance == "100"
        post(stale, key="stale", delta="-5")
        stale.commit()
        assert balance(stale, "did:audit:payer") == 100
        post(stale, key="rollback", delta="10")
        stale.rollback()
        assert balance(stale, "did:audit:payer") == 100
        assert stale.scalar(select(func.count()).select_from(LedgerEvent)) == 2
        assert services.ensure_account(stale, "did:audit:payer", initial_balance="1000").balance == "100"


def test_postgres_forced_cas_collision_retries_fresh_balance(ledger_engine, monkeypatch):
    if ledger_engine.dialect.name != "postgresql":
        pytest.skip("requires independent PostgreSQL writes before account CAS")
    seed(ledger_engine, "did:audit:payer", "100")
    barrier = threading.Barrier(2, timeout=15)
    local = threading.local()
    execute = Session.execute
    def intercept(session, statement, *args, **kwargs):
        if (getattr(statement, "is_update", False) and statement.table.name == "ledger_accounts"
                and not getattr(local, "seen", False)):
            local.seen = True
            barrier.wait()
        return execute(session, statement, *args, **kwargs)
    monkeypatch.setattr(Session, "execute", intercept)
    def credit(index):
        with Session(ledger_engine) as session:
            post(session, key=str(index), delta="10")
            session.commit()
    parallel(lambda: credit(0), lambda: credit(1))
    with Session(ledger_engine) as session:
        assert balance(session, "did:audit:payer") == 120
        assert session.scalar(select(func.count()).select_from(LedgerEvent)) == 2


def make_funded(engine, *, reward="10", deposit="2", inference="1", fee=None):
    seed(engine, "did:audit:payer", "1000")
    seed(engine, "did:audit:executor", "0")
    task_id = "T_" + uuid.uuid4().hex
    economics = {"reward": {"amount": reward}, "security_deposit": {"amount": deposit},
                 "inference_budget": {"amount": inference}}
    if fee:
        economics.update(fee)
    with Session(engine) as session:
        task = Task(id=task_id, poster_did="did:audit:payer", kind="research", origin="research",
                    acceptance_hash="a" * 64, task_hash="b" * 64, created_at=0, updated_at=0,
                    economics=economics)
        session.add(task)
        session.flush()
        services.fund_task(session, task)
        session.commit()
    return task_id


@pytest.mark.parametrize("decision,settlement", [
    ("VERIFIED", {}), ("REJECTED", {}), ("PARTIAL", {"executor_amount": "1e-29"}),
    ("SLASHED", {"slash_subject": "requester"}), ("SLASHED", {"slash_subject": "executor"}),
])
def test_tiny_amounts_conserve_through_every_escrow_transition(ledger_engine, decision, settlement):
    task_id = make_funded(ledger_engine, reward="1.00000000000000000000000000001", deposit="2e-29", inference="3e-29")
    with Session(ledger_engine) as session, localcontext() as context:
        context.prec = 2  # Application code must not inherit this precision.
        services.escrow_settle(session, task=session.get(Task, task_id), executor_did="did:audit:executor",
                               decision=decision, settlement=settlement)
        session.commit()
        escrow = session.get(Escrow, task_id)
        with localcontext() as reference:
            reference.prec = 200
            released, refunded, burned = map(Decimal, (escrow.released_amount, escrow.refunded_amount, escrow.slashed_amount))
            assert released + refunded + burned == Decimal(escrow.reserved_total)
            assert balance(session, "did:audit:executor") == released
            assert balance(session, "did:audit:payer") == Decimal("1000") - released - burned
            deltas = [Decimal(row.amount_delta) for row in session.scalars(select(LedgerEvent))]
            assert sum(deltas, Decimal("0")) == -burned


def test_two_terminal_escrow_decisions_have_one_winner(ledger_engine, monkeypatch):
    task_id = make_funded(ledger_engine)
    barrier = threading.Barrier(2, timeout=15)
    execute = Session.execute
    def intercept(session, statement, *args, **kwargs):
        if getattr(statement, "is_update", False) and statement.table.name == "escrows":
            barrier.wait()
        return execute(session, statement, *args, **kwargs)
    monkeypatch.setattr(Session, "execute", intercept)
    def settle(decision):
        with Session(ledger_engine) as session:
            try:
                services.escrow_settle(session, task=session.get(Task, task_id),
                    executor_did="did:audit:executor", decision=decision)
                session.commit()
                return "accepted"
            except ValueError as exc:
                assert "terminal" in str(exc)
                session.rollback()
                return "terminal"
    assert sorted(parallel(lambda: settle("VERIFIED"), lambda: settle("REJECTED"))) == ["accepted", "terminal"]
    with Session(ledger_engine) as session:
        escrow = session.get(Escrow, task_id)
        assert escrow.status in {"RELEASED", "REFUNDED"}
        assert balance(session, "did:audit:payer") + balance(session, "did:audit:executor") == 1000
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_late_settlement_error_rolls_back_credits_and_terminal_guard(ledger_engine, monkeypatch):
    import agentforge_server.adapters.mock_settlement as adapter
    task_id = make_funded(ledger_engine)
    original = adapter.post_ledger_event
    calls = []
    def fail_second(*args, **kwargs):
        calls.append(kwargs["did"])
        if len(calls) == 2:
            raise ValueError("injected late failure")
        return original(*args, **kwargs)
    with Session(ledger_engine) as session:
        with monkeypatch.context() as scoped:
            scoped.setattr(adapter, "post_ledger_event", fail_second)
            with pytest.raises(ValueError, match="injected"):
                services.escrow_settle(session, task=session.get(Task, task_id), executor_did="did:audit:executor", decision="VERIFIED")
            session.rollback()
        assert session.get(Escrow, task_id).status == "FUNDED"
        assert balance(session, "did:audit:payer") == 987
        assert balance(session, "did:audit:executor") == 0
        assert session.scalar(select(func.count()).select_from(LedgerEvent)) == 1
        services.escrow_settle(session, task=session.get(Task, task_id), executor_did="did:audit:executor", decision="VERIFIED")
        session.commit()
        assert balance(session, "did:audit:payer") == 990
        assert balance(session, "did:audit:executor") == 10
        assert calls == sorted(calls)  # Stable account lock order, not role order.


def economics(amount="10"):
    return {"mode": "BOUNTY", "reward": {"amount": amount, "asset": "MOCK"}}


def test_api_funding_failure_rolls_back_task_escrow_ledger_and_outbox(client, monkeypatch):
    import agentforge_server.app as app_module
    poster = AgentIdentity.generate()
    register(client, poster)
    with db.SessionLocal() as session:
        initial_outbox = session.scalar(select(func.count()).select_from(OutboxEvent))
    original = app_module.queue_outbox
    def fail_outbox(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("injected outbox failure")
    monkeypatch.setattr(app_module, "queue_outbox", fail_outbox)
    response = signed_request(client, poster, "POST", "/api/v1/tasks", task_payload(economics=economics()))
    assert response.status_code == 400, response.text
    with db.SessionLocal() as session:
        assert balance(session, poster.did) == 1000
        for model in (Task, Escrow, LedgerEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == initial_outbox


def test_api_unrepresentable_balance_fails_without_partial_reservation(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    response = signed_request(client, poster, "POST", "/api/v1/tasks", task_payload(economics=economics("1e-78")))
    assert response.status_code == 400, response.text
    with db.SessionLocal() as session:
        assert balance(session, poster.did) == 1000
        assert session.scalar(select(func.count()).select_from(LedgerEvent)) == 0
        assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_api_concurrent_funding_cannot_overspend(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    body = task_payload(economics=economics("600"))
    with TestClient(create_app()) as second:
        responses = parallel(
            lambda: signed_request(client, poster, "POST", "/api/v1/tasks", body),
            lambda: signed_request(second, poster, "POST", "/api/v1/tasks", body),
        )
    assert sorted(r.status_code for r in responses) == [200, 400], [r.text for r in responses]
    with db.SessionLocal() as session:
        assert balance(session, poster.did) == 400
        for model in (Task, Escrow, LedgerEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def prepared_submission(client, funded):
    poster, executor, validator = (AgentIdentity.generate() for _ in range(3))
    register(client, poster)
    register(client, executor)
    register(client, validator, {"name": "validator", "capabilities": ["validation"]})
    settings.trusted_validator_dids = settings.trusted_validator_dids | {validator.did}
    task = create_task(client, poster, task_payload(economics=economics() if funded else None))
    assert signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}).status_code == 200
    submission, _ = submit(client, task, executor)
    return poster, executor, validator, task, submission


def synchronize_submission_guards(monkeypatch):
    if db.engine.dialect.name == "postgresql":
        import agentforge_server.app as app_module
        guard = app_module.guard_pending_submission
        barrier = threading.Barrier(2, timeout=15)
        def synchronized(*args):
            barrier.wait()
            return guard(*args)
        monkeypatch.setattr(app_module, "guard_pending_submission", synchronized)


@pytest.mark.parametrize("funded", [False, True])
def test_api_competing_validations_emit_one_decision_and_reputation_set(client, monkeypatch, funded):
    poster, executor, validator, task, submission = prepared_submission(client, funded)
    path = f"/api/v1/submissions/{submission['submission_id']}/validate"
    bodies = [decision_payload(client, validator, submission, decision) for decision in ("VERIFIED", "REJECTED")]
    synchronize_submission_guards(monkeypatch)
    with TestClient(create_app()) as second:
        responses = parallel(
            lambda: signed_request(client, validator, "POST", path, bodies[0]),
            lambda: signed_request(second, validator, "POST", path, bodies[1]),
        )
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(ValidationDecision)) == 1
        assert session.scalar(select(func.count()).select_from(ReputationEvent)) == 3
        assert session.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.kind == "VALIDATION_RECORDED")) == 1
        assert session.get(Task, task["id"]).status == session.get(Submission, submission["submission_id"]).status
        assert balance(session, poster.did) + balance(session, executor.did) == 2000


def test_api_concurrent_disputes_have_one_open_record(client, monkeypatch):
    poster, executor, validator, task, submission = prepared_submission(client, True)
    path = f"/api/v1/submissions/{submission['submission_id']}/disputes"
    synchronize_submission_guards(monkeypatch)
    def body():
        return {"dispute_id": "D_" + uuid.uuid4().hex, "reason": "Please review this result", "additional_evidence": []}
    with TestClient(create_app()) as second:
        responses = parallel(
            lambda: signed_request(client, poster, "POST", path, body()),
            lambda: signed_request(second, executor, "POST", path, body()),
        )
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Dispute)) == 1
        assert session.get(Escrow, task["id"]).status == "FROZEN"


def test_api_dispute_validation_race_cannot_resurrect_terminal_state(client, monkeypatch):
    poster, executor, validator, task, submission = prepared_submission(client, True)
    base = f"/api/v1/submissions/{submission['submission_id']}"
    body = decision_payload(client, validator, submission)
    synchronize_submission_guards(monkeypatch)
    with TestClient(create_app()) as second:
        responses = parallel(
            lambda: signed_request(client, validator, "POST", base + "/validate", body),
            lambda: signed_request(second, poster, "POST", base + "/disputes",
                                   {"dispute_id": "D_race", "reason": "Please review this result"}),
        )
    assert responses[0].status_code == 200, responses[0].text
    assert responses[1].status_code in {200, 409}, responses[1].text
    with db.SessionLocal() as session:
        assert session.get(Submission, submission["submission_id"]).status == "VERIFIED"
        assert session.get(Task, task["id"]).status == "VERIFIED"
        assert session.get(Escrow, task["id"]).status == "RELEASED"
        assert session.scalar(select(func.count()).select_from(Dispute).where(Dispute.status == "OPEN")) == 0


@pytest.mark.parametrize("funded", [False, True])
def test_api_cancel_claim_race_preserves_one_transition(client, funded):
    poster, executor = AgentIdentity.generate(), AgentIdentity.generate()
    register(client, poster)
    register(client, executor)
    task = create_task(client, poster, task_payload(economics=economics() if funded else None))
    base = f"/api/v1/tasks/{task['id']}"
    with TestClient(create_app()) as second:
        responses = parallel(
            lambda: signed_request(client, poster, "POST", base + "/cancel", {}),
            lambda: signed_request(second, executor, "POST", base + "/claim", {}),
        )
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    with db.SessionLocal() as session:
        task_row = session.get(Task, task["id"])
        if task_row.status == "CANCELLED":
            if funded:
                assert session.get(Escrow, task["id"]).status == "REFUNDED"
            assert balance(session, poster.did) == 1000
            assert session.scalar(select(func.count()).select_from(Claim)) == 0
        else:
            assert task_row.status == "CLAIMED"
            if funded:
                assert session.get(Escrow, task["id"]).status == "FUNDED"
            assert balance(session, poster.did) == (990 if funded else 1000)
            assert session.scalar(select(func.count()).select_from(Claim)) == 1


def test_api_executor_claim_limit_is_atomic_across_tasks(client):
    poster, executor = AgentIdentity.generate(), AgentIdentity.generate()
    register(client, poster)
    register(client, executor)
    tasks = [create_task(client, poster) for _ in range(11)]
    for task in tasks[:9]:
        assert signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}).status_code == 200
    with TestClient(create_app()) as second:
        responses = parallel(
            lambda: signed_request(client, executor, "POST", f"/api/v1/tasks/{tasks[9]['id']}/claim", {}),
            lambda: signed_request(second, executor, "POST", f"/api/v1/tasks/{tasks[10]['id']}/claim", {}),
        )
    assert sorted(r.status_code for r in responses) == [200, 429], [r.text for r in responses]
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Claim).where(Claim.status == "ACTIVE")) == 10


def test_postgres_cancel_rechecks_state_after_competing_claim(client, monkeypatch):
    if db.engine.dialect.name != "postgresql":
        pytest.skip("requires concurrent HTTP writes before task CAS")
    poster, executor = AgentIdentity.generate(), AgentIdentity.generate()
    register(client, poster)
    register(client, executor)
    task = create_task(client, poster, task_payload(economics=economics()))
    reached = threading.Event()
    release = threading.Event()
    execute = Session.execute
    def intercept(session, statement, *args, **kwargs):
        if (getattr(statement, "is_update", False) and statement.table.name == "tasks"
                and statement.compile().params.get("status") == "CANCELLED"):
            reached.set()
            assert release.wait(15), "competing claim did not finish"
        return execute(session, statement, *args, **kwargs)
    monkeypatch.setattr(Session, "execute", intercept)
    with TestClient(create_app()) as second, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(signed_request, client, poster, "POST", f"/api/v1/tasks/{task['id']}/cancel", {})
        try:
            assert reached.wait(15), "cancellation did not reach its atomic guard"
            claimed = signed_request(second, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
            assert claimed.status_code == 200, claimed.text
        finally:
            release.set()
        cancelled = future.result(timeout=20)
    assert cancelled.status_code == 409, cancelled.text
    with db.SessionLocal() as session:
        assert session.get(Task, task["id"]).status == "CLAIMED"
        assert session.get(Escrow, task["id"]).status == "FUNDED"
        assert balance(session, poster.did) == 990
        assert session.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.kind == "TASK_CANCELLED")) == 0


# ---------------------------------------------------------------------------
# Phase 1.3: generic platform fee engine on the mock settlement ledger.
# Conservation: executor_release + platform_fee + requester_refund + slash
# == reserved_total. Zero fee on refunds and slashes, always.
# ---------------------------------------------------------------------------


def fee_economics(*, mode, bps=0, fixed="0", reward="10", deposit="2", inference="1"):
    return {
        "mode": "BOUNTY",
        "service_fee_mode": mode,
        "service_fee_bps": bps,
        "agentforge_service_fee": {"amount": fixed, "asset": "MOCK"},
        "reward": {"amount": reward, "asset": "MOCK"},
        "security_deposit": {"amount": deposit, "asset": "MOCK"},
        "inference_budget": {"amount": inference, "asset": "MOCK"},
    }


def fee_event(session, task_id, decision):
    return session.scalar(select(LedgerEvent).where(
        LedgerEvent.idempotency_key == f"task:{task_id}:fee:{decision}",
    ))


def test_platform_fee_bps_full_release_exact_accounting(ledger_engine):
    task_id = make_funded(ledger_engine, fee=fee_economics(mode="bps", bps=500))
    with Session(ledger_engine) as session:
        services.escrow_settle(session, task=session.get(Task, task_id),
                               executor_did="did:audit:executor", decision="VERIFIED")
        session.commit()
        escrow = session.get(Escrow, task_id)
        # Exact decimals: 500 bps of the 10 reward is 0.5; executor nets 9.5.
        assert (escrow.released_amount, escrow.platform_fee_amount) == ("9.5", "0.5")
        assert (escrow.refunded_amount, escrow.slashed_amount) == ("3", "0")
        # Extended conservation invariant, exact.
        assert (Decimal(escrow.released_amount) + Decimal(escrow.platform_fee_amount)
                + Decimal(escrow.refunded_amount) + Decimal(escrow.slashed_amount)
                == Decimal(escrow.reserved_total) == Decimal("13"))
        assert balance(session, "did:audit:executor") == Decimal("9.5")
        assert balance(session, "agentforge:platform") == Decimal("0.5")
        assert balance(session, "did:audit:payer") == Decimal("990")  # 1000 - 13 + 3 refund
        fee_row = fee_event(session, task_id, "VERIFIED")
        assert fee_row is not None
        assert (fee_row.did, fee_row.amount_delta, fee_row.reason) == ("agentforge:platform", "0.5", "FULL_RELEASE")
        # Deltas sum to zero conservation across the whole ledger (no burn here).
        deltas = [Decimal(row.amount_delta) for row in session.scalars(select(LedgerEvent))]
        assert sum(deltas, Decimal("0")) == 0
        # Audit trail: dedicated PLATFORM_FEE_COLLECTED event with the idempotency key.
        audits = session.scalars(select(AuditEvent).where(
            AuditEvent.kind == "PLATFORM_FEE_COLLECTED", AuditEvent.aggregate_id == task_id,
        )).all()
        assert len(audits) == 1
        assert audits[0].payload["idempotency_key"] == f"task:{task_id}:fee:VERIFIED"
        assert audits[0].payload["platform_fee_amount"] == "0.5"
        # The platform participant is an internal system agent, never active.
        platform = session.get(Agent, "agentforge:platform")
        assert platform is not None and platform.status == "system"


def test_platform_fee_bps_derives_from_released_amount_on_partial(ledger_engine):
    task_id = make_funded(ledger_engine, fee=fee_economics(mode="bps", bps=500))
    with Session(ledger_engine) as session:
        services.escrow_settle(session, task=session.get(Task, task_id),
                               executor_did="did:audit:executor",
                               decision="PARTIAL", settlement={"executor_amount": "4"})
        session.commit()
        escrow = session.get(Escrow, task_id)
        # Fee derives from the 4 actually released: 500 bps -> 0.2, not from the reward.
        assert (escrow.released_amount, escrow.platform_fee_amount) == ("3.8", "0.2")
        assert escrow.refunded_amount == "9"  # (10 - 4) + 2 + 1
        assert balance(session, "did:audit:executor") == Decimal("3.8")
        assert balance(session, "agentforge:platform") == Decimal("0.2")
        assert balance(session, "did:audit:payer") == Decimal("996")  # 1000 - 13 + 9
        assert (Decimal(escrow.released_amount) + Decimal(escrow.platform_fee_amount)
                + Decimal(escrow.refunded_amount) + Decimal(escrow.slashed_amount)
                == Decimal("13"))


def test_platform_fee_fixed_mode_full_release(ledger_engine):
    task_id = make_funded(ledger_engine, fee=fee_economics(mode="fixed", fixed="2"))
    with Session(ledger_engine) as session:
        services.escrow_settle(session, task=session.get(Task, task_id),
                               executor_did="did:audit:executor", decision="VERIFIED")
        session.commit()
        escrow = session.get(Escrow, task_id)
        assert (escrow.released_amount, escrow.platform_fee_amount) == ("8", "2")
        assert balance(session, "did:audit:executor") == 8
        assert balance(session, "agentforge:platform") == 2


def test_platform_fee_fixed_mode_is_capped_at_the_release(ledger_engine):
    # A partial release smaller than the declared fixed fee: the fee is capped
    # at the release, the executor nets zero, and no release credit is posted.
    task_id = make_funded(ledger_engine, fee=fee_economics(mode="fixed", fixed="2"))
    with Session(ledger_engine) as session:
        services.escrow_settle(session, task=session.get(Task, task_id),
                               executor_did="did:audit:executor",
                               decision="PARTIAL", settlement={"executor_amount": "1"})
        session.commit()
        escrow = session.get(Escrow, task_id)
        assert (escrow.released_amount, escrow.platform_fee_amount) == ("0", "1")
        assert balance(session, "did:audit:executor") == 0
        assert balance(session, "agentforge:platform") == 1
        assert balance(session, "did:audit:payer") == 999  # 1000 - 13 + 12 refund
        assert fee_event(session, task_id, "PARTIAL") is not None
        assert session.scalar(select(LedgerEvent).where(
            LedgerEvent.idempotency_key == f"task:{task_id}:release:PARTIAL")) is None


def test_zero_fee_on_refund_never_credits_platform(ledger_engine):
    task_id = make_funded(ledger_engine, fee=fee_economics(mode="bps", bps=500))
    with Session(ledger_engine) as session:
        services.escrow_settle(session, task=session.get(Task, task_id),
                               executor_did="did:audit:executor", decision="REJECTED")
        session.commit()
        escrow = session.get(Escrow, task_id)
        assert escrow.platform_fee_amount == "0" and escrow.refunded_amount == "13"
        assert balance(session, "agentforge:platform") == 0
        assert session.scalar(select(LedgerAccount).where(
            LedgerAccount.did == "agentforge:platform")) is None
        assert fee_event(session, task_id, "REJECTED") is None
        assert session.scalar(select(AuditEvent).where(
            AuditEvent.kind == "PLATFORM_FEE_COLLECTED", AuditEvent.aggregate_id == task_id,
        )) is None


def test_zero_fee_on_slash_never_credits_platform(ledger_engine):
    task_id = make_funded(ledger_engine, fee=fee_economics(mode="bps", bps=500))
    with Session(ledger_engine) as session:
        services.escrow_settle(session, task=session.get(Task, task_id),
                               executor_did="did:audit:executor",
                               decision="SLASHED", settlement={"slash_subject": "requester"})
        session.commit()
        escrow = session.get(Escrow, task_id)
        # Slash punishes; the marketplace must never profit from disputes.
        assert (escrow.released_amount, escrow.platform_fee_amount) == ("0", "0")
        assert escrow.slashed_amount == "2" and escrow.refunded_amount == "11"
        assert balance(session, "agentforge:platform") == 0
        assert fee_event(session, task_id, "SLASHED") is None


@pytest.mark.parametrize("decision,settlement", [("VERIFIED", {}), ("PARTIAL", {"executor_amount": "1e-28"})])
def test_platform_fee_stays_exact_under_hostile_precision(ledger_engine, decision, settlement):
    task_id = make_funded(
        ledger_engine, reward="1.00000000000000000000000000001",
        fee=fee_economics(mode="bps", bps=500, reward="1.00000000000000000000000000001"),
    )
    with Session(ledger_engine) as session, localcontext() as context:
        context.prec = 2  # Application code must not inherit this precision.
        services.escrow_settle(session, task=session.get(Task, task_id), executor_did="did:audit:executor",
                               decision=decision, settlement=settlement)
        session.commit()
        escrow = session.get(Escrow, task_id)
        with localcontext() as reference:
            reference.prec = 200
            released, charged = Decimal(escrow.released_amount), Decimal(escrow.platform_fee_amount)
            assert released + charged + Decimal(escrow.refunded_amount) + Decimal(escrow.slashed_amount) \
                == Decimal(escrow.reserved_total)
            assert balance(session, "did:audit:executor") == released
            assert balance(session, "agentforge:platform") == charged


def test_platform_fee_default_tasks_keep_historical_ledger(ledger_engine):
    task_id = make_funded(ledger_engine)
    with Session(ledger_engine) as session:
        services.escrow_settle(session, task=session.get(Task, task_id),
                               executor_did="did:audit:executor", decision="VERIFIED")
        session.commit()
        escrow = session.get(Escrow, task_id)
        assert (escrow.released_amount, escrow.platform_fee_amount) == ("10", "0")
        assert session.get(Agent, "agentforge:platform") is None
        assert fee_event(session, task_id, "VERIFIED") is None


def prepared_fee_submission(client, economics):
    poster, executor, validator = (AgentIdentity.generate() for _ in range(3))
    register(client, poster)
    register(client, executor)
    register(client, validator, {"name": "validator", "capabilities": ["validation"]})
    settings.trusted_validator_dids = settings.trusted_validator_dids | {validator.did}
    payload = task_payload(economics=economics)
    task = create_task(client, poster, payload)
    assert signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}).status_code == 200
    submission, _ = submit(client, task, executor)
    return poster, executor, validator, task, submission


def test_api_fee_task_settles_with_exact_platform_accounting(client):
    economics = fee_economics(mode="bps", bps=500)
    poster, executor, validator, task, submission = prepared_fee_submission(client, economics)
    body = decision_payload(client, validator, submission, "VERIFIED")
    assert signed_request(client, validator, "POST", f"/api/v1/submissions/{submission['submission_id']}/validate", body).status_code == 200
    final = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert final["status"] == "VERIFIED"
    # The documented escrow view exposes the collected fee.
    assert final["escrow"]["platform_fee_amount"] == "0.5"
    assert final["escrow"]["released_amount"] == "9.5"
    with db.SessionLocal() as session:
        assert balance(session, executor.did) == Decimal("1009.5")  # 1000 faucet + 9.5 net release
        assert balance(session, "agentforge:platform") == Decimal("0.5")
        assert balance(session, poster.did) == Decimal("990")  # faucet 1000 - 13 + 3
        assert session.get(Agent, "agentforge:platform").status == "system"


def test_api_rejects_declared_service_fees_above_the_operator_cap(client, monkeypatch):
    from agentforge_server.admission import validate_security_configuration

    poster = AgentIdentity.generate()
    register(client, poster)

    def create(economics):
        return signed_request(client, poster, "POST", "/api/v1/tasks", task_payload(economics=economics))

    # bps above the 500 default cap.
    response = create(fee_economics(mode="bps", bps=501))
    assert response.status_code == 400, response.text
    assert "operator cap" in response.json()["detail"]
    # fixed fee above 500 bps of the 10 reward (0.5).
    response = create(fee_economics(mode="fixed", fixed="0.51"))
    assert response.status_code == 400
    assert "operator cap" in response.json()["detail"]
    # exactly at the cap is allowed.
    assert create(fee_economics(mode="bps", bps=500)).status_code == 200
    assert create(fee_economics(mode="fixed", fixed="0.5")).status_code == 200
    # mode/value mismatches.
    assert create(fee_economics(mode="none", bps=250)).status_code == 400
    assert create(fee_economics(mode="none", fixed="1")).status_code == 400
    assert create(fee_economics(mode="bps", bps=100, fixed="1")).status_code == 400
    assert create(fee_economics(mode="fixed", fixed="1", bps=100)).status_code == 400
    # REPUTATION tasks never carry a platform fee.
    reputation = task_payload(economics={"mode": "REPUTATION", "service_fee_mode": "bps", "service_fee_bps": 100})
    assert signed_request(client, poster, "POST", "/api/v1/tasks", reputation).status_code == 400
    # No rejected declaration may leave a task or escrow behind.
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Escrow)) == 2

    # The operator cap itself is validated configuration.
    monkeypatch.setattr(settings, "max_service_fee_bps", "50")
    with pytest.raises(ValueError, match="max service fee bps"):
        validate_security_configuration()
    monkeypatch.setattr(settings, "max_service_fee_bps", -1)
    with pytest.raises(ValueError, match="max service fee bps"):
        validate_security_configuration()
    monkeypatch.setattr(settings, "max_service_fee_bps", 10001)
    with pytest.raises(ValueError, match="max service fee bps"):
        validate_security_configuration()
    monkeypatch.setattr(settings, "max_service_fee_bps", 10000)
    validate_security_configuration()
