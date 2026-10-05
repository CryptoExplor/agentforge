"""Dedicated SDK coverage: every ``AgentForgeClient`` method against the live app.

The SDK's own ``httpx`` transport is pointed at the in-process ASGI app by
injecting the ``TestClient`` as ``client.http`` — the documented transport
injection point — so every request below exercises the real ingress
middleware, signature verification, idempotency, drift calibration and error
mapping. Nothing is mocked at the HTTP boundary.

Organization:

* identity: generation, atomic persistence, DID consistency, raw signing.
* compatibility: legacy import paths, error hierarchy, facade attributes.
* one full marketplace flow through the SDK (register -> ... -> validate ->
  reputation/events), then focused flows for cancellation, disputes and the
  submission-scoped validation endpoint.
* pagination: keyset cursor and offset modes of ``list_tasks`` plus every
  filter, preserving the legacy ``{"tasks": [...]}`` envelope.
* errors: structured mapping of live 401/404/409 responses, including the
  clock-drift self-correction loop driven by ``X-Server-Timestamp``.
"""

from __future__ import annotations

import base64
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from agentforge_sdk import (
    AgentForgeClient,
    AgentForgeError,
    AgentIdentity,
    AuthenticationError,
    ClockDriftError,
    IdempotencyConflictError,
)
from agentforge_sdk.crypto import canonical_json
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.settings import settings

from helpers import signed_request  # noqa: E402  (single-source test helpers)


