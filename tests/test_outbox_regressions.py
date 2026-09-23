"""Regression coverage for the six findings audited at 3986dd1."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import replace
import os
import uuid
import threading
import time

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
import test_signed_event_outbox as helpers

from agentforge_sdk.client import AgentIdentity
from agentforge_server import db
from agentforge_server.event_envelope import (
    ENVELOPE_VERSION, LEGACY_ENVELOPE_VERSION, EnvelopeError, build_envelope,
    verify_envelope, verify_actor_causation, validate_envelope, signing_bytes,
)
from agentforge_server.models import Claim, OutboxEvent, ReputationEvent, Task
from agentforge_server.outbox import drain_once, OUTBOX_MAX_ATTEMPTS
from agentforge_server.publisher import load_event_publisher, reset_event_publisher
from agentforge_server.services import guard_active_claim, reap_expired_claims
from agentforge_server.settings import settings

@pytest.fixture
def audit_database(tmp_path, monkeypatch):
    """Isolate PostgreSQL tests in a random schema, never drop a shared database."""
    postgres_url = os.getenv("AGENTFORGE_TEST_POSTGRES_URL")
    if not postgres_url:
        yield f"sqlite:///{tmp_path / 'audit.db'}"
        return
    admin = create_engine(postgres_url)
    schema = "agentforge_test_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(postgres_url).update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        db.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def client(audit_database, monkeypatch):
    from agentforge_server.app import create_app
    monkeypatch.setenv("AGENTFORGE_ENV", "development")
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(audit_database)
    with TestClient(create_app()) as test_client:
        yield test_client

reset_publisher = helpers.reset_publisher


def create_task(client):
    poster = AgentIdentity.generate()
    helpers.register(client, poster)
    response, sent = helpers.signed_request(client, poster, "POST", "/api/v1/tasks", helpers.task_body())
    assert response.status_code == 200, response.text
    return response.json(), sent


def test_a1_causation_preserves_exact_signed_timestamp(client):
    _, sent = create_task(client)
    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        envelope = build_envelope(event, publisher=load_event_publisher())
    # The signature includes the exact header string, not the server event time.
    assert envelope["causation"].get("request_timestamp") == sent["timestamp"]
    assert verify_actor_causation(envelope)  # No saved request/body needed.


def test_a2_configuration_error_does_not_dead_letter_events(client, monkeypatch):
    create_task(client)
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "event_signing_key", "")
    reset_event_publisher()
    adapter = helpers.RecordingAdapter()
    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent))
        for _ in range(OUTBOX_MAX_ATTEMPTS):
            event.next_attempt_at = 0
            session.commit()
            try:
                drain_once(session, adapter)
            except RuntimeError:
                break  # Desired fail-fast configuration error.
        session.refresh(event)
        assert not adapter.published
        assert (event.status, event.attempts) == ("PENDING", 0), event.last_error


def test_a3_unsigned_server_extension_is_rejected():
    publisher = load_event_publisher()
    envelope = build_envelope(helpers.make_event(), publisher=publisher)
    assert verify_envelope(envelope, publisher.public_key_bytes)
    # Mutation needs no private key. Both schema and runtime must forbid it.
    envelope["server"]["untrusted_extra"] = "added after signing"
    assert not verify_envelope(envelope, publisher.public_key_bytes)


def test_a4_builder_enforces_published_schema(monkeypatch):
    monkeypatch.setattr(settings, "event_publisher_id", "valid-publisher")
    publisher = replace(load_event_publisher(), publisher_id="x" * 161)
    with pytest.raises(EnvelopeError):
        build_envelope(helpers.make_event(), publisher=publisher)


def test_a5_two_reapers_penalize_an_expired_claim_only_once(client):
    task, _ = create_task(client)
    executor = AgentIdentity.generate()
    helpers.register(client, executor)
    response, _ = helpers.signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    assert response.status_code == 200, response.text
    with db.SessionLocal() as session:
        claim = session.scalar(select(Claim).where(Claim.task_id == task["id"]))
        claim.lease_expires_at = time.time() - 1
        session.commit()

    # Synchronize real sessions after both have read the same Task/Claim state,
    # before either updates it. No production behavior is mocked or replaced.
    barrier = threading.Barrier(2, timeout=10)

    class SynchronizedSession(Session):
        def get(self, entity, ident, **kwargs):
            result = super().get(entity, ident, **kwargs)
            if entity is Task:
                barrier.wait()
            return result

    def reap():
        with SynchronizedSession(bind=db.engine, autoflush=False, expire_on_commit=False) as session:
            return reap_expired_claims(session)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reap) for _ in range(2)]
        counts = [future.result(timeout=20) for future in futures]
    with db.SessionLocal() as session:
        penalties = session.scalar(select(func.count()).select_from(ReputationEvent).where(
            ReputationEvent.did == executor.did, ReputationEvent.kind == "claim_timeout"))
        events = session.scalar(select(func.count()).select_from(OutboxEvent).where(
            OutboxEvent.aggregate_id == task["id"], OutboxEvent.kind == "CLAIM_EXPIRED"))
    assert (sum(counts), penalties, events) == (1, 1, 1)


def test_a6_schema_guard_rejects_pre_outbox_revision(audit_database, monkeypatch):
    url = audit_database
    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "3293de03bb66")
    db.configure_database(url)
    with pytest.raises(RuntimeError, match="schema|migration|alembic"):
        db.verify_schema()


def test_a7_retry_budget_uses_fresh_database_attempts(client, monkeypatch):
    create_task(client)
    import agentforge_server.outbox as outbox
    clock = [time.time() + 1]
    monkeypatch.setattr(outbox, "now", lambda: clock[0])
    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent))
        event.attempts = OUTBOX_MAX_ATTEMPTS - 2
        session.commit()

    class InterleavedSession(Session):
        def scalars(self, statement, *args, **kwargs):
            result = super().scalars(statement, *args, **kwargs)

            class PausedCandidates:
                def all(self):
                    candidates = result.all()
                    # B fails after A's candidate read, before A constructs its claim.
                    with db.SessionLocal() as other:
                        assert drain_once(other, helpers.RecordingAdapter(result=False)) == 0
                    clock[0] += 2000  # B's retry becomes eligible while A was paused.
                    return candidates

            return PausedCandidates()

    with InterleavedSession(bind=db.engine, autoflush=False, expire_on_commit=False) as session:
        drain_once(session, helpers.RecordingAdapter(result=False))
    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent))
        assert (event.attempts, event.status) == (OUTBOX_MAX_ATTEMPTS, "DEAD")


@pytest.mark.parametrize("field,value", [
    ("request_timestamp", "0"), ("method", "DELETE"), ("path", "/other"),
    ("request_id", "other"), ("request_body_hash", "sha256:" + "0" * 64),
])
def test_actor_signature_rejects_tampered_causation(client, field, value):
    create_task(client)
    publisher = load_event_publisher()
    with db.SessionLocal() as session:
        envelope = build_envelope(session.scalar(select(OutboxEvent)), publisher=publisher)
    assert verify_actor_causation(envelope)
    envelope["causation"][field] = value
    # Even a publisher that signs the changed record cannot forge the actor.
    envelope["server"]["signature"] = publisher.sign(signing_bytes(envelope))
    assert verify_envelope(envelope, publisher.public_key_bytes)
    assert not verify_actor_causation(envelope)


def test_timestamp_header_is_preserved_without_numeric_normalization(client):
    from agentforge_server.crypto import canonical_json, request_signing_bytes
    poster = AgentIdentity.generate()
    helpers.register(client, poster)
    timestamp = f"{time.time():.12e}"
    body = canonical_json(helpers.task_body()).encode()
    path = "/api/v1/tasks"
    nonce = uuid.uuid4().hex
    response = client.post(path, content=body, headers={
        "Content-Type": "application/json", "X-Agent-DID": poster.did,
        "X-Agent-Timestamp": timestamp, "X-Agent-Nonce": nonce,
        "X-Agent-Signature": poster.sign(request_signing_bytes("POST", path, body, timestamp, nonce)),
        "Idempotency-Key": uuid.uuid4().hex,
    })
    assert response.status_code == 200, response.text
    with db.SessionLocal() as session:
        envelope = build_envelope(session.scalar(select(OutboxEvent)), publisher=load_event_publisher())
    assert envelope["causation"]["request_timestamp"] == timestamp
    assert verify_actor_causation(envelope)


def test_legacy_attribution_remains_v1_without_fabricated_timestamp():
    cause = {"request_id": "legacy", "request_signature": "sig",
             "request_body_hash": "sha256:" + "a" * 64}
    publisher = load_event_publisher()
    envelope = build_envelope(helpers.make_event(causation=cause), publisher=publisher)
    assert envelope["version"] == LEGACY_ENVELOPE_VERSION
    assert envelope["causation"] == cause
    assert verify_envelope(envelope, publisher.public_key_bytes)
    assert not verify_actor_causation(envelope)
    # Merely relabeling old data as v2 cannot pass the v2 schema.
    envelope["version"] = ENVELOPE_VERSION
    with pytest.raises(EnvelopeError):
        validate_envelope(envelope)


@pytest.mark.parametrize("version", [LEGACY_ENVELOPE_VERSION, ENVELOPE_VERSION])
@pytest.mark.parametrize("mutation", [
    lambda e: e["server"].update(extra="secret"),
    lambda e: e.update(extra="secret"),
    lambda e: e.update(event_id=123),
    lambda e: e.update(payload_hash="invalid"),
    lambda e: e["aggregate"].update(extra=True),
    lambda e: e["attributes"].update(bad={"nested": True}),
    lambda e: e["attributes"].update(bad=float("nan")),
])
def test_schema_and_runtime_reject_invalid_envelopes(version, mutation):
    publisher = load_event_publisher()
    envelope = build_envelope(helpers.make_event(), publisher=publisher)
    envelope["version"] = version
    mutation(envelope)
    with pytest.raises(EnvelopeError) as error:
        validate_envelope(envelope)
    assert "secret" not in str(error.value)
    assert not verify_envelope(envelope, publisher.public_key_bytes)


@pytest.mark.parametrize("environment,secret", [("production", ""), ("development", "bad-seed")])
def test_worker_configuration_fails_before_ticks(client, monkeypatch, environment, secret):
    from agentforge_server.worker import run_worker, run_once
    from agentforge_server.adapters.technocore import TechnocoreAdapter
    create_task(client)
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "event_signing_key", secret)
    adapter = TechnocoreAdapter("https://example.invalid", gossip_enabled=True, publish_path="/events")
    with pytest.raises(RuntimeError):
        run_worker(adapter=adapter, once=True)
    with pytest.raises(RuntimeError):
        run_once(adapter)
    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent))
        assert (event.status, event.attempts, event.lease_owner) == ("PENDING", 0, None)


def test_disabled_worker_does_not_need_publisher(client, monkeypatch):
    from agentforge_server.worker import run_once
    from agentforge_server.adapters.technocore import TechnocoreAdapter
    monkeypatch.setattr(settings, "event_signing_key", "bad-seed")
    assert run_once(TechnocoreAdapter("https://example.invalid"))["delivered"] == 0


@pytest.mark.parametrize("publisher_id", ["x" * 161, "bad\nheader", "non-ascii-\u00e9"])
def test_invalid_publisher_identity_is_configuration_error(monkeypatch, publisher_id):
    monkeypatch.setattr(settings, "event_publisher_id", publisher_id)
    with pytest.raises(RuntimeError, match="PUBLISHER_ID"):
        load_event_publisher()


def test_worker_stops_between_deliveries(client):
    from agentforge_server.worker import run_worker
    create_task(client)
    create_task(client)
    stop = threading.Event()

    class StopDuringPublish(helpers.RecordingAdapter):
        def status(self):
            return {"adapter": "test"}

        def publish_event(self, envelope):
            stop.set()  # Same event the signal handler sets during delivery.
            return super().publish_event(envelope)

    adapter = StopDuringPublish()
    assert run_worker(stop, adapter=adapter) == 1
    with db.SessionLocal() as session:
        events = session.scalars(select(OutboxEvent)).all()
        assert sorted((e.status, e.attempts) for e in events) == [("DELIVERED", 1), ("PENDING", 0)]
    assert len(adapter.published) == 1


def test_lost_delivery_lease_is_not_counted(client):
    create_task(client)

    class LeaseLost(helpers.RecordingAdapter):
        def publish_event(self, envelope):
            with db.SessionLocal() as other:
                other.execute(update(OutboxEvent).where(OutboxEvent.id == envelope["event_id"])
                              .values(lease_owner="other-worker"))
                other.commit()
            return True

    with db.SessionLocal() as session:
        assert drain_once(session, LeaseLost()) == 0
        session.expire_all()
        event = session.scalar(select(OutboxEvent))
        assert (event.status, event.lease_owner, event.delivered_at) == ("PROCESSING", "other-worker", None)


def migration_config(url, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_additive_migration_preserves_existing_events(audit_database, monkeypatch):
    config = migration_config(audit_database, monkeypatch)
    command.upgrade(config, "3293de03bb66")
    db.configure_database(audit_database)
    with db.engine.begin() as connection:
        connection.execute(text("INSERT INTO outbox_events (id, kind, aggregate_id, payload, status, attempts, "
                                "next_attempt_at, created_at) VALUES ('OUT_old', 'TASK_CREATED', 'T_old', "
                                "'{}', 'PENDING', 3, 0, 1)"))
    with pytest.raises(RuntimeError, match="outbox_events"):
        db.init_db()  # create_all must not pretend to migrate old columns.
    command.upgrade(config, "head")
    db.verify_schema(require_migrations=True)
    with db.SessionLocal() as session:
        event = session.get(OutboxEvent, "OUT_old")
        assert (event.attempts, event.actor_did, event.causation) == (3, None, None)
    command.downgrade(config, "3293de03bb66")
    command.upgrade(config, "head")
    with db.SessionLocal() as session:
        assert session.get(OutboxEvent, "OUT_old").attempts == 3
    # Also exercise the full chain on a disposable schema.
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    db.verify_schema(require_migrations=True)


def test_production_guard_requires_versioned_schema(client):
    db.verify_schema()  # create_all is allowed in development.
    with pytest.raises(RuntimeError, match="migration revision"):
        db.verify_schema(require_migrations=True)


def claimed_task(client):
    task, _ = create_task(client)
    executor = AgentIdentity.generate()
    helpers.register(client, executor)
    response, _ = helpers.signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    assert response.status_code == 200, response.text
    return task, executor, response.json()["claim_id"]


@pytest.mark.parametrize("mutation", ["heartbeat", "submission"])
def test_stale_reaper_cannot_undo_an_active_claim_mutation(client, monkeypatch, mutation):
    import agentforge_server.services as services
    task, executor, claim_id = claimed_task(client)
    clock = [time.time()]
    monkeypatch.setattr(services, "now", lambda: clock[0])
    with db.SessionLocal() as session:
        claim = session.get(Claim, claim_id)
        claim.lease_expires_at = clock[0] + 1
        session.commit()
    read = threading.Event()
    resume = threading.Event()

    class PausedReaper(Session):
        def get(self, entity, ident, **kwargs):
            result = super().get(entity, ident, **kwargs)
            if entity is Task:
                read.set()
                assert resume.wait(10)
            return result

    def reap():
        with PausedReaper(bind=db.engine, autoflush=False, expire_on_commit=False) as session:
            return reap_expired_claims(session)

    with db.SessionLocal() as active, ThreadPoolExecutor(max_workers=1) as pool:
        claim = active.get(Claim, claim_id)
        assert guard_active_claim(active, claim)  # Won before expiry; lock held.
        clock[0] += 2
        future = pool.submit(reap)  # Reads the old committed lease as expired.
        try:
            assert read.wait(10)
            if mutation == "heartbeat":
                claim.lease_expires_at = clock[0] + 900
            else:
                claim.status = "SUBMITTED"
                active.get(Task, task["id"]).status = "SUBMITTED"
            active.commit()
        finally:
            resume.set()
        assert future.result(timeout=15) == 0
    with db.SessionLocal() as session:
        assert session.get(Claim, claim_id).status == ("ACTIVE" if mutation == "heartbeat" else "SUBMITTED")
        assert session.scalar(select(func.count()).select_from(ReputationEvent).where(
            ReputationEvent.did == executor.did, ReputationEvent.kind == "claim_timeout")) == 0


def test_reaper_winner_rejects_stale_active_claim_user(client):
    task, _, claim_id = claimed_task(client)
    with db.SessionLocal() as old:
        claim = old.get(Claim, claim_id)  # Cache the ACTIVE state.
        with db.SessionLocal() as other:
            other.execute(update(Claim).where(Claim.id == claim_id).values(lease_expires_at=0))
            other.commit()
            assert reap_expired_claims(other) == 1
        assert not guard_active_claim(old, claim)
        assert claim.status == "EXPIRED"


@pytest.mark.parametrize("operation", ["heartbeat", "inference", "submission"])
def test_api_mutations_use_the_atomic_claim_guard(client, monkeypatch, operation):
    import importlib
    app_module = importlib.import_module("agentforge_server.app")
    import test_audit_fixes as flow
    task, executor, claim_id = claimed_task(client)
    calls = []

    def lose_guard(session, claim):
        calls.append(claim.id)
        return False

    monkeypatch.setattr(app_module, "guard_active_claim", lose_guard)
    path = f"/api/v1/tasks/{task['id']}/inference"
    body = {"provider": "mock", "model_ref": "mock:v1", "input_data": {}}
    if operation == "heartbeat":
        path, body = f"/api/v1/claims/{claim_id}/heartbeat", {}
    elif operation == "submission":
        path = f"/api/v1/tasks/{task['id']}/submissions"
        body = flow.submit_payload(client, task, executor)
    response, _ = helpers.signed_request(client, executor, "POST", path, body)
    assert response.status_code == 409, response.text
    assert calls == [claim_id]
    with db.SessionLocal() as session:
        assert session.get(Task, task["id"]).status == "CLAIMED"


def test_reaper_rolls_back_transition_when_side_effect_fails(client, monkeypatch):
    import agentforge_server.services as services
    task, executor, claim_id = claimed_task(client)
    with db.SessionLocal() as session:
        session.get(Claim, claim_id).lease_expires_at = 0
        session.commit()

    def fail_outbox(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(services, "queue_outbox", fail_outbox)
    with pytest.raises(RuntimeError), db.SessionLocal() as session:
        reap_expired_claims(session)
    with db.SessionLocal() as session:
        assert session.get(Claim, claim_id).status == "ACTIVE"
        assert session.get(Task, task["id"]).status == "CLAIMED"
        assert session.scalar(select(func.count()).select_from(ReputationEvent).where(
            ReputationEvent.did == executor.did, ReputationEvent.kind == "claim_timeout")) == 0


def test_reaper_commits_expired_orphan_without_task_side_effects(client):
    task, executor, claim_id = claimed_task(client)
    with db.SessionLocal() as session:
        session.get(Claim, claim_id).lease_expires_at = 0
        session.get(Task, task["id"]).claim_id = None
        session.commit()
        assert reap_expired_claims(session) == 0
    with db.SessionLocal() as session:
        assert session.get(Claim, claim_id).status == "EXPIRED"
        assert session.scalar(select(func.count()).select_from(ReputationEvent).where(
            ReputationEvent.did == executor.did, ReputationEvent.kind == "claim_timeout")) == 0


def test_proof_event_commits_to_the_stored_proof(client):
    import test_audit_fixes as flow
    task, executor, _ = claimed_task(client)
    submission, _ = flow.submit(client, task, executor)
    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "PROOF_SUBMITTED"))
        assert event.payload["proof_hash"] == submission["proof_hash"]
        envelope = build_envelope(event, publisher=load_event_publisher())
        assert envelope["attributes"]["proof_hash"] == submission["proof_hash"]
        assert "proof" not in event.payload
