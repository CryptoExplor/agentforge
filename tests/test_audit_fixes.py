from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, registration_bytes, request_bytes, sha256_json
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.models import (
    Agent,
    Claim,
    Dispute,
    Escrow,
    InferenceSession,
    LedgerEvent,
    OutboxEvent,
    Submission,
    Task,
    ValidationDecision,
)
from agentforge_server.outbox import drain_once
from agentforge_server.provenance import clear_source_adapters, register_source_adapter
from agentforge_server.services import escrow_settle, fund_task, new_id, queue_outbox
from agentforge_server.settings import settings


def signed_request(
    client: TestClient,
    identity: AgentIdentity,
    method: str,
    path: str,
    payload: dict,
    *,
    key: str | None = None,
    nonce: str | None = None,
    signing_path: str | None = None,
    include_idempotency: bool = True,
):
    body = canonical_json(payload).encode()
    timestamp = str(int(time.time()))
    nonce = nonce or uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(
            request_bytes(method, signing_path or path.split("?", 1)[0], body, timestamp, nonce)
        ),
    }
    if include_idempotency:
        headers["Idempotency-Key"] = key or f"idem-{uuid.uuid4().hex}"
    return client.request(method, path, content=body, headers=headers)


def register(client: TestClient, identity: AgentIdentity, manifest: dict | None = None):
    manifest = manifest or {"name": "agent", "capabilities": [], "chains": ["base"]}
    challenge = client.get("/api/v1/register/challenge").json()
    payload = {
        "challenge_id": challenge["challenge_id"],
        "nonce": challenge["nonce"],
        "did": identity.did,
        "manifest": manifest,
    }
    payload["signature"] = identity.sign(
        registration_bytes(
            challenge["challenge_id"],
            challenge["nonce"],
            identity.did,
            manifest,
        )
    )
    response = client.post(
        "/api/v1/agents/register",
        content=canonical_json(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "true")
    monkeypatch.setattr(settings, "enable_mock_faucet", True)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    db.configure_database(f"sqlite:///{tmp_path / 'audit.db'}")
    with TestClient(create_app()) as test_client:
        yield test_client


def task_payload(
    *,
    visibility: str = "public",
    acceptance: dict | None = None,
    economics: dict | None = None,
    demand_provenance: dict | None = None,
    generation_policy: dict | None = None,
    deadline: float | None = None,
) -> dict:
    payload = {
        "kind": "research",
        "visibility": visibility,
        "origin": "research",
        "required_capabilities": [],
        "chains": ["base"],
        "input": {"question": "answer this"},
        "acceptance": acceptance or {"required_outputs": ["answer"]},
        "demand_provenance": demand_provenance or {"type": "research_question", "level": 3},
        "generation_policy": generation_policy or {"minimum_provenance_level": 1},
        "economics": economics or {"mode": "REPUTATION"},
    }
    if deadline is not None:
        payload["deadline"] = deadline
    return payload


def create_task(client, poster, payload: dict | None = None, *, key: str | None = None):
    response = signed_request(client, poster, "POST", "/api/v1/tasks", payload or task_payload(), key=key)
    assert response.status_code == 200, response.text
    return response.json()


def submit_payload(
    client: TestClient,
    task: dict,
    executor: AgentIdentity,
    *,
    result: dict | None = None,
    evidence: list[dict] | None = None,
    inference_session_ids: list[str] | None = None,
    submission_id: str | None = None,
    created_at: float | None = None,
) -> dict:
    result = result or {"answer": "ok"}
    evidence = evidence or []
    inference_session_ids = inference_session_ids or []
    submission_id = submission_id or f"S_{uuid.uuid4().hex}"
    created_at = created_at or time.time()
    proof_core = {
        "task_id": task["id"],
        "submission_id": submission_id,
        "executor_did": executor.did,
        "input_hash": sha256_json(task["input"]),
        "result_hash": sha256_json(result),
        "evidence": evidence,
        "inference_session_ids": inference_session_ids,
        "created_at": created_at,
    }
    return {
        "submission_id": submission_id,
        "result": result,
        "evidence": evidence,
        "inference_session_ids": inference_session_ids,
        "created_at": created_at,
        "proof_signature": executor.sign(canonical_json(proof_core).encode()),
    }


def submit(client, task, executor, **kwargs):
    payload = submit_payload(client, task, executor, **kwargs)
    response = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/submissions",
        payload,
    )
    assert response.status_code == 200, response.text
    return response.json(), payload


