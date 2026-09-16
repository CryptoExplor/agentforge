"""Asset and mode guardrails - PR2: server-derived settlement modes.

- Mock settlement provider must reject FLOP and non-allowed assets.
- Only MOCK and TEST_CREDIT are allowed.
- Provider and deployment mode are server-derived, not client-selectable.
- Eligibility statuses remain server-derived.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from agentforge_sdk.client import AgentIdentity
from agentforge_sdk.crypto import canonical_json, registration_bytes, request_bytes
from agentforge_server import db
from agentforge_server.app import create_app
from agentforge_server.settings import settings
from agentforge_server.settlement import get_deployment_mode, get_settlement_provider


def signed_request(client: TestClient, identity: AgentIdentity, method: str, path: str, payload: dict, *, key: str | None = None):
    body = canonical_json(payload).encode()
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Agent-DID": identity.did,
        "X-Agent-Timestamp": timestamp,
        "X-Agent-Nonce": nonce,
        "X-Agent-Signature": identity.sign(request_bytes(method, path.split("?", 1)[0], body, timestamp, nonce)),
        "Idempotency-Key": key or f"idem-{uuid.uuid4().hex}",
    }
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
        registration_bytes(challenge["challenge_id"], challenge["nonce"], identity.did, manifest)
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
    # Ensure provider and deployment mode are server-derived defaults
    monkeypatch.setattr(settings, "settlement_provider", "mock")
    monkeypatch.setattr(settings, "deployment_mode", "local")
    db.configure_database(f"sqlite:///{tmp_path / 'guardrails.db'}")
    with TestClient(create_app()) as test_client:
        yield test_client


def test_mock_provider_rejects_flop_asset(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    payload = {
        "kind": "research",
        "visibility": "public",
        "origin": "research",
        "required_capabilities": [],
        "chains": ["base"],
        "input": {"question": "test flop rejection"},
        "acceptance": {"required_outputs": ["answer"]},
        "demand_provenance": {"type": "research_question", "level": 3},
        "generation_policy": {"minimum_provenance_level": 1},
        "economics": {"mode": "BOUNTY", "reward": {"amount": "10", "asset": "FLOP"}},
    }
    resp = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
    assert resp.status_code == 400, resp.text
    assert "not supported" in resp.text.lower() or "flop" in resp.text.lower()


def test_mock_provider_accepts_mock_and_test_credit(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    for asset in ["MOCK", "TEST_CREDIT", "mock", "test_credit"]:
        payload = {
            "kind": "research",
            "visibility": "public",
            "origin": "research",
            "required_capabilities": [],
            "chains": ["base"],
            "input": {"question": f"test {asset}"},
            "acceptance": {"required_outputs": ["answer"]},
            "demand_provenance": {"type": "research_question", "level": 3},
            "generation_policy": {"minimum_provenance_level": 1},
            "economics": {"mode": "BOUNTY", "reward": {"amount": "1", "asset": asset}},
        }
        resp = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
        assert resp.status_code == 200, f"asset {asset} should be allowed: {resp.text}"
        assert resp.json()["escrow"]["asset"] in {"MOCK", "TEST_CREDIT"}


def test_mock_provider_rejects_unknown_assets(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    for asset in ["ETH", "USDC", "BTC", "REAL"]:
        payload = {
            "kind": "research",
            "visibility": "public",
            "origin": "research",
            "required_capabilities": [],
            "chains": ["base"],
            "input": {"question": f"test {asset}"},
            "acceptance": {"required_outputs": ["answer"]},
            "demand_provenance": {"type": "research_question", "level": 3},
            "generation_policy": {"minimum_provenance_level": 1},
            "economics": {"mode": "BOUNTY", "reward": {"amount": "1", "asset": asset}},
        }
        resp = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
        assert resp.status_code == 400, f"asset {asset} should be rejected"
        assert "not supported" in resp.text.lower()


def test_client_cannot_choose_network_or_provider_mode(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    # Try to inject extra fields that would select network/provider
    payload = {
        "kind": "research",
        "visibility": "public",
        "origin": "research",
        "required_capabilities": [],
        "chains": ["base"],
        "input": {"question": "test mode guardrail"},
        "acceptance": {"required_outputs": ["answer"]},
        "demand_provenance": {"type": "research_question", "level": 3},
        "generation_policy": {"minimum_provenance_level": 1},
        "economics": {"mode": "BOUNTY", "reward": {"amount": "1", "asset": "MOCK"}},
        "network": "MAINNET",
        "deployment_mode": "MAINNET",
        "provider_mode": "FLOP",
        "settlement_provider": "flop",
    }
    resp = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
    # Pydantic extra=forbid should reject unknown fields
    assert resp.status_code == 422, resp.text


def test_eligibility_is_server_derived_not_client_selected(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    # Client tries to claim EXTERNAL_NETWORK_VERIFIED but has low provenance
    payload = {
        "kind": "research",
        "visibility": "public",
        "origin": "research",
        "required_capabilities": [],
        "chains": ["base"],
        "input": {"question": "test eligibility"},
        "acceptance": {"required_outputs": ["answer"]},
        "demand_provenance": {"type": "research_question", "level": 0},
        "generation_policy": {
            "economic_eligibility": "EXTERNAL_NETWORK_VERIFIED",
            "minimum_provenance_level": 1,
        },
        "economics": {"mode": "REPUTATION"},
    }
    resp = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
    assert resp.status_code == 200, resp.text
    task = resp.json()
    # Server must derive NOT_ELIGIBLE because verified_level 0 < minimum 1
    assert task["activity_eligibility"] == "NOT_ELIGIBLE"
    assert task["demand_provenance"]["verified_level"] == 0
    # Even though client asked for EXTERNAL_NETWORK_VERIFIED, server did not grant it
    assert task["activity_eligibility"] != "EXTERNAL_NETWORK_VERIFIED"


def test_deployment_and_settlement_mode_are_server_derived(client):
    # Settings are server-derived
    assert get_settlement_provider() is not None
    assert get_deployment_mode() == "local"
    # Provider should be mock
    from agentforge_server.adapters.mock_settlement import MockSettlementProvider

    assert isinstance(get_settlement_provider(), MockSettlementProvider)


def test_unsupported_settlement_provider_raises(monkeypatch):
    from agentforge_server.settlement import reset_settlement_provider

    monkeypatch.setattr(settings, "settlement_provider", "flop_onchain")
    reset_settlement_provider()
    try:
        with pytest.raises(RuntimeError, match="not enabled in MVP"):
            get_settlement_provider()
    finally:
        reset_settlement_provider()


def test_zero_reward_task_with_test_credit_deposit(client):
    poster = AgentIdentity.generate()
    register(client, poster)
    payload = {
        "kind": "research",
        "visibility": "public",
        "origin": "research",
        "required_capabilities": [],
        "chains": ["base"],
        "input": {"question": "deposit in test credit"},
        "acceptance": {"required_outputs": ["answer"]},
        "demand_provenance": {"type": "research_question", "level": 3},
        "generation_policy": {"minimum_provenance_level": 1},
        "economics": {
            "mode": "REPUTATION",
            "security_deposit": {"amount": "5", "asset": "TEST_CREDIT"},
        },
    }
    resp = signed_request(client, poster, "POST", "/api/v1/tasks", payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["escrow"]["asset"] == "TEST_CREDIT"
    assert resp.json()["escrow"]["deposit_amount"] == "5"
