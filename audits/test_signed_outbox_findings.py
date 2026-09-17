"""Audit probes for 3986dd1; assertions express required, currently broken behavior.

Run explicitly: .venv/bin/python -m pytest -q audits/test_signed_outbox_findings.py
These are intentionally outside the default testpaths; failures are findings,
not an assertion that the existing 52-test suite fails. No external network used.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_signed_event_outbox as helpers

from agentforge_sdk.client import AgentIdentity
from agentforge_server import db
from agentforge_server.event_envelope import EnvelopeError, build_envelope, verify_envelope
from agentforge_server.models import Claim, OutboxEvent, ReputationEvent, Task
from agentforge_server.outbox import drain_once, OUTBOX_MAX_ATTEMPTS
from agentforge_server.publisher import load_event_publisher, reset_event_publisher
from agentforge_server.services import reap_expired_claims
from agentforge_server.settings import settings

client = helpers.client
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
    # Mutation needs no private key. The schema forbids it, runtime does not.
    envelope["server"]["untrusted_extra"] = "added after signing"
    assert not verify_envelope(envelope, publisher.public_key_bytes)


def test_a4_builder_enforces_published_schema(monkeypatch):
    monkeypatch.setattr(settings, "event_publisher_id", "x" * 161)
    publisher = load_event_publisher()
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


def test_a6_schema_guard_rejects_pre_outbox_revision(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'old-schema.db'}"
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