def decision_payload(client, validator: AgentIdentity, submission: dict, decision: str = "VERIFIED", settlement=None):
    body = {
        "decision_id": f"VD_{uuid.uuid4().hex}",
        "decision": decision,
        "policy": "deterministic_then_domain",
        "checks": [{"code": "CLIENT_CLAIM", "result": "PASS"}],
        "reason_codes": [],
        "settlement": settlement or {},
        "evidence_hash": submission["proof_hash"],
    }
    core = {
        "decision_id": body["decision_id"],
        "submission_id": submission["submission_id"],
        "validator_did": validator.did,
        **body,
    }
    body["signature"] = validator.sign(canonical_json(core).encode())
    return body


def validate(client, validator, submission, *, decision="VERIFIED", settlement=None, key=None):
    body = decision_payload(client, validator, submission, decision, settlement)
    response = signed_request(
        client,
        validator,
        "POST",
        f"/api/v1/submissions/{submission['submission_id']}/validate",
        body,
        key=key,
    )
    return response, body


def setup_three_agents(client, *, private=False):
    poster = AgentIdentity.generate()
    executor = AgentIdentity.generate()
    validator = AgentIdentity.generate()
    register(client, poster, {"name": "poster", "capabilities": ["research"], "chains": ["base"]})
    register(client, executor, {"name": "executor", "capabilities": ["research"], "chains": ["base"]})
    register(client, validator, {"name": "validator", "capabilities": ["validation"], "chains": ["base"]})
    # Explicit operator grant; registration metadata itself conveys no authority.
    settings.trusted_validator_dids = settings.trusted_validator_dids | {validator.did}
    task = create_task(client, poster, task_payload(visibility="private" if private else "public"))
    return poster, executor, validator, task


def test_idempotency_replay_and_conflict_cover_the_mutating_flow(client):
    poster, executor, validator, task = setup_three_agents(client)
    create_body = task_payload()
    key = "same-task-key"
    first = signed_request(client, poster, "POST", "/api/v1/tasks", create_body, key=key)
    replay = signed_request(client, poster, "POST", "/api/v1/tasks", copy.deepcopy(create_body), key=key)
    conflict_body = copy.deepcopy(create_body)
    conflict_body["input"]["question"] = "different"
    conflict = signed_request(client, poster, "POST", "/api/v1/tasks", conflict_body, key=key)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409

    claim_key = "same-claim-key"
    claimed = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}, key=claim_key)
    claimed_replay = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {}, key=claim_key)
    assert claimed.status_code == claimed_replay.status_code == 200
    assert claimed_replay.json() == claimed.json()

    inference_body = {
        "provider": "mock",
        "model_ref": "mock:model-v1",
        "requested_compute": "1.25",
        "input_data": {"x": 1},
    }
    inference = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/inference",
        inference_body,
        key="same-inference-key",
    )
    inference_replay = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/inference",
        inference_body,
        key="same-inference-key",
    )
    assert inference.status_code == inference_replay.status_code == 200
    assert inference.json() == inference_replay.json()

    submission_body = submit_payload(
        client,
        task,
        executor,
        inference_session_ids=[inference.json()["session_id"]],
    )
    submitted = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/submissions",
        submission_body,
        key="same-submission-key",
    )
    submitted_replay = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/submissions",
        submission_body,
        key="same-submission-key",
    )
    assert submitted.status_code == submitted_replay.status_code == 200
    assert submitted.json() == submitted_replay.json()
    session_view = signed_request(
        client,
        executor,
        "GET",
        f'/api/v1/inference/{inference.json()["session_id"]}',
        {},
        include_idempotency=False,
    )
    assert session_view.status_code == 200
    assert session_view.json()["submission_id"] == submitted.json()["submission_id"]

    submission = submitted.json()
    validation_body = decision_payload(client, validator, submission)
    validated = signed_request(
        client,
        validator,
        "POST",
        f"/api/v1/submissions/{submission['submission_id']}/validate",
        validation_body,
        key="same-validation-key",
    )
    validated_replay = signed_request(
        client,
        validator,
        "POST",
        f"/api/v1/submissions/{submission['submission_id']}/validate",
        validation_body,
        key="same-validation-key",
    )
    assert validated.status_code == validated_replay.status_code == 200
    assert validated.json() == validated_replay.json()


