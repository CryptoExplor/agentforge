"""Signed dual-attribution event outbox - PR #3.

Covers the canonical event envelope, actor versus server attribution, causation
from verified requests, redaction of private payloads, feature-flagged transport,
idempotent publication, retry/dead-letter telemetry, key rotation and
versioning, malformed envelopes, and worker lifecycle.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy import select

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, registration_bytes, request_bytes
from agentforge_server import db
from agentforge_server.adapters.technocore import TechnocoreAdapter
from agentforge_server.app import create_app
from agentforge_server.crypto import request_signing_bytes, sha256_bytes, verify_signature
from agentforge_server.event_envelope import (
    ENVELOPE_VERSION,
    EnvelopeError,
    build_envelope,
    hash_payload,
    signing_bytes,
    validate_envelope,
    verify_envelope,
)
from agentforge_server.models import OutboxEvent
from agentforge_server.outbox import (
    OUTBOX_MAX_ATTEMPTS,
    drain_once,
    outbox_metrics,
)
from agentforge_server.publisher import (
    EventPublisher,
    get_event_publisher,
    load_event_publisher,
    reset_event_publisher,
)
from agentforge_server.services import new_id, queue_outbox
from agentforge_server.settings import settings
from agentforge_server.worker import run_once, run_worker

REPO_ROOT = Path(__file__).resolve().parents[1]
ENVELOPE_SCHEMA = json.loads(
    (REPO_ROOT / "protocol" / "v1" / "event-envelope.schema.json").read_text()
)
SCHEMA_VALIDATOR = Draft202012Validator(ENVELOPE_SCHEMA)


def signed_request(
    client: TestClient,
    identity: AgentIdentity,
    method: str,
    path: str,
    payload: dict,
    *,
    key: str | None = None,
    nonce: str | None = None,
):
    body = canonical_json(payload).encode()
    timestamp = str(int(time.time()))
    nonce = nonce or uuid.uuid4().hex
    path_only = path.split("?", 1)[0]
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(request_bytes(method, path_only, body, timestamp, nonce)),
        "Idempotency-Key": key or f"idem-{uuid.uuid4().hex}",
    }
    response = client.request(method, path, content=body, headers=headers)
    return response, {"body": body, "timestamp": timestamp, "nonce": nonce, "path": path_only, "method": method}


def register(client: TestClient, identity: AgentIdentity):
    manifest = {"name": "agent", "capabilities": [], "chains": ["base"]}
    challenge = client.get("/api/v1/register/challenge").json()
    payload = {
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
        "did": identity.did,
        "manifest": manifest,
    }
    payload["signature"] = identity.sign(
        registration_bytes(challenge["challenge_id"], challenge["nonce"], identity.did, manifest)
    )
    response = client.post(
        "/api/v1/agents/register",
        content=canonical_json(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def task_body(question: str = "envelope under test") -> dict:
    return {
        "kind": "research",
        "visibility": "public",
        "origin": "research",
        "required_capabilities": [],
        "chains": ["base"],
        "input": {"question": question},
        "acceptance": {"required_outputs": ["answer"]},
        "demand_provenance": {"type": "research_question", "level": 3},
        "generation_policy": {"minimum_provenance_level": 1},
        "economics": {"mode": "BOUNTY", "reward": {"amount": "1", "asset": "MOCK"}},
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "true")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(f"sqlite:///{tmp_path / 'outbox.db'}")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_publisher(monkeypatch):
    """Keep publisher state per-test and development-safe."""
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "event_signing_key", "")
    monkeypatch.setattr(settings, "event_publisher_id", "agentforge-test-publisher")
    reset_event_publisher()
    yield
    reset_event_publisher()


def make_event(
    *,
    kind: str = "TASK_CLAIMED",
    aggregate_id: str = "TASK-1",
    payload: dict | None = None,
    actor_did: str | None = "did:key:zActor",
    causation: dict | None = None,
    created_at: float | None = None,
) -> OutboxEvent:
    return OutboxEvent(
        id=new_id("OUT"),
        kind=kind,
        aggregate_id=aggregate_id,
        payload=payload if payload is not None else {"task_id": "TASK-1", "claim_id": "C-1"},
        status="PENDING",
        attempts=0,
        next_attempt_at=time.time(),
        created_at=created_at or time.time(),
        delivered_at=None,
        actor_did=actor_did,
        causation=causation,
    )


class RecordingAdapter:
    """Captures published envelopes; used to assert transport contracts."""

    def __init__(self, *, enabled: bool = True, result: bool = True, error: Exception | None = None):
        self.enabled = enabled
        self.result = result
        self.error = error
        self.published: list[dict] = []

    def publish_event(self, envelope: dict) -> bool:
        if self.error:
            raise self.error
        self.published.append(envelope)
        return self.result


def seeded_publisher() -> EventPublisher:
    settings.event_signing_key = "11" * 32
    reset_event_publisher()
    return get_event_publisher()


# ---------------------------------------------------------------- envelope shape


def test_envelope_is_deterministic_versioned_and_schema_valid():
    publisher = seeded_publisher()
    event = make_event(created_at=1_760_000_000.5)
    first = build_envelope(event, publisher=publisher)
    second = build_envelope(event, publisher=publisher)

    assert first["version"] == ENVELOPE_VERSION
    assert first == second, "identical rows must publish byte-identical envelopes"
    assert canonical_json(first) == canonical_json(second)
    assert first["occurred_at"] == "2025-10-09T08:53:20.500Z"  # epoch 1760000000.5
    assert first["aggregate"] == {"type": "task", "id": "TASK-1"}
    assert first["payload_hash"] == hash_payload(event.payload)
    assert first["payload_hash"].startswith("sha256:")
    SCHEMA_VALIDATOR.validate(first)
    validate_envelope(first)


def test_actor_and_server_attribution_are_both_present_and_verifiable():
    publisher = seeded_publisher()
    causation = {
        "request_id": "nonce-1",
        "request_signature": "sig",
        "request_body_hash": "sha256:" + "a" * 64,
    }
    envelope = build_envelope(
        make_event(actor_did="did:key:zPoster", causation=causation), publisher=publisher
    )

    assert envelope["actor"] == {"did": "did:key:zPoster"}
    assert envelope["causation"] == causation
    assert envelope["server"]["publisher_id"] == "agentforge-test-publisher"
    assert envelope["server"]["key_id"] == publisher.key_id
    assert verify_envelope(envelope, publisher.public_key_bytes)
    SCHEMA_VALIDATOR.validate(envelope)


def test_server_signature_covers_actor_causation_and_payload_hash():
    publisher = seeded_publisher()
    envelope = build_envelope(
        make_event(
            causation={
                "request_id": "nonce-1",
                "request_signature": "sig",
                "request_body_hash": "sha256:" + "a" * 64,
            }
        ),
        publisher=publisher,
    )
    assert verify_envelope(envelope, publisher.public_key_bytes)

    for mutate in (
        lambda env: env.__setitem__("actor", {"did": "did:key:zSomeoneElse"}),
        lambda env: env.__setitem__("payload_hash", "sha256:" + "b" * 64),
        lambda env: env["causation"].__setitem__("request_id", "other"),
        lambda env: env["server"].__setitem__("publisher_id", "other-publisher"),
        lambda env: env["server"].__setitem__("key_id", "0" * 16),
    ):
        tampered = json.loads(canonical_json(envelope))
        mutate(tampered)
        assert not verify_envelope(tampered, publisher.public_key_bytes)


def test_server_generated_events_have_null_actor_and_causation():
    publisher = seeded_publisher()
    envelope = build_envelope(
        make_event(kind="CLAIM_EXPIRED", actor_did=None, causation=None), publisher=publisher
    )
    assert envelope["actor"] is None
    assert envelope["causation"] is None
    assert envelope["aggregate"] == {"type": "task", "id": "TASK-1"}
    assert verify_envelope(envelope, publisher.public_key_bytes)
    SCHEMA_VALIDATOR.validate(envelope)


def test_canonical_signing_bytes_exclude_signature_only():
    publisher = seeded_publisher()
    envelope = build_envelope(make_event(), publisher=publisher)
    covered = signing_bytes(envelope)
    assert b"signature" not in covered
    assert json.loads(covered.decode())["server"] == {
        "publisher_id": publisher.publisher_id,
        "key_id": publisher.key_id,
    }


# ------------------------------------------------------------------- redaction


def test_envelope_never_publishes_raw_payload_or_private_content():
    publisher = seeded_publisher()
    event = make_event(
        kind="PROOF_SUBMITTED",
        payload={
            "task_id": "TASK-9",
            "submission_id": "SUB-9",
            "proof_hash": "c" * 64,
            "evidence": "PRIVATE-EVIDENCE-BODY",
            "api_key": "sk-secret-value",
            "wallet": {"private_key": "0xdeadbeef"},
        },
    )
    envelope = build_envelope(event, publisher=publisher)
    serialized = canonical_json(envelope)

    assert "payload" not in envelope
    for leaked in ("PRIVATE-EVIDENCE-BODY", "sk-secret-value", "0xdeadbeef", "private_key"):
        assert leaked not in serialized
    assert envelope["attributes"] == {
        "task_id": "TASK-9",
        "submission_id": "SUB-9",
        "proof_hash": "c" * 64,
    }
    assert envelope["payload_hash"] == hash_payload(event.payload)
    SCHEMA_VALIDATOR.validate(envelope)


def test_queued_task_payload_omits_private_input(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    body = task_body("PRIVATE-QUESTION-TEXT")
    response, _ = signed_request(client, poster, "POST", "/api/v1/tasks", body)
    assert response.status_code == 200, response.text

    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        assert event is not None
        assert "input" not in event.payload
        assert "PRIVATE-QUESTION-TEXT" not in canonical_json(event.payload)
        assert event.payload["task_hash"]


# ------------------------------------------------------------------ causation


def test_verified_request_is_recorded_as_causation(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    body = task_body()
    response, sent = signed_request(client, poster, "POST", "/api/v1/tasks", body)
    assert response.status_code == 200, response.text

    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        assert event.actor_did == poster.did
        causation = event.causation
        envelope = build_envelope(event, publisher=seeded_publisher())

    assert causation["request_id"] == sent["nonce"]
    assert causation["request_body_hash"] == "sha256:" + sha256_bytes(sent["body"])
    assert causation["method"] == "POST"
    assert causation["path"] == "/api/v1/tasks"
    # The recorded agent signature really covers the request that caused the event.
    assert verify_signature(
        poster.did,
        request_signing_bytes(
            sent["method"], sent["path"], sent["body"], sent["timestamp"], sent["nonce"]
        ),
        causation["request_signature"],
    )
    assert envelope["causation"] == causation
    assert envelope["actor"] == {"did": poster.did}
    SCHEMA_VALIDATOR.validate(envelope)


def test_claim_event_records_executor_as_actor(client):
    poster = AgentIdentity.generate()
    executor = AgentIdentity.generate()
    register(client, poster)
    register(client, executor)
    task = signed_request(client, poster, "POST", "/api/v1/tasks", task_body())[0].json()

    claim, _ = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    assert claim.status_code == 200, claim.text

    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CLAIMED"))
        assert event.actor_did == executor.did
        assert event.payload["executor_did"] == executor.did


# ------------------------------------------------------- transport feature flag


def test_disabled_transport_leaves_events_pending_without_consuming_attempts(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())
    adapter = TechnocoreAdapter("https://technocore.example", gossip_enabled=False, publish_path="/events")

    assert adapter.enabled is False
    assert adapter.status()["publish_path_configured"] is True
    with db.SessionLocal() as session:
        before = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        assert drain_once(session, adapter) == 0
        session.refresh(before)
        assert before.status == "PENDING"
        assert before.attempts == 0
        assert before.last_attempt_at is None
        assert before.last_error is None


def test_enabled_but_unconfigured_transport_never_invents_an_endpoint(client, monkeypatch):
    monkeypatch.setattr(settings, "gossip_enabled", True)
    monkeypatch.setattr(settings, "technocore_publish_path", "")
    adapter = TechnocoreAdapter.from_settings()

    assert adapter.enabled is False
    assert adapter.status() == {
        "adapter": "technocore",
        "base_url": settings.technocore_base_url,
        "enabled": False,
        "configured": False,
        "gossip_enabled": True,
        "publish_path_configured": False,
        "envelope_version": ENVELOPE_VERSION,
    }
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())
    with db.SessionLocal() as session:
        assert drain_once(session, adapter) == 0
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        assert event.status == "PENDING" and event.attempts == 0


def test_configured_transport_posts_the_signed_envelope(client, monkeypatch):
    monkeypatch.setattr(settings, "gossip_enabled", True)
    monkeypatch.setattr(settings, "technocore_publish_path", "/v1/gossip")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())

    transport_client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = TechnocoreAdapter.from_settings()
    adapter._client = transport_client
    publisher = seeded_publisher()
    try:
        with db.SessionLocal() as session:
            assert drain_once(session, adapter) == 1
            metrics = outbox_metrics(session)
    finally:
        transport_client.close()

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == f"{settings.technocore_base_url}/v1/gossip"
    envelope = json.loads(request.content.decode())
    assert request.headers["X-AgentForge-Event"] == envelope["event_id"]
    assert request.headers["X-AgentForge-Publisher"] == "agentforge-test-publisher"
    assert request.headers["X-AgentForge-Key-Id"] == envelope["server"]["key_id"]
    assert envelope["event_type"] == "TASK_CREATED"
    assert verify_envelope(envelope, publisher.public_key_bytes)
    SCHEMA_VALIDATOR.validate(envelope)
    assert metrics["delivered_count"] == 1
    assert metrics["pending_count"] == 0
    assert metrics["last_success_at"] is not None
    assert metrics["last_failure_at"] is None


def test_transport_failure_is_recorded_and_retried_without_losing_the_event(client, monkeypatch):
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())
    publisher = seeded_publisher()
    adapter = RecordingAdapter(result=False)

    with db.SessionLocal() as session:
        assert drain_once(session, adapter) == 0
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        assert event.status == "PENDING"
        assert event.attempts == 1
        assert event.last_attempt_at is not None
        assert "transport did not accept" in event.last_error

    # A retry after failure republishes the identical envelope.
    adapter.result = True
    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        first = adapter.published[-1] if adapter.published else None
        event.next_attempt_at = time.time() - 1
        session.commit()
        assert drain_once(session, adapter) == 1
        session.refresh(event)
        assert event.status == "DELIVERED"
        assert event.last_error is None
    assert first is None or verify_envelope(first, publisher.public_key_bytes)


def test_repeated_drain_of_a_delivered_event_is_idempotent(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())
    publisher = seeded_publisher()
    adapter = RecordingAdapter()

    with db.SessionLocal() as session:
        assert drain_once(session, adapter) == 1
        assert drain_once(session, adapter) == 0, "a DELIVERED event must not be published twice"
    assert len(adapter.published) == 1

    # Two publishes of the same row produce one identical signed record.
    event = make_event(created_at=1_760_000_000.0)
    first = build_envelope(event, publisher=publisher)
    second = build_envelope(event, publisher=publisher)
    assert first["event_id"] == second["event_id"]
    assert first["server"]["signature"] == second["server"]["signature"]


def test_exhausted_attempts_move_to_dead_letter_with_error(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())

    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        event.attempts = OUTBOX_MAX_ATTEMPTS - 1
        event.next_attempt_at = time.time() - 1
        session.commit()
        assert drain_once(session, RecordingAdapter(result=False)) == 0
        session.refresh(event)
        assert event.status == "DEAD"
        assert event.lease_owner is None
        assert event.last_error
        metrics = outbox_metrics(session)
        assert metrics["dead_count"] == 1
        assert metrics["pending_count"] == 0
        assert metrics["last_failure_at"] is not None


def test_expired_lease_is_reclaimable_and_metrics_report_age(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())

    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_CREATED"))
        old = time.time() - 120
        event.created_at = old
        event.status = "PROCESSING"
        event.attempts = 1
        event.lease_owner = "abandoned-worker"
        event.lease_expires_at = time.time() - 1
        session.commit()

        metrics = outbox_metrics(session)
        assert metrics["processing_count"] == 1
        assert metrics["oldest_pending_age_seconds"] is None

        adapter = RecordingAdapter()
        assert drain_once(session, adapter) == 1, "an expired lease must be reclaimable"
        session.refresh(event)
        assert event.status == "DELIVERED"

        event.status = "PENDING"
        session.commit()
        assert outbox_metrics(session)["oldest_pending_age_seconds"] == pytest.approx(120, abs=5)


# ------------------------------------------------------ keys, rotation, errors


def test_key_rotation_changes_key_id_and_invalidates_old_signatures(monkeypatch):
    monkeypatch.setattr(settings, "event_signing_key", "11" * 32)
    monkeypatch.setattr(settings, "environment", "development")
    reset_event_publisher()
    first = get_event_publisher()
    envelope = build_envelope(make_event(), publisher=first)
    assert first.source == "environment"
    assert verify_envelope(envelope, first.public_key_bytes)

    monkeypatch.setattr(settings, "event_signing_key", "22" * 32)
    reset_event_publisher()
    second = get_event_publisher()
    assert second.key_id != first.key_id
    assert not verify_envelope(envelope, second.public_key_bytes)

    rotated = build_envelope(make_event(), publisher=second)
    assert rotated["server"]["key_id"] == second.key_id
    assert verify_envelope(rotated, second.public_key_bytes)
    SCHEMA_VALIDATOR.validate(rotated)


def test_publisher_id_and_seed_are_server_derived(monkeypatch):
    monkeypatch.setattr(settings, "event_publisher_id", "agentforge-node-7")
    monkeypatch.setattr(settings, "event_signing_key", "ab" * 32)
    publisher = load_event_publisher()
    assert publisher.publisher_id == "agentforge-node-7"
    assert publisher.source == "environment"
    assert len(publisher.public_key_bytes) == 32
    # Stable cache: the same process must not silently change identity.
    assert get_event_publisher().key_id == publisher.key_id


def test_ephemeral_development_key_is_stable_within_the_process(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "event_signing_key", "")
    reset_event_publisher()
    first = get_event_publisher()
    assert first.source == "ephemeral"
    assert get_event_publisher() is first, "retries must not silently rotate the key"


def test_production_requires_a_configured_signing_key(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "event_signing_key", "")
    with pytest.raises(RuntimeError, match="required in production"):
        load_event_publisher()


def test_invalid_signing_key_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "event_signing_key", "not-a-valid-seed")
    with pytest.raises(RuntimeError, match="32 bytes|32-byte Ed25519 seed"):
        load_event_publisher()

    monkeypatch.setattr(settings, "event_signing_key", "zzzz-not-hex-or-base64!!")
    with pytest.raises(RuntimeError, match="hex or base64url"):
        load_event_publisher()


def test_malformed_envelopes_are_rejected():
    publisher = seeded_publisher()
    envelope = build_envelope(make_event(), publisher=publisher)

    for field in ("event_id", "event_type", "occurred_at", "aggregate", "payload_hash", "causation", "server"):
        broken = json.loads(canonical_json(envelope))
        broken.pop(field)
        with pytest.raises(EnvelopeError):
            validate_envelope(broken)
        assert not verify_envelope(broken, publisher.public_key_bytes)

    unknown_version = json.loads(canonical_json(envelope))
    unknown_version["version"] = "agentforge-event/2"
    with pytest.raises(EnvelopeError, match="unsupported envelope version"):
        validate_envelope(unknown_version)

    raw_payload = json.loads(canonical_json(envelope))
    raw_payload["payload"] = {"secret": True}
    with pytest.raises(EnvelopeError, match="raw event payloads"):
        validate_envelope(raw_payload)

    bad_actor = json.loads(canonical_json(envelope))
    bad_actor["actor"] = {"did": "not-a-did"}
    with pytest.raises(EnvelopeError, match="did:key"):
        validate_envelope(bad_actor)

    bad_causation = json.loads(canonical_json(envelope))
    bad_causation["causation"] = {"request_id": "x"}
    with pytest.raises(EnvelopeError, match="causation requires"):
        validate_envelope(bad_causation)


def test_build_envelope_rejects_an_event_without_identity():
    publisher = seeded_publisher()
    with pytest.raises(EnvelopeError):
        build_envelope(make_event(kind=""), publisher=publisher)


# ------------------------------------------------------------------- worker


def test_worker_single_tick_reports_counts(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())
    adapter = TechnocoreAdapter("https://technocore.example", gossip_enabled=False, publish_path="/events")

    summary = run_once(adapter)
    assert summary["delivered"] == 0
    assert summary["reaped_claims"] == 0
    assert summary["pending_count"] >= 1
    assert summary["dead_count"] == 0


def test_worker_stops_gracefully_when_signalled(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())
    stop_event = threading.Event()
    stop_event.set()  # behaves like a SIGTERM that arrived before the first tick
    adapter = TechnocoreAdapter("https://technocore.example", gossip_enabled=False, publish_path="/events")

    started = time.monotonic()
    ticks = run_worker(stop_event, adapter=adapter)
    assert ticks == 0
    assert time.monotonic() - started < 5


def test_worker_loop_exits_after_one_tick_when_asked(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    signed_request(client, poster, "POST", "/api/v1/tasks", task_body())
    adapter = TechnocoreAdapter("https://technocore.example", gossip_enabled=False, publish_path="/events")
    ticks = run_worker(threading.Event(), adapter=adapter, once=True)
    assert ticks == 1


def test_outbox_metrics_start_empty(client):
    metrics = outbox_metrics(db.SessionLocal())
    assert metrics == {
        "pending_count": 0,
        "processing_count": 0,
        "delivered_count": 0,
        "dead_count": 0,
        "oldest_pending_age_seconds": None,
        "last_success_at": None,
        "last_failure_at": None,
    }


def test_queue_outbox_defaults_to_unattributed_server_events(client):
    with db.SessionLocal() as session:
        event = queue_outbox(session, kind="TASK_EXPIRED", aggregate_id="TASK-42", payload={"task_id": "TASK-42"})
        session.commit()
        assert event.actor_did is None
        assert event.causation is None
        envelope = build_envelope(event, publisher=seeded_publisher())
    assert envelope["actor"] is None
    assert envelope["causation"] is None
    SCHEMA_VALIDATOR.validate(envelope)
