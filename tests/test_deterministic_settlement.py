"""Task-level verification strategies: deterministic auto-settlement.

A task created with ``verification_strategy="deterministic"`` is verified by the
server when the proof bundle is submitted, and is settled inside that same
transaction: no validator is asked to arrive, so deterministic work cannot be
starved or griefed, and a signed proof gets an immediate verdict.

These tests pin the observable contract:

- valid proof -> submission/task ``VERIFIED`` and escrow ``RELEASED``;
- invalid proof -> submission/task ``REJECTED``, requester refunded, no payout;
- ``peer_review``/``operator`` tasks keep waiting for a signed decision;
- a late validator gets ``409`` on an already auto-settled deterministic task.

All values are asserted against the mock ledger's Decimal/string accounting.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, registration_bytes, request_bytes, sha256_json
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.models import Claim, Escrow, OutboxEvent, ReputationEvent, Submission, Task
from agentforge_server.schemas import TaskCreate, TaskResponse
from agentforge_server.settings import settings


def signed_request(
    client: TestClient,
    identity: AgentIdentity,
    method: str,
    path: str,
    payload: dict,
    *,
    key: str | None = None,
) -> Response:
    body = canonical_json(payload).encode()
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(request_bytes(method, path, body, timestamp, nonce)),
        "Idempotency-Key": key or f"idem-{uuid.uuid4().hex}",
    }
    return client.request(method, path, content=body, headers=headers)


def register(client: TestClient, identity: AgentIdentity, manifest: dict) -> dict:
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
    db.configure_database(f"sqlite:///{tmp_path / 'deterministic.db'}")
    with TestClient(create_app()) as test_client:
        yield test_client


def task_payload(
    *,
    kind: str = "deterministic",
    acceptance: dict,
    verification_strategy: str | None = None,
    economics: dict | None = None,
    required_capabilities: list[str] | None = None,
    deadline: float | None = None,
) -> dict:
    payload: dict = {
        "kind": kind,
        "visibility": "public",
        "origin": "external",
        "required_capabilities": required_capabilities or [],
        "chains": ["base"],
        "input": {"question": "hash this"},
        "acceptance": acceptance,
        "demand_provenance": {"type": "external_request", "level": 3, "novelty_hash": "sha256:test"},
        "generation_policy": {"minimum_provenance_level": 1},
        "economics": economics
        or {
            "mode": "BOUNTY",
            "reward": {"amount": "10", "asset": "MOCK"},
            "security_deposit": {"amount": "1", "asset": "MOCK"},
            "inference_budget": {"amount": "2", "asset": "MOCK"},
        },
    }
    if verification_strategy is not None:
        payload["verification_strategy"] = verification_strategy
    if deadline is not None:
        payload["deadline"] = deadline
    return payload


def create_task(client: TestClient, poster: AgentIdentity, payload: dict) -> dict:
    response = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
    assert response.status_code == 200, response.text
    return response.json()


def claim_task(client: TestClient, task: dict, executor: AgentIdentity) -> dict:
    response = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
    assert response.status_code == 200, response.text
    return response.json()


def submit_payload(
    task: dict,
    executor: AgentIdentity,
    *,
    result: dict,
    submission_id: str | None = None,
    created_at: float | None = None,
) -> dict:
    submission_id = submission_id or f"S_{uuid.uuid4().hex}"
    created_at = created_at or time.time()
    proof_core = {
        "task_id": task["id"],
        "submission_id": submission_id,
        "executor_did": executor.did,
        "input_hash": sha256_json(task["input"]),
        "result_hash": sha256_json(result),
        "evidence": [],
        "inference_session_ids": [],
        "created_at": created_at,
    }
    return {
        "submission_id": submission_id,
        "result": result,
        "evidence": [],
        "inference_session_ids": [],
        "created_at": created_at,
        "proof_signature": executor.sign(canonical_json(proof_core).encode()),
    }


def submit(client: TestClient, task: dict, executor: AgentIdentity, *, result: dict, **kwargs):
    payload = submit_payload(task, executor, result=result, **kwargs)
    return signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    )


def validation_payload(submission: dict, validator: AgentIdentity, decision: str = "VERIFIED") -> dict:
    body = {
        "decision_id": f"VD_{uuid.uuid4().hex}",
        "decision": decision,
        "policy": "deterministic_then_domain",
        "checks": [{"code": "CLIENT_CLAIM", "result": "PASS"}],
        "reason_codes": [],
        "settlement": {},
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


def validate(client: TestClient, validator: AgentIdentity, submission: dict, *, decision: str = "VERIFIED"):
    body = validation_payload(submission, validator, decision)
    return signed_request(
        client,
        validator,
        "POST",
        f"/api/v1/submissions/{submission['submission_id']}/validate",
        body,
    )


def setup_agents(client: TestClient):
    poster = AgentIdentity.generate()
    executor = AgentIdentity.generate()
    validator = AgentIdentity.generate()
    register(client, poster, {"name": "poster", "capabilities": ["deterministic_exec"], "chains": ["base"]})
    register(client, executor, {"name": "executor", "capabilities": ["deterministic_exec"], "chains": ["base"]})
    register(client, validator, {"name": "validator", "capabilities": ["validation"], "chains": ["base"]})
    # Explicit operator grant: registration metadata alone conveys no validator
    # authority, and an auto-settled deterministic task must still be refused to
    # a validator that *is* allowed to validate every other task.
    settings.trusted_validator_dids = settings.trusted_validator_dids | {validator.did}
    return poster, executor, validator


def balance(client: TestClient, identity: AgentIdentity) -> str:
    response = signed_request(client, identity, "GET", f"/api/v1/agents/{identity.did}/balance", {})
    assert response.status_code == 200, response.text
    return response.json()["balance"]


def outbox_kinds() -> list[str]:
    with db.SessionLocal() as session:
        return [row.kind for row in session.scalars(select(OutboxEvent)).all()]


# --------------------------------------------------------------- auto-settle

def test_deterministic_task_auto_settles_on_valid_submission(client):
    poster, executor, _ = setup_agents(client)
    result = {"answer": 42, "digest": "sha256:deadbeef"}
    acceptance = {
        "required_outputs": ["answer"],
        "expected_result_hash": sha256_json(result),
        "result_schema": {
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
        },
    }
    task = create_task(
        client,
        poster,
        task_payload(acceptance=acceptance, verification_strategy="deterministic"),
    )
    assert task["verification_strategy"] == "deterministic"
    assert task["acceptance_hash"] == sha256_json(acceptance)
    assert float(balance(client, poster)) == 987  # 1000 - (10 reward + 1 deposit + 2 budget)
    claim = claim_task(client, task, executor)

    response = submit(client, task, executor, result=result)
    assert response.status_code == 200, response.text
    submission = response.json()

    # The verdict is immediate: no validator, no waiting.
    assert submission["verification_strategy"] == "deterministic"
    assert submission["status"] == "VERIFIED"
    assert submission["verification"]["decision"] == "VERIFIED"
    assert submission["verification"]["failed_checks"] == []

    final_task = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert final_task["status"] == "VERIFIED"
    assert final_task["escrow"]["status"] == "RELEASED"
    assert final_task["escrow"]["transition"] == "FULL_RELEASE"
    assert final_task["escrow"]["released_amount"] == "10"
    assert final_task["escrow"]["refunded_amount"] == "3"  # deposit + unused budget
    assert final_task["escrow"]["reserved_total"] == "13"
    # The executor is paid, and the requester keeps the unspent reserve.
    assert float(balance(client, executor)) == 1010
    assert float(balance(client, poster)) == 990

    reputation = client.get(f"/api/v1/reputation/{executor.did}").json()
    assert reputation["overall"] == 1.0
    assert reputation["by_role"]["executor"] == 1.0

    with db.SessionLocal() as session:
        stored_task = session.get(Task, task["id"])
        stored_claim = session.get(Claim, claim["claim_id"])
        stored_submission = session.get(Submission, submission["submission_id"])
        events = session.scalars(
            select(ReputationEvent).where(ReputationEvent.did == executor.did)
        ).all()
        assert stored_submission.status == "VERIFIED"
        assert stored_claim.status == "COMPLETED"
        assert stored_task.verification_strategy == "deterministic"
        assert [(event.kind, event.delta) for event in events] == [("task_verified", 1.0)]

    assert "TASK_VERIFIED" in outbox_kinds()
    with db.SessionLocal() as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "TASK_VERIFIED"))
        assert event is not None
        # Dual attribution: the request actor plus the verified request, and both
        # counterparties of the settled exchange in the payload.
        assert event.actor_did == executor.did
        assert event.aggregate_id == task["id"]
        assert event.causation["request_id"]
        assert event.payload["poster_did"] == poster.did
        assert event.payload["executor_did"] == executor.did
        assert event.payload["decision"] == "VERIFIED"


def test_deterministic_task_auto_rejects_on_mismatched_output(client):
    poster, executor, _ = setup_agents(client)
    expected = {"answer": 42}
    task = create_task(
        client,
        poster,
        task_payload(
            acceptance={
                "required_outputs": ["answer"],
                "expected_result_hash": sha256_json(expected),
            },
            verification_strategy="deterministic",
        ),
    )
    claim = claim_task(client, task, executor)
    funded_poster_balance = float(balance(client, poster))

    response = submit(client, task, executor, result={"answer": 43})
    assert response.status_code == 200, response.text
    submission = response.json()
    assert submission["status"] == "REJECTED"
    assert submission["verification"]["decision"] == "REJECTED"
    assert submission["verification"]["failed_checks"] == ["EXPECTED_RESULT_HASH_MATCH"]

    final_task = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert final_task["status"] == "REJECTED"
    assert final_task["escrow"]["status"] == "REFUNDED"
    assert final_task["escrow"]["transition"] == "REFUND"
    assert final_task["escrow"]["released_amount"] == "0"
    assert final_task["escrow"]["refunded_amount"] == "13"
    # The whole reserve returns to the requester and the executor is not paid.
    assert float(balance(client, poster)) == funded_poster_balance + 13
    assert float(balance(client, executor)) == 1000

    reputation = client.get(f"/api/v1/reputation/{executor.did}").json()
    assert reputation["overall"] == -1.0
    assert reputation["by_role"]["executor"] == -1.0

    with db.SessionLocal() as session:
        events = session.scalars(
            select(ReputationEvent).where(ReputationEvent.did == executor.did)
        ).all()
        assert [(event.kind, event.delta) for event in events] == [("task_rejected", -1.0)]
        assert session.get(Claim, claim["claim_id"]).status == "COMPLETED"
        escrow = session.get(Escrow, task["id"])
        assert escrow.status == "REFUNDED"

    assert "TASK_REJECTED" in outbox_kinds()


def test_deterministic_task_rejects_missing_or_invalid_schema_output(client):
    poster, executor, _ = setup_agents(client)
    task = create_task(
        client,
        poster,
        task_payload(
            acceptance={
                "required_outputs": ["answer"],
                "result_schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
            verification_strategy="deterministic",
        ),
    )
    claim_task(client, task, executor)

    # Structurally signed, but the declared schema does not accept the result.
    response = submit(client, task, executor, result={"answer": 42})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "REJECTED"
    assert "RESULT_SCHEMA_VALID" in response.json()["verification"]["failed_checks"]
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["escrow"]["status"] == "REFUNDED"

    # The claim is completed by auto-settlement, so a second submission is refused.
    second = submit(client, task, executor, result={"answer": "42"})
    assert second.status_code == 409


def test_deterministic_reputation_task_settles_without_escrow(client):
    poster, executor, _ = setup_agents(client)
    task = create_task(
        client,
        poster,
        task_payload(
            acceptance={"required_outputs": ["answer"]},
            verification_strategy="deterministic",
            economics={"mode": "REPUTATION"},
        ),
    )
    assert task["escrow"] is None
    claim_task(client, task, executor)

    response = submit(client, task, executor, result={"answer": "ok"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "VERIFIED"

    final_task = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert final_task["status"] == "VERIFIED"
    assert final_task["escrow"] is None
    assert client.get(f"/api/v1/reputation/{executor.did}").json()["overall"] == 1.0


def test_auto_settlement_is_atomic_and_conserves_escrow(client):
    poster, executor, _ = setup_agents(client)
    result = {"answer": 7}
    task = create_task(
        client,
        poster,
        task_payload(
            acceptance={"required_outputs": ["answer"], "expected_result_hash": sha256_json(result)},
            verification_strategy="deterministic",
        ),
    )
    claim_task(client, task, executor)
    assert submit(client, task, executor, result=result).status_code == 200

    with db.SessionLocal() as session:
        escrow = session.get(Escrow, task["id"])
        # One commit produced the terminal state, the ledger movement, the
        # reputation event, and the outbox event together.
        assert escrow.status == "RELEASED"
        assert escrow.released_amount == "10"
        assert escrow.refunded_amount == "3"
        assert int(escrow.released_amount) + int(escrow.refunded_amount) + int(
            escrow.slashed_amount
        ) == int(escrow.reserved_total)
        assert session.get(Task, task["id"]).status == "VERIFIED"
        events = session.scalars(select(OutboxEvent).where(OutboxEvent.kind == "TASK_VERIFIED")).all()
        assert len(events) == 1

    # A second, competing submission cannot double-settle the escrow.
    replay = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/submissions",
        submit_payload(task, executor, result=result),
    )
    assert replay.status_code == 409
    assert float(balance(client, executor)) == 1010


@pytest.mark.parametrize("strategy", ["peer_review", "operator"])
def test_peer_review_strategy_remains_manual(client, strategy):
    poster, executor, validator = setup_agents(client)
    task = create_task(
        client,
        poster,
        task_payload(acceptance={"required_outputs": ["answer"]}, verification_strategy=strategy),
    )
    assert task["verification_strategy"] == strategy
    claim_task(client, task, executor)

    response = submit(client, task, executor, result={"answer": "ok"})
    assert response.status_code == 200, response.text
    submission = response.json()
    # Nothing settles until an independent validator signs a decision.
    assert submission["status"] == "SUBMITTED"
    assert "verification" not in submission
    pending = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert pending["status"] == "SUBMITTED"
    assert pending["escrow"]["status"] == "FUNDED"
    assert float(balance(client, executor)) == 1000

    assert validate(client, validator, submission).status_code == 200
    settled = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert settled["status"] == "VERIFIED"
    assert settled["escrow"]["status"] == "RELEASED"
    assert float(balance(client, executor)) == 1010
    assert "TASK_VERIFIED" not in outbox_kinds()


def test_deterministic_auto_settlement_verifies_attached_inference_sessions(client):
    poster, executor, _ = setup_agents(client)
    result = {"answer": "ok"}
    task = create_task(
        client,
        poster,
        task_payload(
            acceptance={"required_outputs": ["answer"], "expected_result_hash": sha256_json(result)},
            verification_strategy="deterministic",
        ),
    )
    claim_task(client, task, executor)
    inference = signed_request(
        client,
        executor,
        "POST",
        f"/api/v1/tasks/{task['id']}/inference",
        {
            "provider": "mock",
            "model_ref": "mock:model-v1",
            "requested_compute": "1.5",
            "input_data": {"question": "hash this"},
        },
    )
    assert inference.status_code == 200, inference.text
    session_id = inference.json()["session_id"]

    payload = submit_payload(task, executor, result=result)
    payload["inference_session_ids"] = [session_id]
    proof_core = {
        "task_id": task["id"],
        "submission_id": payload["submission_id"],
        "executor_did": executor.did,
        "input_hash": sha256_json(task["input"]),
        "result_hash": sha256_json(result),
        "evidence": [],
        "inference_session_ids": [session_id],
        "created_at": payload["created_at"],
    }
    payload["proof_signature"] = executor.sign(canonical_json(proof_core).encode())
    submitted = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    )
    assert submitted.status_code == 200, submitted.text
    # Receipt integrity is part of the deterministic tier, not a validator's job.
    assert submitted.json()["status"] == "VERIFIED"
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["escrow"]["status"] == "RELEASED"


def test_tampered_proof_signature_is_rejected_before_auto_settlement(client):
    poster, executor, _ = setup_agents(client)
    task = create_task(
        client,
        poster,
        task_payload(acceptance={"required_outputs": ["answer"]}, verification_strategy="deterministic"),
    )
    claim_task(client, task, executor)
    payload = submit_payload(task, executor, result={"answer": "ok"})
    payload["proof_signature"] = AgentIdentity.generate().sign(b"not this proof")

    rejected = signed_request(
        client, executor, "POST", f"/api/v1/tasks/{task['id']}/submissions", payload
    )
    assert rejected.status_code == 401
    # Nothing settled and nothing was paid for an unauthenticated proof bundle.
    pending = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert pending["status"] == "CLAIMED"
    assert pending["escrow"]["status"] == "FUNDED"
    assert float(balance(client, executor)) == 1000


def test_task_representation_matches_the_documented_response_schema(client):
    poster, _, _ = setup_agents(client)
    task = create_task(
        client,
        poster,
        task_payload(
            acceptance={"required_outputs": ["answer"]},
            verification_strategy="deterministic",
        ),
    )
    # The documented TaskResponse schema must accept exactly what the API returns,
    # including the strategy field, so the contract cannot silently drift.
    parsed = TaskResponse.model_validate(task)
    assert parsed.verification_strategy == "deterministic"
    assert parsed.status == "FUNDED"
    assert parsed.escrow is not None and parsed.escrow.status == "FUNDED"
    assert TaskCreate.model_validate({"acceptance": {"required_outputs": ["answer"]}}).verification_strategy == "peer_review"
    assert (
        TaskCreate.model_validate(
            {"acceptance": {"required_outputs": ["answer"]}, "verification_strategy": "operator"}
        ).verification_strategy
        == "operator"
    )
    with pytest.raises(ValueError):
        TaskCreate.model_validate(
            {"acceptance": {"required_outputs": ["answer"]}, "verification_strategy": "trust_me"}
        )


def test_default_verification_strategy_is_peer_review(client):
    poster, executor, _ = setup_agents(client)
    task = create_task(client, poster, task_payload(acceptance={"required_outputs": ["answer"]}))
    assert task["verification_strategy"] == "peer_review"
    claim_task(client, task, executor)
    response = submit(client, task, executor, result={"answer": "ok"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SUBMITTED"


def test_competing_validator_blocked_on_auto_settled_task(client):
    poster, executor, validator = setup_agents(client)
    result = {"answer": 42}
    task = create_task(
        client,
        poster,
        task_payload(
            acceptance={"required_outputs": ["answer"], "expected_result_hash": sha256_json(result)},
            verification_strategy="deterministic",
        ),
    )
    claim_task(client, task, executor)
    submission = submit(client, task, executor, result=result).json()
    assert submission["status"] == "VERIFIED"

    blocked = validate(client, validator, submission)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "task has already settled via deterministic strategy"

    # The blocked decision changed nothing: no decision row, no re-settlement.
    with db.SessionLocal() as session:
        assert session.get(Escrow, task["id"]).status == "RELEASED"
        assert session.get(Submission, submission["submission_id"]).status == "VERIFIED"
    assert float(balance(client, executor)) == 1010
    assert client.get(f"/api/v1/reputation/{validator.did}").json()["overall"] == 0.0


def test_competing_validator_blocked_on_auto_rejected_task(client):
    poster, executor, validator = setup_agents(client)
    task = create_task(
        client,
        poster,
        task_payload(
            acceptance={"required_outputs": ["answer"], "expected_result_hash": sha256_json({"answer": 42})},
            verification_strategy="deterministic",
        ),
    )
    claim_task(client, task, executor)
    submission = submit(client, task, executor, result={"answer": "wrong"}).json()
    assert submission["status"] == "REJECTED"

    blocked = validate(client, validator, submission, decision="VERIFIED")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "task has already settled via deterministic strategy"
    # A rejected deterministic task cannot be re-opened by a friendly validator.
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "REJECTED"
    assert float(balance(client, executor)) == 1000