def test_claim_expiry_reopens_and_rejects_old_work(client):
    poster, executor, _, task = setup_three_agents(client)
    claim_response = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    claim_id = claim_response.json()["claim_id"]
    with db.SessionLocal() as session:
        claim = session.get(Claim, claim_id)
        assert claim
        claim.lease_expires_at = time.time() - 1
        session.commit()

    heartbeat = signed_request(client, executor, "POST", f"/api/v1/claims/{claim_id}/heartbeat", {})
    assert heartbeat.status_code == 409
    reopened = client.get(f"/api/v1/tasks/{task['id']}")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "OPEN"
    with db.SessionLocal() as session:
        expired = session.get(Claim, claim_id)
        assert expired and expired.status == "EXPIRED"

    new_claim = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    assert new_claim.status_code == 200
    inference = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/inference",
        {"provider": "mock", "requested_compute": "2"},
    )
    assert inference.status_code == 200
    with db.SessionLocal() as session:
        current = session.get(Claim, new_claim.json()["claim_id"])
        assert current
        current.lease_expires_at = time.time() - 1
        session.commit()
    late = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/submissions",
        submit_payload(task=task, client=client, executor=executor, inference_session_ids=[inference.json()["session_id"]]),
    )
    assert late.status_code in {404, 409}
    assert "submission" not in late.text.lower() or late.status_code == 409


def test_private_payloads_sessions_proofs_and_balances_require_authentication(client):
    poster, executor, validator, task = setup_three_agents(client, private=True)
    assert all(item["id"] != task["id"] for item in client.get("/api/v1/tasks").json()["tasks"])
    assert client.get(f"/api/v1/tasks/{task['id']}").status_code == 404

    outsider = AgentIdentity.generate()
    register(client, outsider, {"name": "outsider", "capabilities": [], "chains": ["base"]})
    assert signed_request(client, outsider, "GET", f"/api/v1/tasks/{task['id']}", {}, include_idempotency=False).status_code == 404
    owner_view = signed_request(client, poster, "GET", f"/api/v1/tasks/{task['id']}", {}, include_idempotency=False)
    assert owner_view.status_code == 200
    assert owner_view.json()["input"] == task["input"]
    assert client.get(f"/api/v1/agents/{poster.did}/balance").status_code == 401
    assert signed_request(client, outsider, "GET", f"/api/v1/agents/{poster.did}/balance", {}, include_idempotency=False).status_code == 403
    assert signed_request(client, poster, "GET", f"/api/v1/agents/{poster.did}/balance", {}, include_idempotency=False).status_code == 200

    claim = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    assert claim.status_code == 200
    inference = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/inference",
        {"provider": "mock", "input_data": {"private": True}},
    )
    assert inference.status_code == 200
    submission, _ = submit(client, task, executor, inference_session_ids=[inference.json()["session_id"]])
    assert signed_request(client, outsider, "GET", f"/api/v1/inference/{inference.json()['session_id']}", {}, include_idempotency=False).status_code == 404
    assert signed_request(client, outsider, "GET", f"/api/v1/submissions/{submission['submission_id']}", {}, include_idempotency=False).status_code == 404
    assert signed_request(client, outsider, "GET", f"/api/v1/proofs/{submission['submission_id']}", {}, include_idempotency=False).status_code == 404
    assert signed_request(client, executor, "GET", f"/api/v1/inference/{inference.json()['session_id']}", {}, include_idempotency=False).status_code == 200
    assert signed_request(client, executor, "GET", f"/api/v1/submissions/{submission['submission_id']}", {}, include_idempotency=False).status_code == 200
    assert signed_request(client, executor, "GET", f"/api/v1/proofs/{submission['submission_id']}", {}, include_idempotency=False).status_code == 200


def test_provenance_claims_stay_level_zero_until_a_registered_adapter(client):
    clear_source_adapters()
    poster = AgentIdentity.generate()
    register(client, poster)
    provenance = {
        "type": "external_event",
        "level": 3,
        "source": "trusted",
        "source_adapter": "trusted",
        "source_ref": "event-1",
        "novelty_hash": "novel-1",
    }
    unverified = create_task(
        client,
        poster,
        task_payload(demand_provenance=provenance),
    )
    assert unverified["demand_provenance"]["reported_level"] == 3
    assert unverified["demand_provenance"]["verified_level"] == 0
    assert unverified["demand_provenance"]["level"] == 0
    assert unverified["activity_eligibility"] == "NOT_ELIGIBLE"

    register_source_adapter("trusted", lambda claim: claim.get("source_ref") == "event-1")
    verified = create_task(client, poster, task_payload(demand_provenance=provenance))
    assert verified["demand_provenance"]["verified_level"] == 1
    assert verified["activity_eligibility"] == "REPUTATION_ELIGIBLE"
    clear_source_adapters()


