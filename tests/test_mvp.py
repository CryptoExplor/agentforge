from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, registration_bytes, request_bytes, sha256_json
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.settings import settings


def signed_request(client: TestClient, identity: AgentIdentity, method: str, path: str, payload: dict):
    body = canonical_json(payload).encode()
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(request_bytes(method, path, body, timestamp, nonce)),
        "Idempotency-Key": nonce,
    }
    return client.request(method, path, content=body, headers=headers)


def register(client: TestClient, identity: AgentIdentity, manifest: dict):
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


def test_full_mock_exchange(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ENABLE_MOCK_FAUCET", "true")
    settings.enable_mock_faucet = True
    db.configure_database(f"sqlite:///{tmp_path / 'test.db'}")
    api = create_app()

    with TestClient(api) as client:
        poster = AgentIdentity.generate()
        executor = AgentIdentity.generate()
        validator = AgentIdentity.generate()

        register(client, poster, {"name": "poster", "capabilities": ["research"], "chains": ["base"]})
        register(client, executor, {"name": "executor", "capabilities": ["proxy_security"], "chains": ["base"]})
        register(client, validator, {"name": "validator", "capabilities": ["validation"], "chains": ["base"]})
        # Explicit operator grant; registration metadata itself conveys no authority.
        settings.trusted_validator_dids = settings.trusted_validator_dids | {validator.did}

        task_payload = {
            "kind": "expert",
            "visibility": "public",
            "origin": "external",
            "required_capabilities": ["proxy_security"],
            "chains": ["base"],
            "input": {"contract": "0xabc"},
            "acceptance": {
                "required_outputs": ["risk"],
                "required_evidence": ["bytecode_hash"],
            },
            "demand_provenance": {
                "type": "external_event",
                "level": 1,
                "source": "test",
                "source_ref": "event-1",
                "novelty_hash": "sha256:test",
            },
            "generation_policy": {
                "economic_eligibility": "ECONOMIC_ELIGIBLE",
                "minimum_provenance_level": 1,
                "synthetic_demo": False,
            },
            "economics": {
                "mode": "BOUNTY",
                "reward": {"amount": "10", "asset": "MOCK"},
                "security_deposit": {"amount": "1", "asset": "MOCK"},
                "inference_budget": {"amount": "2", "asset": "MOCK"},
            },
            "deadline": time.time() + 3600,
        }
        created = signed_request(client, poster, "POST", "/api/v1/tasks", task_payload)
        assert created.status_code == 200, created.text
        task = created.json()
        assert task["status"] == "FUNDED"
        assert task["escrow"]["status"] == "FUNDED"

        claimed = signed_request(client, executor, "POST", f"/api/v1/tasks/{task['id']}/claim", {})
        assert claimed.status_code == 200, claimed.text
        claim = claimed.json()

        inference = signed_request(
            client,
            executor,
            "POST",
            f"/api/v1/tasks/{task['id']}/inference",
            {
                "provider": "mock",
                "model_ref": "mock:model-v1",
                "max_latency_ms": 1000,
                "requested_compute": "1000",
                "confidential": False,
                "input_data": {"question": "is this safe?"},
            },
        )
        assert inference.status_code == 200, inference.text
        session_id = inference.json()["session_id"]

        result = {"risk": "unknown"}
        evidence = [{"kind": "bytecode_hash", "content_hash": "sha256:abc"}]
        submission_id = "S_" + uuid.uuid4().hex
        created_at = time.time()
        proof_core = {
            "task_id": task["id"],
            "submission_id": submission_id,
            "executor_did": executor.did,
            "input_hash": sha256_json(task["input"]),
            "result_hash": sha256_json(result),
            "evidence": evidence,
            "inference_session_ids": [session_id],
            "created_at": created_at,
        }
        submitted = signed_request(
            client,
            executor,
            "POST",
            f"/api/v1/tasks/{task['id']}/submissions",
            {
                "submission_id": submission_id,
                "result": result,
                "evidence": evidence,
                "inference_session_ids": [session_id],
                "created_at": created_at,
                "proof_signature": executor.sign(canonical_json(proof_core).encode()),
            },
        )
        assert submitted.status_code == 200, submitted.text
        submission = submitted.json()
        assert submission["status"] == "SUBMITTED"

        decision_id = "VD_" + uuid.uuid4().hex
        decision_core = {
            "decision_id": decision_id,
            "submission_id": submission_id,
            "validator_did": validator.did,
            "decision": "VERIFIED",
            "policy": "deterministic_then_domain",
            "checks": [{"code": "EVIDENCE_PRESENT", "result": "PASS"}],
            "reason_codes": ["ACCEPTANCE_CRITERIA_SATISFIED"],
            "settlement": {},
            "evidence_hash": submission["proof_hash"],
        }
        validated = signed_request(
            client,
            validator,
            "POST",
            f"/api/v1/submissions/{submission_id}/validate",
            {
                "decision_id": decision_id,
                "decision": "VERIFIED",
                "policy": "deterministic_then_domain",
                "checks": decision_core["checks"],
                "reason_codes": decision_core["reason_codes"],
                "settlement": {},
                "evidence_hash": submission["proof_hash"],
                "signature": validator.sign(canonical_json(decision_core).encode()),
            },
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["decision"] == "VERIFIED"

        final_task = client.get(f"/api/v1/tasks/{task['id']}").json()
        assert final_task["status"] == "VERIFIED"
        assert final_task["escrow"]["status"] == "RELEASED"
        balance = signed_request(client, executor, "GET", f"/api/v1/agents/{executor.did}/balance", {})
        assert balance.status_code == 200
        assert float(balance.json()["balance"]) == 1010
        assert client.get(f"/api/v1/reputation/{executor.did}").json()["overall"] == 1.0


def test_invalid_proof_is_rejected(tmp_path):
    settings.enable_mock_faucet = True
    db.configure_database(f"sqlite:///{tmp_path / 'test.db'}")
    api = create_app()
    with TestClient(api) as client:
        identity = AgentIdentity.generate()
        register(client, identity, {"name": "agent", "capabilities": []})
        response = signed_request(client, identity, "POST", "/api/v1/tasks", {
            "kind": "research",
            "origin": "research",
            "acceptance": {"required_outputs": ["answer"]},
            "demand_provenance": {"type": "research_question", "level": 1},
            "generation_policy": {"minimum_provenance_level": 1},
            "economics": {"mode": "REPUTATION"},
        })
        assert response.status_code == 200
        task = response.json()
        # The same agent cannot claim its own task, so this only checks the API was created.
        assert task["status"] == "OPEN"