# ---------------------------------------------------------------------------
# Fixtures and local helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_operator_policy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_validator_dids", frozenset())
    monkeypatch.setattr(settings, "open_operators", True)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A live app on a disposable SQLite database, with the mock faucet on."""
    monkeypatch.setenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "true")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(f"sqlite:///{tmp_path / 'sdk.db'}")
    with TestClient(create_app()) as http:
        yield http


def sdk(server: TestClient, identity: AgentIdentity) -> AgentForgeClient:
    """An SDK client whose transport is the in-process live app."""
    client = AgentForgeClient("http://exchange.test", identity)
    # Release the unused default httpx client before swapping in the app.
    client.transport.http.close()
    client.http = server
    return client


def register(server: TestClient, identity: AgentIdentity, manifest: dict) -> AgentForgeClient:
    client = sdk(server, identity)
    profile = client.register(manifest)
    assert profile["did"] == identity.did
    return client


def approve_validator(identity: AgentIdentity) -> None:
    settings.trusted_validator_dids = settings.trusted_validator_dids | {identity.did}


def task_payload(
    *,
    reward: str = "10",
    kind: str = "expert",
    origin: str = "external",
    capabilities: list[str] | None = None,
    strategy: str = "peer_review",
) -> dict:
    return {
        "kind": kind,
        "visibility": "public",
        "origin": origin,
        "verification_strategy": strategy,
        "required_capabilities": capabilities or ["proxy_security"],
        "chains": ["base"],
        "input": {"contract": "0xabc"},
        "acceptance": {"required_outputs": ["risk"], "required_evidence": ["bytecode_hash"]},
        "demand_provenance": {
            "type": "external_event",
            "level": 1,
            "source": "test",
            "source_ref": "event-1",
        },
        "generation_policy": {
            "economic_eligibility": "ECONOMIC_ELIGIBLE",
            "minimum_provenance_level": 1,
        },
        "economics": {
            "mode": "BOUNTY",
            "reward": {"amount": reward, "asset": "MOCK"},
            "security_deposit": {"amount": "1", "asset": "MOCK"},
            "inference_budget": {"amount": "2", "asset": "MOCK"},
        },
        "deadline": time.time() + 3600,
    }


def pending_submission(server: TestClient, *, strategy: str = "peer_review") -> dict:
    """Drive poster -> executor -> submitted proof through the SDK."""
    poster = AgentIdentity.generate()
    executor = AgentIdentity.generate()
    poster_client = register(
        server, poster, {"name": "poster", "capabilities": ["research"], "chains": ["base"]}
    )
    executor_client = register(
        server,
        executor,
        {"name": "executor", "capabilities": ["proxy_security"], "chains": ["base"]},
    )
    task = poster_client.create_task(task_payload(strategy=strategy))
    claim = executor_client.claim(task["id"])
    session = executor_client.infer(task["id"], {"provider": "mock", "requested_compute": "1"})
    submission = executor_client.submit(
        task["id"],
        result={"risk": "unknown"},
        evidence=[{"kind": "bytecode_hash", "content_hash": "sha256:abc"}],
        inference_session_ids=[session["session_id"]],
    )
    return {
        "poster": poster,
        "executor": executor,
        "poster_client": poster_client,
        "executor_client": executor_client,
        "task": task,
        "claim": claim,
        "session": session,
        "submission": submission,
    }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_identity_roundtrip_did_and_raw_signing(tmp_path):
    identity = AgentIdentity.generate()
    assert identity.did.startswith("did:key:z")

    target = tmp_path / "identity.json"
    identity.save(target)
    loaded = AgentIdentity.load(target)
    assert loaded.did == identity.did
    assert loaded.private_key_hex == identity.private_key_hex

    message = b"protocol message"
    signature = identity.sign(message)
    decoded = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    identity.key.public_key().verify(decoded, message)  # raises on mismatch


def test_identity_load_rejects_tampered_did(tmp_path):
    identity = AgentIdentity.generate()
    target = tmp_path / "identity.json"
    identity.save(target)
    import json

    tampered = json.loads(target.read_text())
    tampered["did"] = "did:key:zSomethingElse"
    target.write_text(json.dumps(tampered))
    with pytest.raises(AgentForgeError, match="does not match"):
        AgentIdentity.load(target)


# ---------------------------------------------------------------------------
# Modular layout and backward compatibility
# ---------------------------------------------------------------------------


def test_legacy_import_paths_and_error_hierarchy():
    import agentforge_sdk
    import agentforge_sdk.client as legacy
    import agentforge_sdk.errors as errors
    import agentforge_sdk.identity as identity
    import agentforge_sdk.transport as transport

    # The two historical import paths expose the same objects.
    from agentforge_sdk.client import (  # noqa: F401
        AgentForgeClient as LegacyClient,
        AgentForgeError as LegacyError,
        AgentIdentity as LegacyIdentity,
    )

    assert legacy.AgentForgeClient is LegacyClient is agentforge_sdk.AgentForgeClient
    assert legacy.AgentIdentity is LegacyIdentity is agentforge_sdk.AgentIdentity
    assert legacy.AgentIdentity is identity.AgentIdentity
    assert legacy.AgentForgeError is LegacyError is agentforge_sdk.AgentForgeError
    assert legacy.AgentForgeError is errors.AgentForgeError
    assert agentforge_sdk.AuthenticationError is errors.AuthenticationError
    assert agentforge_sdk.ClockDriftError is errors.ClockDriftError
    assert agentforge_sdk.IdempotencyConflictError is errors.IdempotencyConflictError

    # Structured errors stay catchable as the historical base class.
    for structured in (AuthenticationError, ClockDriftError, IdempotencyConflictError):
        assert issubclass(structured, AgentForgeError)

    # The facade keeps the historical filesystem patch surface: these module
    # attributes are the same stdlib module objects the identity code calls.
    assert legacy.os is os is sys.modules["os"]
    assert legacy.tempfile is sys.modules["tempfile"]

    # The transport module owns signing and error mapping; the facade composes it.
    assert hasattr(transport, "Transport")
    assert hasattr(transport, "error_for")


def test_client_facade_delegates_transport_attributes(server):
    identity = AgentIdentity.generate()
    client = AgentForgeClient("http://exchange.test/", identity)
    assert client.base_url == "http://exchange.test"
    assert client.transport.base_url == client.base_url

    # http and clock_offset are live views onto the transport.
    assert client.http is client.transport.http
    client.clock_offset = 12.5
    assert client.transport.clock_offset == 12.5
    assert client.clock_offset == 12.5
    client.transport.clock_offset = -3.0
    assert client.clock_offset == -3.0

    # Injecting a transport replacement is visible through the facade, and
    # closing the facade closes whatever transport is injected.
    class Closeable:
        closed = False

        def close(self):
            self.closed = True

    marker = Closeable()
    client.http = marker
    assert client.transport.http is marker
    client.close()
    assert marker.closed is True


# ---------------------------------------------------------------------------
# Full marketplace flow through the SDK
# ---------------------------------------------------------------------------


def test_full_exchange_flow_covers_every_core_method(server):
    poster, executor, validator = (
        AgentIdentity.generate(),
        AgentIdentity.generate(),
        AgentIdentity.generate(),
    )
    poster_client = register(
        server, poster, {"name": "poster", "capabilities": ["research"], "chains": ["base"]}
    )
    executor_client = register(
        server,
        executor,
        {"name": "executor", "capabilities": ["proxy_security"], "chains": ["base"]},
    )
    validator_client = register(
        server,
        validator,
        {"name": "validator", "capabilities": ["validation"], "chains": ["base"]},
    )
    approve_validator(validator)

    # The mock faucet funded both whitelisted assets on registration.
    assert poster_client.balance() == {**poster_client.balance(), "balance": "1000"}
    assert poster_client.balance(asset="TEST_CREDIT")["balance"] == "1000"
    assert poster_client.balance(asset="UNLISTED")["balance"] == "0"

    # Capability index and agent discovery.
    capabilities = poster_client.capabilities()["capabilities"]
    names = {item["name"]: item["agent_count"] for item in capabilities}
    assert names == {"research": 1, "proxy_security": 1, "validation": 1}

    found = poster_client.search_agents(capability="proxy_security")["agents"]
    assert [agent["did"] for agent in found] == [executor.did]
    assert len(poster_client.search_agents(chain="base")["agents"]) == 3

    agent = poster_client.get_agent(executor.did)
    assert agent["did"] == executor.did
    assert agent["name"] == "executor"

    # Task lifecycle: create -> get -> claim -> heartbeat -> infer -> submit.
    task = poster_client.create_task(task_payload())
    assert task["status"] == "FUNDED"
    assert task["escrow"]["status"] == "FUNDED"
    # 10 reward + 1 deposit + 2 inference budget were reserved from escrow.
    assert poster_client.balance()["balance"] == "987"

    fetched = executor_client.get_task(task["id"])
    assert fetched["id"] == task["id"]
    assert fetched["escrow"]["reserved_total"] == "13"

    claim = executor_client.claim(task["id"])
    assert claim["status"] == "ACTIVE"
    beat = executor_client.heartbeat(claim["claim_id"])
    assert beat["claim_id"] == claim["claim_id"]
    assert beat["lease_expires_at"] > 0

    session = executor_client.infer(task["id"], {"provider": "mock", "requested_compute": "1"})
    assert session["task_id"] == task["id"]
    retrieved = executor_client.get_inference_session(session["session_id"])
    assert retrieved["session_id"] == session["session_id"]
    assert retrieved["task_id"] == task["id"]
    assert retrieved["status"] == session["status"]

    submission = executor_client.submit(
        task["id"],
        result={"risk": "unknown"},
        evidence=[{"kind": "bytecode_hash", "content_hash": "sha256:abc"}],
        inference_session_ids=[session["session_id"]],
    )
    assert submission["status"] == "SUBMITTED"

    read_back = poster_client.get_submission(submission["submission_id"])
    assert read_back["proof_hash"] == submission["proof_hash"]
    proof = poster_client.get_proof(submission["submission_id"])
    assert proof["proof_hash"] == submission["proof_hash"]
    assert proof["status"] == "SUBMITTED"

    # Task-scoped peer validation: the proof hash is fetched automatically
    # from the submission to build the decision signature.
    verdict = validator_client.validate_task(
        task["id"],
        decision="VERIFIED",
        submission_id=submission["submission_id"],
        reason_codes=["ACCEPTANCE_CRITERIA_SATISFIED"],
    )
    assert verdict["decision"] == "VERIFIED"
    assert verdict["submission_id"] == submission["submission_id"]
    assert verdict["evidence_hash"] == submission["proof_hash"]

    settled = poster_client.get_task(task["id"])
    assert settled["status"] == "VERIFIED"
    assert settled["escrow"]["status"] == "RELEASED"

    # Reputation reflects the settled outcome for every role.
    assert poster_client.reputation(executor.did)["overall"] == 1.0
    assert poster_client.reputation(validator.did)["overall"] == 0.1
    assert poster_client.reputation()["did"] == poster.did  # own reputation by default

    # min_reputation is a local filter over the returned agent views.
    strong = poster_client.search_agents(min_reputation=0.5)["agents"]
    assert [agent["did"] for agent in strong] == [executor.did]

    # Actor-scoped audit events, with cursor pagination.
    first_page = poster_client.events(limit=1)
    assert [event["kind"] for event in first_page["events"]] == ["AGENT_REGISTERED"]
    assert first_page["has_more"] is True
    second_page = poster_client.events(cursor=first_page["next_cursor"])
    assert second_page["events"][0]["kind"] == "TASK_CREATED"
    assert second_page["has_more"] is False


def test_validate_task_with_explicit_evidence_hash(server):
    state = pending_submission(server)
    validator = AgentIdentity.generate()
    validator_client = register(
        server, validator, {"name": "validator", "capabilities": ["validation"], "chains": ["base"]}
    )
    approve_validator(validator)

    verdict = validator_client.validate_task(
        state["task"]["id"],
        decision="VERIFIED",
        submission_id=state["submission"]["submission_id"],
        evidence_hash=state["submission"]["proof_hash"],
        checks=[{"kind": "manual", "passed": True}],
    )
    assert verdict["decision"] == "VERIFIED"
    assert verdict["evidence_hash"] == state["submission"]["proof_hash"]
    assert state["poster_client"].get_task(state["task"]["id"])["status"] == "VERIFIED"


def test_validate_task_requires_submission_id(server):
    state = pending_submission(server)
    validator_client = sdk(server, AgentIdentity.generate())
    with pytest.raises(ValueError, match="submission_id"):
        validator_client.validate_task(state["task"]["id"], decision="VERIFIED")


def test_submission_scoped_validate(server):
    state = pending_submission(server)
    validator = AgentIdentity.generate()
    validator_client = register(
        server, validator, {"name": "validator", "capabilities": ["validator"], "chains": ["base"]}
    )
    approve_validator(validator)

    verdict = validator_client.validate(
        state["submission"]["submission_id"],
        decision="REJECTED",
        reason_codes=["ACCEPTANCE_CRITERIA_UNSATISFIED"],
    )
    assert verdict["decision"] == "REJECTED"
    task = state["poster_client"].get_task(state["task"]["id"])
    assert task["status"] == "REJECTED"
    assert task["escrow"]["status"] == "REFUNDED"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancel_task_refunds_escrow_and_double_cancel_conflicts(server):
    poster_client = register(
        server,
        AgentIdentity.generate(),
        {"name": "poster", "capabilities": ["research"], "chains": ["base"]},
    )
    task = poster_client.create_task(task_payload())
    assert poster_client.balance()["balance"] == "987"

    cancelled = poster_client.cancel_task(task["id"])
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["escrow"]["status"] == "REFUNDED"
    assert poster_client.balance()["balance"] == "1000"

    # A second cancel of the same task is a plain conflict, not an
    # idempotency conflict, and stays catchable as the base SDK error.
    with pytest.raises(AgentForgeError, match="cannot be cancelled") as exc_info:
        poster_client.cancel_task(task["id"])
    assert not isinstance(exc_info.value, IdempotencyConflictError)
    assert exc_info.value.status_code == 409


def test_cancel_task_rejects_non_poster(server):
    state = pending_submission(server)
    with pytest.raises(AgentForgeError, match="only the requester") as exc_info:
        state["executor_client"].cancel_task(state["task"]["id"])
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Disputes
# ---------------------------------------------------------------------------


def test_dispute_flow_open_and_resolve(server):
    state = pending_submission(server)
    validator = AgentIdentity.generate()
    validator_client = register(
        server, validator, {"name": "validator", "capabilities": ["validation"], "chains": ["base"]}
    )
    approve_validator(validator)

    dispute = state["poster_client"].open_dispute(
        state["submission"]["submission_id"],
        reason="the result does not match the acceptance criteria",
    )
    assert dispute["status"] == "OPEN"
    assert state["poster_client"].get_task(state["task"]["id"])["status"] == "DISPUTED"

    resolved = validator_client.resolve_dispute(
        dispute["dispute_id"],
        submission_id=state["submission"]["submission_id"],
        decision="VERIFIED",
        checks=[{"kind": "manual", "passed": True}],
    )
    assert resolved["decision"] == "VERIFIED"
    task = state["poster_client"].get_task(state["task"]["id"])
    assert task["status"] == "VERIFIED"
    assert task["escrow"]["status"] == "RELEASED"


# ---------------------------------------------------------------------------
# list_tasks: filters, legacy envelope and pagination
# ---------------------------------------------------------------------------


def test_list_tasks_legacy_envelope_and_filters(server):
    poster_client = register(
        server,
        AgentIdentity.generate(),
        {"name": "poster", "capabilities": ["research"], "chains": ["base"]},
    )
    plans = [
        ("1", "research", "research", ["research"]),
        ("5", "expert", "external", ["proxy_security"]),
        ("10", "research", "research", ["research"]),
    ]
    created = [
        poster_client.create_task(
            task_payload(reward=reward, kind=kind, origin=origin, capabilities=capabilities)
        )
        for reward, kind, origin, capabilities in plans
    ]
    ids = [task["id"] for task in created]

    # Legacy calls (filters/limit only) keep the exact historical envelope.
    legacy = poster_client.list_tasks()
    assert set(legacy) == {"tasks"}
    assert [task["id"] for task in legacy["tasks"]] == list(reversed(ids))
    assert set(poster_client.list_tasks(limit=2)) == {"tasks"}

    # Every server filter is passed through unchanged.
    assert [t["id"] for t in poster_client.list_tasks(kind="expert")["tasks"]] == [ids[1]]
    assert [t["id"] for t in poster_client.list_tasks(origin="external")["tasks"]] == [ids[1]]
    assert [t["id"] for t in poster_client.list_tasks(capability="proxy_security")["tasks"]] == [ids[1]]
    assert [t["id"] for t in poster_client.list_tasks(min_reward="6")["tasks"]] == [ids[2]]
    assert len(poster_client.list_tasks(status="FUNDED")["tasks"]) == 3
    assert len(poster_client.list_tasks(chain="base")["tasks"]) == 3
    assert len(poster_client.list_tasks(verification_strategy="peer_review")["tasks"]) == 3


def test_list_tasks_cursor_and_offset_pagination(server):
    poster_client = register(
        server,
        AgentIdentity.generate(),
        {"name": "poster", "capabilities": ["research"], "chains": ["base"]},
    )
    ids = [
        poster_client.create_task(task_payload(reward=reward))["id"]
        for reward in ("1", "5", "10")
    ]

    # Keyset pagination: page through with the opaque cursor, newest first.
    page_one = poster_client.list_tasks(limit=2, offset=0)
    assert page_one["total"] == 3
    assert page_one["limit"] == 2
    assert page_one["offset"] == 0
    assert page_one["has_more"] is True
    assert page_one["next_cursor"]
    page_two = poster_client.list_tasks(limit=2, cursor=page_one["next_cursor"])
    assert [t["id"] for t in page_two["tasks"]] == [ids[0]]
    assert page_two["has_more"] is False
    assert page_two["next_cursor"] is None
    # The cursor wins over offset: the response reports the cursor page.
    assert page_two["offset"] == 0
    walked = [t["id"] for t in page_one["tasks"]] + [t["id"] for t in page_two["tasks"]]
    assert walked == list(reversed(ids))

    # Offset pagination over the same deterministic order.
    tail = poster_client.list_tasks(limit=2, offset=2)
    assert [t["id"] for t in tail["tasks"]] == [ids[0]]
    assert tail["has_more"] is False
    assert tail["total"] == 3

    # Filters compose with pagination metadata.
    filtered = poster_client.list_tasks(min_reward="6", limit=1, offset=0)
    assert filtered["total"] == 1
    assert [t["id"] for t in filtered["tasks"]] == [ids[2]]


def test_list_tasks_preserves_historical_passthrough_of_extra_filters(server):
    poster_client = register(
        server,
        AgentIdentity.generate(),
        {"name": "poster", "capabilities": ["research"], "chains": ["base"]},
    )
    poster_client.create_task(task_payload())
    # Unknown keywords were always forwarded as query parameters; the server
    # ignores the unrecognized one and the legacy envelope is preserved.
    result = poster_client.list_tasks(some_future_filter="value")
    assert set(result) == {"tasks"}
    assert len(result["tasks"]) == 1


# ---------------------------------------------------------------------------
# Structured error mapping against live responses
# ---------------------------------------------------------------------------


def test_unregistered_agent_maps_to_authentication_error(server):
    stranger = sdk(server, AgentIdentity.generate())
    with pytest.raises(AuthenticationError) as exc_info:
        stranger.claim("T_whatever")
    assert exc_info.value.status_code == 401
    assert str(exc_info.value) == "HTTP 401: unknown or inactive agent"
    assert exc_info.value.detail == "unknown or inactive agent"
    # The structured error is still the historical base class.
    assert isinstance(exc_info.value, AgentForgeError)


def test_missing_task_maps_to_plain_agentforge_error(server):
    client = register(
        server,
        AgentIdentity.generate(),
        {"name": "poster", "capabilities": ["research"], "chains": ["base"]},
    )
    with pytest.raises(AgentForgeError, match="HTTP 404: task not found") as exc_info:
        client.get_task("T_missing")
    assert type(exc_info.value) is AgentForgeError
    assert exc_info.value.status_code == 404


def test_clock_drift_error_then_self_correction_from_server_header(server):
    client = register(
        server,
        AgentIdentity.generate(),
        {"name": "poster", "capabilities": ["research"], "chains": ["base"]},
    )
    # A badly skewed local clock pushes the signature outside the server's
    # drift window; balance() always authenticates, so the 401 is guaranteed.
    client.clock_offset = 5000.0
    with pytest.raises(ClockDriftError) as exc_info:
        client.balance()
    assert exc_info.value.status_code == 401
    assert "clock drift" in str(exc_info.value)

    # The very same response carried X-Server-Timestamp, so the transport
    # recalibrated and the retried logical operation succeeds.
    assert abs(client.clock_offset) < 5
    assert client.balance()["did"] == client.identity.did


def test_idempotency_key_conflict_maps_to_structured_error(server):
    poster = AgentIdentity.generate()
    client = register(
        server, poster, {"name": "poster", "capabilities": ["research"], "chains": ["base"]}
    )
    # The SDK mints a fresh nonce per request that doubles as its
    # Idempotency-Key, so a key conflict needs two hand-built requests with
    # one shared key — exactly the reuse the server must refuse.
    payload = task_payload(reward="2")
    first = signed_request(
        server, poster, "POST", "/api/v1/tasks", payload, key="reused-key", nonce="n1"
    )
    assert first.status_code == 200
    conflict = signed_request(
        server, poster, "POST", "/api/v1/tasks", task_payload(reward="3"), key="reused-key", nonce="n2"
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Idempotency-Key was reused with a different request"
    with pytest.raises(IdempotencyConflictError) as exc_info:
        client.transport.decode(conflict)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Idempotency-Key was reused with a different request"

    # The identical request under the same key replays the stored response.
    replay = signed_request(
        server, poster, "POST", "/api/v1/tasks", payload, key="reused-key", nonce="n3"
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------


def test_client_context_manager_closes_transport(server):
    identity = AgentIdentity.generate()
    with AgentForgeClient("http://exchange.test", identity) as client:
        assert not client.http.is_closed
    assert client.http.is_closed
