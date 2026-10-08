"""Dormant settlement machinery against real app-created tasks and real SQL.

No HTTP mocking. The two test-only ports model a submission system and an
independent observer; neither is a rail implementation or testnet evidence.
Reuses the existing isolated SQLite/PostgreSQL fixture without changing it.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import threading

import pytest
from alembic import command as alembic
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from agentforge_sdk import AgentForgeClient, AgentIdentity
from agentforge_server import db, settlement
from agentforge_server.adapters.mock_settlement import MockSettlementProvider
from agentforge_server.app import create_app
from agentforge_server.models import (
    AuditEvent, Base, Escrow, LedgerEvent, SettlementAttempt as Attempt,
    SettlementAttemptEvent as History, SettlementIntent as Intent, Task,
)
from agentforge_server.services import add_audit, now
from agentforge_server.settings import settings
from agentforge_server.settlement_attempts import (
    Action, IntentSpec, Observation, ObservationKind as Kind, State,
    SubmissionReference, VerifiedReceipt, command_from, enqueue_intent,
)
from agentforge_server.settlement_worker import SettlementWorker
from helpers import register, signed_request
from test_outbox_regressions import audit_database, client  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
TARGET = "fixture:domain:escrow-v1"
BODY = {
    "kind": "deterministic", "verification_strategy": "deterministic",
    "input": {}, "acceptance": {"expected_outputs": {"answer": "ready"}},
    "economics": {"mode": "BOUNTY", "reward": {"amount": "10", "asset": "MOCK"}},
}


@pytest.fixture
def market(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    response = signed_request(client, poster, "POST", "/api/v1/tasks", BODY)
    assert response.status_code == 200, response.text
    task = response.json()
    return client, poster, task


@pytest.fixture
def spec(market):
    _, poster, task = market
    return IntentSpec(task["id"], Action.HOLD, TARGET, "MOCK", poster.did, "10", minimum_finality=3)


def enqueue(spec, key="intent-key"):
    with db.SessionLocal() as session, session.begin():
        row = enqueue_intent(session, spec, idempotency_key=key)
        return row.id


def attempts():
    with db.SessionLocal() as session:
        return session.scalars(select(Attempt).order_by(Attempt.created_at, Attempt.number)).all()


def current():
    return attempts()[-1]


def due(*, expire=False):
    with db.SessionLocal() as session, session.begin():
        values = {"next_attempt_at": 0}
        if expire:
            values["lease_expires_at"] = 0
        session.execute(update(Attempt).values(**values))


def history():
    with db.SessionLocal() as session:
        return session.scalars(select(History).order_by(History.created_at, History.id)).all()


class TestSubmitter:
    __test__ = False

    def __init__(self):
        self.calls = []
        self.hook = lambda command: None

    def submit(self, command):
        self.calls.append(command)
        self.hook(command)
        return SubmissionReference("tx:original")


class TestVerifier:
    __test__ = False

    def __init__(self):
        self.calls = []
        self.kind = Kind.FINALIZED
        self.depth = 3
        self.mutate = lambda observation: observation
        self.hook = lambda command: None
        self.replacement_ref = "tx:replacement"

    def inspect(self, command, transaction_ref):
        self.calls.append((command, transaction_ref))
        self.hook(command)
        if self.kind == Kind.UNKNOWN:
            return Observation(Kind.UNKNOWN)
        receipt = VerifiedReceipt(command.intent_id, command.intent_hash, command.spec,
                                  transaction_ref or "tx:original", "block:canonical", self.depth)
        return self.mutate(Observation(self.kind, receipt,
                                      self.replacement_ref if self.kind == Kind.REPLACED else None))


@pytest.fixture
def ports():
    return TestSubmitter(), TestVerifier()


def worker(ports, **kwargs):
    return SettlementWorker(db.SessionLocal, target=TARGET, submitter=ports[0], verifier=ports[1], **kwargs)


def finalize(spec, ports):
    identifier = enqueue(spec)
    assert worker(ports).tick() == 1
    assert current().status == State.SUBMITTED
    due()
    assert worker(ports).tick() == 1
    assert current().status == State.FINALIZED
    return identifier


def test_live_mock_flow_never_enqueues_external_work(market):
    client, _, task = market
    identity = AgentIdentity.generate()
    register(client, identity)
    sdk = AgentForgeClient("http://testserver", identity)
    sdk.http = client
    sdk.claim(task["id"])
    result = sdk.submit(task["id"], result={"answer": "ready"})
    assert result["status"] == "VERIFIED"
    assert sdk.get_task(task["id"])["escrow"]["released_amount"] == "10"
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Intent)) == 0
        assert session.scalar(select(func.count()).select_from(Attempt)) == 0


def test_queue_is_atomic_with_caller_audit_and_rollback(spec, ports):
    with db.SessionLocal() as session:
        enqueue_intent(session, spec, idempotency_key="rollback")
        add_audit(session, actor_did=spec.payer, kind="TEST_INTENT", aggregate_type="task", aggregate_id=spec.task_id)
        # Separate committed reader/worker cannot see an uncommitted command.
        assert worker(ports).tick() == 0
        session.rollback()
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Intent)) == 0
        assert session.scalar(select(func.count()).select_from(Attempt)) == 0
        assert session.scalar(select(func.count()).select_from(History)) == 0
        assert session.scalar(select(AuditEvent).where(AuditEvent.kind == "TEST_INTENT")) is None
    assert ports[0].calls == []


def test_real_request_rolls_back_marketplace_and_intent_together(client, monkeypatch):
    poster = AgentIdentity.generate()
    register(client, poster)

    class Producer(MockSettlementProvider):
        def fund(self, session, task):
            super().fund(session, task)
            enqueue_intent(session, IntentSpec(task.id, Action.HOLD, TARGET, "MOCK", task.poster_did, "10"),
                           idempotency_key="producer-failure")
            raise ValueError("test producer failed after enqueue")

    monkeypatch.setattr(settlement, "_provider", Producer())
    response = signed_request(client, poster, "POST", "/api/v1/tasks", BODY)
    assert response.status_code == 400, response.text
    with db.SessionLocal() as session:
        for model in (Task, Escrow, Intent, Attempt, History):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(LedgerEvent).where(LedgerEvent.reason == "TASK_FUND")) is None


def test_key_replay_and_independent_slot_exclusivity(spec, ports):
    identifier = finalize(spec, ports)
    assert enqueue(replace(spec, reserved="10.000")) == identifier
    assert len(history()) == 5
    with pytest.raises(ValueError, match="idempotency conflict"):
        enqueue(replace(spec, reserved="11"))
    with pytest.raises(ValueError, match="allocation conflict"):
        enqueue(spec, key="new-key-same-hold")
    assert len(attempts()) == 1


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_enqueue_unique_slot(spec, same_key):
    barrier = threading.Barrier(2, timeout=10)

    def run(index):
        barrier.wait()
        try:
            return enqueue(spec, key="shared" if same_key else f"key:{index}")
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    if same_key:
        assert results[0] == results[1] != "conflict"
    else:
        assert results.count("conflict") == 1
    assert len(attempts()) == len(history()) == 1


def test_no_open_session_or_connection_at_either_io_port(spec, ports, market):
    enqueue(spec)
    active = set()

    class TrackedSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            active.add(self)

        def close(self):
            super().close()
            active.remove(self)

    sessions = sessionmaker(bind=db.engine, class_=TrackedSession, expire_on_commit=False)

    def boundary(command):
        assert active == set()
        assert db.engine.pool.checkedout() == 0
        # A real HTTP read AND separate SQL writer succeed during external I/O.
        assert market[0].get(f"/api/v1/tasks/{spec.task_id}").status_code == 200
        with db.SessionLocal() as session, session.begin():
            row = session.get(Intent, command.intent_id)
            assert row is not None
            assert session.scalar(select(Attempt.submission_started_at)) is not None
            session.execute(update(Task).where(Task.id == spec.task_id).values(updated_at=now()))

    ports[0].hook = ports[1].hook = boundary
    w = SettlementWorker(sessions, target=TARGET, submitter=ports[0], verifier=ports[1])
    w.tick()
    due()
    w.tick()
    assert current().status == State.FINALIZED
    assert active == set()


def test_submitter_success_cannot_finalize(spec, ports):
    enqueue(spec)
    worker(ports).tick()
    row = current()
    assert row.status == State.SUBMITTED
    assert row.block_ref is None and row.finality_depth == 0
    assert ports[1].calls == []
    ports[1].kind, ports[1].depth = Kind.CONFIRMED, 1
    due()
    worker(ports).tick()
    assert current().status == State.CONFIRMED
    ports[1].kind, ports[1].depth = Kind.FINALIZED, 3
    due()
    worker(ports).tick()
    assert current().status == State.FINALIZED
    assert len(ports[0].calls) == 1


@pytest.mark.parametrize("phase", ["before_send", "after_send", "record_write"])
def test_crash_recovery_never_resubmits(spec, ports, phase):
    enqueue(spec)
    first = worker(ports)
    work = first._claim(current().id)
    assert work.submit
    if phase != "before_send":
        ports[0].submit(work.command)
    if phase == "record_write":
        # Fail a real journal INSERT: reference/status must roll back together.
        def fail(conn, cursor, statement, params, context, executemany):
            if statement.startswith("INSERT INTO settlement_attempt_events"):
                raise RuntimeError("database write failed")
        event.listen(db.engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(RuntimeError):
                first._record_submission(work, "tx:original")
        finally:
            event.remove(db.engine, "before_cursor_execute", fail)
    due(expire=True)
    ports[1].kind = Kind.UNKNOWN if phase == "before_send" else Kind.FINALIZED
    worker(ports).tick()
    assert current().status == (State.MANUAL_REVIEW if phase == "before_send" else State.FINALIZED)
    assert len(ports[0].calls) == (0 if phase == "before_send" else 1)
    assert ports[1].calls[0][1] is None  # recovery by immutable intent id


def test_timeout_unknown_and_later_independent_recovery(spec, ports):
    enqueue(spec)
    secret = "https://user:password@example.invalid/private?token=secret"

    def timeout(command):
        raise TimeoutError(secret)

    ports[0].hook = timeout
    worker(ports).tick()
    assert current().status == State.MANUAL_REVIEW
    assert current().last_error == "SUBMISSION_UNCERTAIN"
    ports[1].kind = Kind.UNKNOWN
    for _ in range(3):
        due()
        worker(ports).tick()
        assert current().status == State.MANUAL_REVIEW
    assert current().next_attempt_at >= now() + 200
    ports[1].kind = Kind.FINALIZED
    due()
    worker(ports).tick()
    assert current().status == State.FINALIZED
    assert len(ports[0].calls) == 1
    assert secret not in str([e.details for e in history()])


def test_competing_workers_and_expired_owner_are_fenced(spec, ports):
    enqueue(spec)
    entered, release = threading.Event(), threading.Event()

    def pause(command):
        entered.set()
        assert release.wait(10)

    ports[0].hook = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(worker(ports).tick)
        try:
            assert entered.wait(10)
            assert worker(ports).tick() == 0
            due(expire=True)
            assert worker(ports).tick() == 1  # verification, never submission
            assert current().status == State.FINALIZED
        finally:
            release.set()
        assert first.result(timeout=10) == 1
    assert current().status == State.FINALIZED  # late submission ref cannot regress
    assert len(ports[0].calls) == len(ports[1].calls) == 1


def test_expired_lease_cannot_write_even_without_successor(spec, ports):
    enqueue(spec)
    w = worker(ports)
    work = w._claim(current().id)
    due(expire=True)
    w._record_submission(work, "tx:stale")
    assert current().status == State.PENDING
    assert current().transaction_ref is None


@pytest.mark.parametrize("field,value", [
    ("intent_id", "different:intent"), ("intent_hash", "0" * 64),
    ("transaction_ref", "tx:wrong"), ("transaction_ref", "x" * 201),
    ("block_ref", None), ("block_ref", "secret?token=yes"),
    ("finality_depth", 2), ("finality_depth", True), ("finality_depth", -1),
])
def test_untrusted_receipt_fields_cannot_finalize(spec, ports, field, value):
    enqueue(spec)
    w = worker(ports)
    w.tick()
    ports[1].mutate = lambda o: replace(o, receipt=replace(o.receipt, **{field: value}))
    due()
    w.tick()
    assert current().status == State.MANUAL_REVIEW
    assert current().last_error == "VERIFICATION_UNAVAILABLE"
    assert len(ports[0].calls) == 1


@pytest.mark.parametrize("changes", [
    {"target": "another:domain"}, {"asset": "WRONG"}, {"payer": "did:other"},
    {"reserved": "11"}, {"executor": "did:other"}, {"fee_recipient": "did:other"},
    {"task_id": "different-task"}, {"minimum_finality": 1},
    {"action": Action.REFUND, "refunded": "10"},
])
def test_receipt_must_bind_every_economic_commitment(spec, ports, changes):
    enqueue(spec)
    w = worker(ports)
    w.tick()
    ports[1].mutate = lambda o: replace(o, receipt=replace(o.receipt, spec=replace(spec, **changes)))
    due()
    w.tick()
    assert current().status == State.MANUAL_REVIEW


@pytest.mark.parametrize("bad", [
    {"success": True}, SubmissionReference("tx:original"), Observation(Kind.FINALIZED),
    Observation("FINALIZED"),
])
def test_verifier_does_not_accept_submitter_claims(spec, ports, bad):
    enqueue(spec)
    worker(ports).tick()
    ports[1].mutate = lambda o: bad
    due()
    worker(ports).tick()
    assert current().status == State.MANUAL_REVIEW


def test_replacement_is_verified_successor_not_a_second_send(spec, ports):
    enqueue(spec)
    w = worker(ports)
    w.tick()
    ports[1].kind = Kind.REPLACED
    due()
    w.tick()
    rows = attempts()
    assert [r.status for r in rows] == [State.REPLACED, State.SUBMITTED]
    assert [r.number for r in rows] == [1, 2]
    assert len({r.intent_id for r in rows}) == 1
    assert rows[1].transaction_ref == "tx:replacement"
    ports[1].kind = Kind.FINALIZED
    due()
    w.tick()
    assert current().status == State.FINALIZED
    assert len(ports[0].calls) == 1


def test_replacement_cycle_is_manual_review(spec, ports):
    enqueue(spec)
    w = worker(ports)
    w.tick()
    ports[1].kind = Kind.REPLACED
    due()
    w.tick()
    ports[1].replacement_ref = "tx:original"
    due()
    w.tick()
    assert current().status == State.MANUAL_REVIEW
    assert current().last_error == "REPLACEMENT_REJECTED"
    assert len(attempts()) == 2


@pytest.mark.parametrize("observation", [Kind.REORGED, Kind.CONFIRMED, Kind.SUBMITTED])
def test_finalized_attempts_remain_observed_for_reorgs(spec, ports, observation):
    finalize(spec, ports)
    ports[1].kind = observation
    due()
    worker(ports).tick()
    assert current().status == State.REORGED
    ports[1].kind = Kind.FINALIZED
    due()
    worker(ports).tick()
    assert current().status == State.FINALIZED
    assert len(ports[0].calls) == 1
    assert any(e.to_status == State.REORGED for e in history())


def test_definitive_failure_never_creates_retry_action(spec, ports):
    enqueue(spec)
    w = worker(ports)
    w.tick()
    ports[1].kind = Kind.FAILED
    due()
    w.tick()
    assert current().status == State.FAILED
    due()
    assert w.tick() == 0
    with pytest.raises(ValueError):
        enqueue(spec, key="retry-with-new-key")
    assert len(ports[0].calls) == 1


@pytest.mark.parametrize("action,allocation", [
    (Action.RELEASE, {"released": "9.5", "fee": "0.5"}),
    (Action.RELEASE, {"released": "4", "fee": "0.5", "refunded": "5.5"}),
    (Action.REFUND, {"refunded": "10"}),
    (Action.SLASH, {"slashed": "2", "refunded": "8"}),
])
def test_terminal_allocations_and_exclusivity(spec, ports, action, allocation):
    finalize(spec, ports)
    terminal = replace(spec, action=action, executor="did:executor", fee_recipient="internal:fees", **allocation)
    terminal_id = enqueue(terminal, key="terminal")
    assert enqueue(terminal, key="terminal") == terminal_id
    with pytest.raises(ValueError):
        enqueue(replace(spec, action=Action.REFUND, refunded="10"), key="competing-terminal")
    worker(ports).tick()
    due()
    worker(ports).tick()
    assert current().status == State.FINALIZED
    # Attempt observation never credits the existing mock ledger or changes task state.
    with db.SessionLocal() as session:
        assert session.get(Escrow, spec.task_id).status == "FUNDED"
        assert session.get(Task, spec.task_id).status == "FUNDED"


def test_terminal_requires_matching_finalized_hold_and_rechecks_before_send(spec, ports):
    terminal = replace(spec, action=Action.REFUND, refunded="10")
    with pytest.raises(ValueError, match="finalized hold"):
        enqueue(terminal)
    finalize(spec, ports)
    with pytest.raises(ValueError, match="finalized hold"):
        enqueue(replace(terminal, target="wrong:target"), key="wrong-target")
    enqueue(terminal, key="terminal")
    # Read-only reconciliation observes hold reorg before the terminal dispatch.
    with db.SessionLocal() as session, session.begin():
        row = session.scalar(select(Attempt).join(Intent).where(Intent.slot == "hold"))
        row.status = State.REORGED
    worker(ports).tick()
    assert current().status == State.MANUAL_REVIEW
    assert current().last_error == "HOLD_NOT_FINALIZED"
    assert len(ports[0].calls) == 1


@pytest.mark.parametrize("changes", [
    {"reserved": "0"}, {"reserved": "-1"}, {"reserved": "NaN"}, {"reserved": 10.0},
    {"action": Action.HOLD, "released": "1", "executor": "did:x"},
    {"action": Action.RELEASE, "released": "11", "executor": "did:x"},
    {"action": Action.RELEASE, "released": "10"},
    {"action": Action.REFUND, "refunded": "9", "fee": "1", "fee_recipient": "did:fee"},
    {"action": Action.SLASH, "slashed": "9", "fee": "1", "fee_recipient": "did:fee"},
    {"minimum_finality": 0}, {"minimum_finality": True}, {"version": 2},
])
def test_invalid_intents_fail_before_storage(spec, changes):
    with pytest.raises(ValueError):
        enqueue(replace(spec, **changes))
    assert attempts() == []


def test_corrupt_intent_fails_closed_before_io(spec, ports):
    enqueue(spec)
    with db.SessionLocal() as session, session.begin():
        session.execute(update(Intent).values(intent_hash="0" * 64))
    worker(ports).tick()
    assert current().status == State.MANUAL_REVIEW
    assert current().last_error == "INTENT_CORRUPT"
    assert ports[0].calls == ports[1].calls == []


def test_worker_is_target_scoped_and_separate_ports_required(spec, ports):
    enqueue(spec)
    w = SettlementWorker(db.SessionLocal, target="other:domain", submitter=ports[0], verifier=ports[1])
    assert w.tick() == 0
    assert current().submission_started_at is None
    with pytest.raises(ValueError, match="separate ports"):
        SettlementWorker(db.SessionLocal, target=TARGET, submitter=ports[0], verifier=ports[0])
    assert settlement.get_settlement_provider().__class__ is MockSettlementProvider


def test_additive_migration_round_trip_preserves_live_marketplace(audit_database, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", audit_database)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", False)
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    cfg = Config(str(ROOT / "alembic.ini"))
    alembic.upgrade(cfg, "head")
    db.configure_database(audit_database)
    db.verify_schema(require_migrations=True)
    with TestClient(create_app()) as api:
        poster = AgentIdentity.generate()
        register(api, poster)
        response = signed_request(api, poster, "POST", "/api/v1/tasks", BODY)
        assert response.status_code == 200
        task_id = response.json()["id"]
    enqueue(IntentSpec(task_id, Action.HOLD, TARGET, "MOCK", poster.did, "10"))
    # Schema parity includes indexes, FKs and defaults, not only table presence.
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    with db.engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    alembic.downgrade(cfg, "b0c9d8e7f6a5")
    assert "settlement_intents" not in inspect(db.engine).get_table_names()
    with pytest.raises(RuntimeError, match="schema is not ready"):
        db.verify_schema(require_migrations=True)
    with db.SessionLocal() as session:
        assert session.get(Task, task_id).status == "FUNDED"
        assert session.get(Escrow, task_id).reserved_total == "10"
    alembic.upgrade(cfg, "head")
    db.verify_schema(require_migrations=True)
    assert attempts() == []  # downgrade is explicitly destructive to new evidence
    with db.SessionLocal() as session:
        assert session.get(Task, task_id).status == "FUNDED"


def test_concurrent_terminal_decisions_have_one_winner(spec, ports):
    finalize(spec, ports)
    barrier = threading.Barrier(2, timeout=10)
    terminal_specs = [replace(spec, action=Action.REFUND, refunded="10"),
                      replace(spec, action=Action.RELEASE, executor="did:executor", released="10")]

    def run(index):
        barrier.wait()
        try:
            return enqueue(terminal_specs[index], key=f"terminal:{index}")
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    assert results.count("conflict") == 1
    assert len(attempts()) == 2  # hold plus exactly one terminal allocation


def test_two_workers_racing_the_same_pending_row_dispatch_once(spec, ports):
    enqueue(spec)
    identifier = current().id
    barrier = threading.Barrier(2, timeout=10)

    def claim():
        barrier.wait()
        return worker(ports)._claim(identifier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(work is not None for work in results) == 1
    work = next(w for w in results if w is not None)
    worker(ports)._record_submission(work, ports[0].submit(work.command).transaction_ref)
    assert len(ports[0].calls) == 1
    assert current().status == State.SUBMITTED


def test_expired_verifier_cannot_overwrite_successor_reconciliation(spec, ports):
    enqueue(spec)
    w = worker(ports)
    w.tick()
    due()
    old = w._claim(current().id)
    old_observation = ports[1].inspect(old.command, old.transaction_ref)
    due(expire=True)
    ports[1].kind = Kind.REORGED
    assert worker(ports).tick() == 1
    w._record_observation(old, old_observation)
    assert current().status == State.REORGED


def test_verifier_errors_are_sanitized_and_do_not_resubmit(spec, ports):
    enqueue(spec)
    w = worker(ports)
    w.tick()

    def broken(command):
        raise RuntimeError("secret key in transport failure")

    ports[1].hook = broken
    due()
    w.tick()
    assert current().last_error == "VERIFICATION_UNAVAILABLE"
    assert "secret key" not in str([e.details for e in history()])
    assert len(ports[0].calls) == 1


def test_definitive_failure_requires_finality_evidence(spec, ports):
    enqueue(spec)
    worker(ports).tick()
    ports[1].kind, ports[1].depth = Kind.FAILED, 1
    due()
    worker(ports).tick()
    assert current().status == State.MANUAL_REVIEW
    ports[1].depth = 3
    due()
    worker(ports).tick()
    assert current().status == State.FAILED


def test_partial_execution_or_wrong_fee_cannot_finalize_terminal_intent(spec, ports):
    finalize(spec, ports)
    terminal = replace(spec, action=Action.RELEASE, executor="did:executor", fee_recipient="did:fee",
                       released="9", fee="1")
    enqueue(terminal, key="terminal")
    worker(ports).tick()
    ports[1].mutate = lambda o: replace(o, receipt=replace(o.receipt, spec=replace(terminal, released="8", fee="2")))
    due()
    worker(ports).tick()
    assert current().status == State.MANUAL_REVIEW


def test_database_backstops_active_attempt_and_valid_states(spec):
    from sqlalchemy.exc import IntegrityError
    identifier = enqueue(spec)
    with db.SessionLocal() as session:
        session.add(Attempt(id="second", intent_id=identifier, number=2, status=State.PENDING,
                            next_attempt_at=now(), verification_attempts=0, finality_depth=0,
                            created_at=now(), updated_at=now()))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(update(Attempt).values(status="UNRECOGNIZED"))
            session.commit()
        session.rollback()
    assert len(attempts()) == 1


def test_wrong_payer_or_missing_task_cannot_enqueue(spec):
    with pytest.raises(ValueError, match="task and its payer"):
        enqueue(replace(spec, payer="did:wrong"))
    with pytest.raises(ValueError, match="task and its payer"):
        enqueue(replace(spec, task_id="nonexistent"))
    assert attempts() == []


def test_successful_live_producer_commits_intent_with_task_not_during_rpc(client, monkeypatch, ports):
    poster = AgentIdentity.generate()
    register(client, poster)

    class Producer(MockSettlementProvider):
        # Test-only producer: this is NOT registered as an external provider.
        # Production mock never creates an intent. Verify the real route's
        # transaction ownership without replacing the HTTP path or signing.
        def fund(self, session, task):
            escrow = super().fund(session, task)
            enqueue_intent(session, IntentSpec(task.id, Action.HOLD, TARGET, "MOCK", task.poster_did, "10"),
                           idempotency_key=f"task:{task.id}:hold")
            assert ports[0].calls == []
            return escrow

    monkeypatch.setattr(settlement, "_provider", Producer())
    response = signed_request(client, poster, "POST", "/api/v1/tasks", BODY)
    assert response.status_code == 200, response.text
    assert current().status == State.PENDING
    assert ports[0].calls == []
    worker(ports).tick()
    assert current().status == State.SUBMITTED
    assert ports[0].calls[0].spec.task_id == response.json()["id"]


def test_completion_rechecks_time_after_acquiring_sql_lock(spec, ports, monkeypatch):
    import agentforge_server.settlement_worker as module
    enqueue(spec)
    w = worker(ports)
    work = w._claim(current().id)
    expiry = current().lease_expires_at
    times = iter([expiry - 1, expiry + 1])
    # Predicate was evaluated before expiry; the locked row is read after it.
    monkeypatch.setattr(module, "now", lambda: next(times))
    w._record_submission(work, "tx:too-late")
    assert current().transaction_ref is None
    assert current().status == State.PENDING