def test_deterministic_acceptance_rejects_validator_claims_and_tampered_proofs(client):
    poster, executor, validator, task = setup_three_agents(client)
    # The result is structurally signed by the executor but violates the task's
    # independently evaluated JSON Schema.
    with db.SessionLocal() as session:
        stored = session.get(Task, task["id"])
        assert stored
        stored.acceptance = {
            "required_outputs": ["answer"],
            "result_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        }
        stored.acceptance_hash = sha256_json(stored.acceptance)
        session.commit()
    claim = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    assert claim.status_code == 200
    submission, submission_body = submit(client, task, executor, result={"answer": 42})
    decision = decision_payload(client, validator, submission)
    decision["checks"] = [{"code": "EVERYTHING_PASS", "result": "PASS"}]
    # The signature still covers the old checks, so create a matching signature
    # for the intentionally optimistic validator claim.
    core = {
        "decision_id": decision["decision_id"],
        "submission_id": submission["submission_id"],
        "validator_did": validator.did,
        **{key: decision[key] for key in ["decision", "policy", "checks", "reason_codes", "settlement", "evidence_hash"]},
    }
    decision["signature"] = validator.sign(canonical_json(core).encode())
    rejected = signed_request(
        client,
        validator,
        "POST",
        f"/api/v1/submissions/{submission['submission_id']}/validate",
        decision,
    )
    assert rejected.status_code == 422
    assert "deterministic" in rejected.text.lower()
    with db.SessionLocal() as session:
        assert session.scalar(select(ValidationDecision).where(ValidationDecision.submission_id == submission["submission_id"])) is None

    # A body/result change without resigning the proof is rejected at ingress.
    second_task = create_task(client, poster)
    assert signed_request(client, executor, "POST", f"/api/v1/tasks/{second_task['id']}/claim", {}).status_code == 200
    tampered = submit_payload(client, second_task, executor)
    tampered["result"] = {"answer": "changed"}
    bad = signed_request(client, executor, "POST", f"/api/v1/tasks/{second_task['id']}/submissions", tampered)
    assert bad.status_code == 401


def test_cursor_audit_pagination_is_authenticated_and_stable(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    create_task(client, poster)
    create_task(client, poster)
    first = signed_request(
        client,
        poster,
        "GET",
        "/api/v1/events?limit=1",
        {},
        include_idempotency=False,
        signing_path="/api/v1/events",
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["events"]) == 1
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str)
    second = signed_request(
        client,
        poster,
        "GET",
        "/api/v1/events?limit=1&cursor=" + quote(cursor, safe=""),
        {},
        include_idempotency=False,
        signing_path="/api/v1/events",
    )
    assert second.status_code == 200, second.text
    assert first.json()["events"][0]["id"] != second.json()["events"][0]["id"]
    assert signed_request(client, AgentIdentity.generate(), "GET", "/api/v1/events", {}, include_idempotency=False).status_code == 401


def test_active_claim_index_and_outbox_leases_are_database_safe(client):
    poster = AgentIdentity.generate()
    executor = AgentIdentity.generate()
    register(client, poster)
    register(client, executor)
    task = create_task(client, poster)
    with db.SessionLocal() as session:
        first = Claim(
            id=new_id("C"), task_id=task["id"], executor_did=executor.did, attempt=1,
            status="ACTIVE", lease_expires_at=time.time() + 100, heartbeat_at=time.time(),
            created_at=time.time(), updated_at=time.time(),
        )
        second = Claim(
            id=new_id("C"), task_id=task["id"], executor_did=executor.did, attempt=1,
            status="ACTIVE", lease_expires_at=time.time() + 100, heartbeat_at=time.time(),
            created_at=time.time(), updated_at=time.time(),
        )
        session.add_all([first, second])
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    class Adapter:
        def __init__(self):
            self.published = []

        def publish_event(self, envelope):
            # The outbox now publishes signed canonical envelopes.
            self.published.append(envelope["event_id"])
            return True

    adapter = Adapter()
    with db.SessionLocal() as session:
        for old_event in session.scalars(select(OutboxEvent)).all():
            session.delete(old_event)
        session.commit()
        event = OutboxEvent(
            id=new_id("OUT"), kind="TEST", aggregate_id="task", payload={}, status="PROCESSING",
            attempts=1, next_attempt_at=time.time(), lease_owner="other",
            lease_expires_at=time.time() + 1000, created_at=time.time(), delivered_at=None,
        )
        session.add(event)
        session.commit()
        assert drain_once(session, adapter) == 0
        event.lease_expires_at = time.time() - 1
        session.commit()
        assert drain_once(session, adapter) == 1
        assert len(adapter.published) == 1


def test_mock_escrow_transitions_conserve_value_and_cannot_double_settle(client):
    poster = AgentIdentity.generate()
    executor = AgentIdentity.generate()
    register(client, poster)
    register(client, executor)

    transitions = [
        ("VERIFIED", {}, "RELEASED", "10", "3", "0"),
        ("PARTIAL", {"executor_amount": "4"}, "PARTIAL", "4", "9", "0"),
        ("REJECTED", {}, "REFUNDED", "0", "13", "0"),
        ("SLASHED", {"slash_subject": "requester"}, "SLASHED", "0", "11", "2"),
    ]
    with db.SessionLocal() as session:
        for decision, settlement, status, released, refunded, slashed in transitions:
            task = Task(
                id=new_id("T"), version=1, kind="research", visibility="public", origin="research",
                poster_did=poster.did, required_capabilities=[], chains=[], input_data={"x": 1},
                acceptance={"required_outputs": ["answer"]}, demand_provenance={}, generation_policy={},
                anti_circularity={}, economics={"reward": {"amount": "10", "asset": "MOCK"}, "security_deposit": {"amount": "2", "asset": "MOCK"}, "inference_budget": {"amount": "1", "asset": "MOCK"}},
                deadline=None, status="OPEN", activity_eligibility="REPUTATION_ELIGIBLE",
                acceptance_hash=sha256_json({"required_outputs": ["answer"]}), task_hash=new_id("HASH"),
                claim_id=None, state_version=1, created_at=time.time(), updated_at=time.time(),
            )
            session.add(task)
            session.flush()
            fund_task(session, task)
            session.flush()
            escrow_settle(session, task=task, executor_did=executor.did, decision=decision, settlement=settlement)
            session.commit()
            escrow = session.get(Escrow, task.id)
            assert escrow and escrow.status == status
            assert (escrow.released_amount, escrow.refunded_amount, escrow.slashed_amount) == (released, refunded, slashed)
            assert escrow_settle
            with pytest.raises(ValueError):
                escrow_settle(session, task=task, executor_did=executor.did, decision=decision, settlement=settlement)

        slash_event = session.scalar(
            select(LedgerEvent).where(
                LedgerEvent.reason == "SLASH",
                LedgerEvent.amount_delta == "0",
            )
        )
        assert slash_event


def test_authentication_rejects_missing_idempotency_stale_and_bad_signatures(client):
    identity = AgentIdentity.generate()
    register(client, identity)
    body = task_payload()
    missing = signed_request(client, identity, "POST", "/api/v1/tasks", body, include_idempotency=False)
    assert missing.status_code == 400

    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time()) - 10_000)
    raw = canonical_json(body).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(request_bytes("POST", "/api/v1/tasks", raw, timestamp, nonce)),
        "Idempotency-Key": "stale",
    }
    assert client.post("/api/v1/tasks", content=raw, headers=headers).status_code == 401

    bad = signed_request(client, identity, "POST", "/api/v1/tasks", body)
    assert bad.status_code == 200
    bad_nonce = uuid.uuid4().hex
    bad_headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": str(int(time.time())),
        "X-Agent-Nonce": bad_nonce,
        "X-Agent-Signature": "not-a-signature",
        "Idempotency-Key": "bad-signature",
    }
    assert client.post("/api/v1/tasks", content=raw, headers=bad_headers).status_code == 401


def test_production_does_not_auto_create_sqlite_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "auto_create_schema", False)
    db.configure_database(f"sqlite:///{tmp_path / 'production.db'}")
    with pytest.raises(RuntimeError, match="SQLite"):
        with TestClient(create_app()):
            pass


def test_alembic_initial_schema_is_reproducible(tmp_path):
    import os
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    env = os.environ.copy()
    env["AGENTFORGE_DATABASE_URL"] = url
    server_path = str(repo_root / "server")
    env["PYTHONPATH"] = f"{server_path}{os.pathsep}{env.get('PYTHONPATH', '')}"
    import sys
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    engine = db._build_engine(url)
    tables = set(__import__("sqlalchemy").inspect(engine).get_table_names())
    assert {"agents", "claims", "inference_sessions", "idempotency_records", "outbox_events", "validation_decisions"} <= tables
